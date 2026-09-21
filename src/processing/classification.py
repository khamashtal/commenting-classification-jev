"""Classification stage: deterministic signals, the Jev calls, exclusions and ranking.

The division of labour is deliberate. Anything code can compute exactly — counting words,
measuring capitals, spotting a URL — stays in code, because Jev's own documentation says
it does not count reliably. Jev is asked only for judgments a knowledgeable person makes
in a second.

Every threshold and weight lives in a constant at the top of this module, so changing
policy is a number under review rather than a reworded question.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient, SystemOneResponse

from clients.vf_mcp import Comment
from processing.fetch import ArticleThread
from processing.log_config import logger
from processing.questions import (
    BATTERY,
    CHOICE_IDS,
    NOUL_IDS,
    SCORE_IDS,
    build_state,
    top_level,
)

# ------------------------------------------------------------------- code-side policy

MIN_WORDS = 15  # below this the brief calls a comment substance-free
MAX_CAPS_RATIO = 0.5  # share of alphabetic characters that may be upper case
SWEET_SPOT = (20, 100)  # the brief's preferred length, reported but not enforced
TELEGRAPH_DOMAINS = ("telegraph.co.uk",)

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_WORD_RE = re.compile(r"\S+")

# ------------------------------------------------------------------ Jev-side policy

# id -> (excludes at or beyond, flags for review at or beyond). A signal whose value
# rises with risk uses "high" direction; on_topic is the one where a LOW value is bad.
EXCLUDE_AT: dict[str, float] = {
    "sarcasm": 0.70,
    "personal_attack": 0.70,
    "group_hostility": 0.60,
    "profanity_or_threat": 0.60,
    "unverified_claim": 1.5,  # Score, 0–2; only the top level is a real problem
}
FLAG_AT: dict[str, float] = {
    "sarcasm": 0.40,
    "personal_attack": 0.40,
    "group_hostility": 0.35,
    "profanity_or_threat": 0.35,
    "unverified_claim": 1.0,
}
# on_topic runs the other way: high is good.
ON_TOPIC_EXCLUDE_BELOW = 0.35
ON_TOPIC_FLAG_BELOW = 0.60

WEIGHTS: dict[str, float] = {
    "experience": 0.35,  # personal_experience x experience_relevant
    "tone": 0.15,
    "readability": 0.10,
    "contribution": 0.15,  # max(proposes_solution, reasoned_argument)
    "standalone": 0.10,
    "representativeness": 0.15,
}


# ----------------------------------------------------------------------- data classes


@dataclass(frozen=True, slots=True)
class CodeSignals:
    """What code can measure exactly, with no model involved."""

    word_count: int
    caps_ratio: float
    has_external_url: bool
    has_paragraph_break: bool

    @property
    def in_sweet_spot(self) -> bool:
        return SWEET_SPOT[0] <= self.word_count <= SWEET_SPOT[1]


@dataclass(slots=True)
class Classification:
    """One comment, everything measured about it, and the verdict."""

    comment: Comment
    signals: CodeSignals
    answers: dict[str, Any] = field(default_factory=dict)
    excluded_reason: str | None = None
    flags: list[str] = field(default_factory=list)
    quality_score: float = 0.0
    error: str | None = None

    @property
    def classified(self) -> bool:
        """Whether Jev actually saw this comment."""
        return bool(self.answers)

    @property
    def pin_eligible(self) -> bool:
        """Replies cannot be pinned; they can still go in a carousel."""
        return not self.comment.is_reply

    @property
    def stance(self) -> str:
        value = self.answers.get("stance")
        return value["choice"] if isinstance(value, dict) else "unknown"

    def noul(self, question_id: str) -> float:
        value = self.answers.get(question_id)
        return float(value) if isinstance(value, (int, float)) else 0.0

    def score(self, question_id: str) -> float:
        value = self.answers.get(question_id)
        return float(value) if isinstance(value, (int, float)) else 0.0

    def normalised(self, question_id: str) -> float:
        """A Score mapped onto 0–1 by dividing by its top level."""
        return self.score(question_id) / top_level(question_id)


# ------------------------------------------------------------------ deterministic pass


def compute_signals(text: str) -> CodeSignals:
    """Measure a comment without calling anything."""
    words = _WORD_RE.findall(text)
    letters = [c for c in text if c.isalpha()]
    caps = sum(1 for c in letters if c.isupper())
    return CodeSignals(
        word_count=len(words),
        caps_ratio=(caps / len(letters)) if letters else 0.0,
        has_external_url=_has_external_url(text),
        has_paragraph_break="\n\n" in text.strip() or "\n" in text.strip(),
    )


def _has_external_url(text: str) -> bool:
    """Any link that is not on a Telegraph domain.

    The brief excludes these because we cannot vouch for a page we do not control, nor
    guarantee it stays the same.
    """
    for match in _URL_RE.findall(text):
        host = match.removeprefix("https://").removeprefix("http://").split("/")[0]
        host = host.removeprefix("www.").lower()
        if not any(
            host == domain or host.endswith(f".{domain}")
            for domain in TELEGRAPH_DOMAINS
        ):
            return True
    return False


def hard_exclusion(comment: Comment, signals: CodeSignals) -> str | None:
    """A reason to skip Jev entirely, or None to go ahead.

    Applied before classification, so a four-word comment never costs an API call.
    """
    if comment.state and comment.state != "visible":
        return f"not visible (state: {comment.state})"
    if signals.word_count < MIN_WORDS:
        return f"too short ({signals.word_count} words, minimum {MIN_WORDS})"
    if signals.has_external_url:
        return "contains a link off telegraph.co.uk"
    if signals.caps_ratio > MAX_CAPS_RATIO:
        return f"mostly capitals ({signals.caps_ratio:.0%})"
    return None


# ------------------------------------------------------------------------ the Jev pass


def _read_answers(response: SystemOneResponse) -> dict[str, Any]:
    """Flatten a Jev response into plain values keyed by question id."""
    answers: dict[str, Any] = {}
    for qid in NOUL_IDS:
        answers[qid] = response.answers[qid].noul
    for qid in SCORE_IDS:
        answer = response.answers[qid]
        answers[qid] = answer.score
        answers[f"{qid}__confidence"] = answer.confidence
    for qid in CHOICE_IDS:
        answer = response.answers[qid]
        answers[qid] = {
            "choice": answer.choice,
            "confidence": answer.confidence,
            "probabilities": dict(answer.probabilities),
        }
    return answers


async def _classify_one(
    *,
    client: AsyncTypeSafeClient,
    record: Classification,
    thread: ArticleThread,
    article_body: str,
    parent_text: dict[str, str],
    model: str | None,
    semaphore: asyncio.Semaphore,
    usage: Counter[str],
    resolved: set[str],
) -> None:
    """Send one comment's battery and store the answers on ``record``."""
    state = build_state(
        headline=thread.article.headline,
        standfirst=thread.article.standfirst,
        body=article_body,
        comment_text=record.comment.text,
        parent_text=parent_text.get(record.comment.uuid),
    )
    async with semaphore:
        try:
            response = await client.system_one(
                state=state,
                questions=BATTERY,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001 - one bad comment must not kill the run
            record.error = f"{type(exc).__name__}: {exc}"
            logger.warning("Jev failed for %s: %s", record.comment.uuid, record.error)
            return
    record.answers = _read_answers(response)
    # The model that actually answered, which is what the report must name when the
    # request left the version to the API.
    resolved.add(response.model)
    usage["input_tokens"] += response.usage.input_tokens or 0
    usage["output_tokens"] += response.usage.output_tokens or 0
    usage["requests"] += 1


# ----------------------------------------------------------------- verdict and ranking


def apply_thresholds(record: Classification) -> None:
    """Set ``excluded_reason`` and ``flags`` from the Jev answers."""
    if not record.classified:
        return

    for qid, limit in EXCLUDE_AT.items():
        value = record.answers.get(qid, 0.0)
        if isinstance(value, (int, float)) and value >= limit:
            record.excluded_reason = f"{qid} {value:.2f} (excludes at {limit})"
            return

    on_topic = record.noul("on_topic")
    if on_topic < ON_TOPIC_EXCLUDE_BELOW:
        record.excluded_reason = (
            f"off topic {on_topic:.2f} (excludes below {ON_TOPIC_EXCLUDE_BELOW})"
        )
        return

    for qid, limit in FLAG_AT.items():
        value = record.answers.get(qid, 0.0)
        if isinstance(value, (int, float)) and value >= limit:
            record.flags.append(f"{qid} {value:.2f}")
    if on_topic < ON_TOPIC_FLAG_BELOW:
        record.flags.append(f"on_topic {on_topic:.2f}")


def stance_shares(records: list[Classification]) -> dict[str, float]:
    """The share of each stance across everything Jev classified.

    This is what makes representativeness possible: it cannot be judged from a single
    comment, only from the thread around it.
    """
    counts = Counter(
        r.stance for r in records if r.classified and r.excluded_reason is None
    )
    total = sum(counts.values())
    if not total:
        return {}
    return {stance: count / total for stance, count in counts.items()}


def quality_score(record: Classification, shares: dict[str, float]) -> float:
    """Combine the answers into one number, with the weights above.

    Each part is on 0–1 before weighting, so the weights mean what they say.
    """
    experience = record.normalised("personal_experience") * record.noul(
        "experience_relevant",
    )
    contribution = max(
        record.noul("proposes_solution"),
        record.noul("reasoned_argument"),
    )
    representativeness = shares.get(record.stance, 0.0)
    return (
        WEIGHTS["experience"] * experience
        + WEIGHTS["tone"] * record.normalised("tone")
        + WEIGHTS["readability"] * record.normalised("readability")
        + WEIGHTS["contribution"] * contribution
        + WEIGHTS["standalone"] * record.noul("standalone")
        + WEIGHTS["representativeness"] * representativeness
    )


# ------------------------------------------------------------------------ entry point


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Everything the report needs."""

    records: list[Classification]
    shares: dict[str, float]
    usage: dict[str, int]
    model: str
    """The model version that answered, read back from the responses, not requested."""

    @property
    def shortlist(self) -> list[Classification]:
        """Classified, not excluded, best first."""
        keep = [r for r in self.records if r.classified and r.excluded_reason is None]
        return sorted(keep, key=lambda r: r.quality_score, reverse=True)

    @property
    def excluded(self) -> list[Classification]:
        return [r for r in self.records if r.excluded_reason is not None]

    @property
    def flagged(self) -> list[Classification]:
        return [r for r in self.records if r.excluded_reason is None and r.flags]

    @property
    def estimated_cost_usd(self) -> float:
        """Jev bills input tokens only, at $0.042 per million."""
        return self.usage.get("input_tokens", 0) * 0.042 / 1_000_000


async def classify_thread(
    *,
    client: AsyncTypeSafeClient,
    thread: ArticleThread,
    model: str | None,
    article_max_words: int,
    concurrency: int,
) -> ClassificationResult:
    """Run the whole classification stage over one article's comments."""
    article_body = thread.article.capped_body(article_max_words)
    parent_text = thread.parent_text

    records = [
        Classification(comment=c, signals=compute_signals(c.text))
        for c in thread.comments
    ]
    for record in records:
        reason = hard_exclusion(record.comment, record.signals)
        if reason:
            record.excluded_reason = reason

    to_classify = [r for r in records if r.excluded_reason is None]
    logger.info(
        "Classifying %d of %d comments (%d skipped by code-side rules)",
        len(to_classify),
        len(records),
        len(records) - len(to_classify),
    )

    usage: Counter[str] = Counter()
    # More than one entry means the alias advanced mid-run; the report then names both.
    resolved: set[str] = set()
    semaphore = asyncio.Semaphore(concurrency)
    await asyncio.gather(
        *(
            _classify_one(
                client=client,
                record=record,
                thread=thread,
                article_body=article_body,
                parent_text=parent_text,
                model=model,
                semaphore=semaphore,
                usage=usage,
                resolved=resolved,
            )
            for record in to_classify
        ),
    )

    for record in to_classify:
        apply_thresholds(record)

    shares = stance_shares(records)
    for record in records:
        if record.classified:
            record.quality_score = quality_score(record, shares)

    return ClassificationResult(
        records=records,
        shares=shares,
        usage=dict(usage),
        model=", ".join(sorted(resolved)) or "no successful requests",
    )
