"""Classification stage: deterministic signals, the Jev calls, exclusions and ranking.

The division of labour is deliberate. Anything code can compute exactly — counting words,
measuring capitals, spotting a URL — stays in code, because Jev's own documentation says
it does not count reliably. Jev is asked only for judgments a knowledgeable person makes
in a second.

Every threshold and weight arrives as a `ClassificationConfig`, read from
`config.toml` at startup and passed in. Nothing here reaches for a global, so the same
functions serve a CLI run, an HTTP request and a test holding a policy of its own, and
changing policy is a number under review rather than a reworded question.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import msgspec
from typesafe_sdk import SystemOneResponse

from clients.jev import JevClient, Spend
from clients.viafoura import Comment
from processing.config import ClassificationConfig
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
from processing.store import ThreadStore

# ------------------------------------------------------------------- code-side policy

# Which domains a link may point at. This one stays in code rather than in `config.toml`:
# it is the boundary the brief draws around content we can vouch for, not a number to
# tune, and widening it is a decision that belongs in a reviewed diff.
TELEGRAPH_DOMAINS = ("telegraph.co.uk",)

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_WORD_RE = re.compile(r"\S+")

# Everything else — the word floor, the caps ceiling, the sweet spot, the exclusion and
# flag thresholds and the score weights — arrives as a `ClassificationConfig`. See
# `processing/config.py` for the shape and `config.toml` for the values in force.


# ----------------------------------------------------------------------- data classes


@dataclass(frozen=True, slots=True)
class CodeSignals:
    """What code can measure exactly, with no model involved."""

    word_count: int
    caps_ratio: float
    has_external_url: bool
    has_paragraph_break: bool

    def in_sweet_spot(self, config: ClassificationConfig) -> bool:
        """Whether the length is the brief's preferred one. Reported, never enforced."""
        low, high = config.sweet_spot
        return low <= self.word_count <= high


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
    model: str = ""
    """The model version that answered this comment, not the run's cumulative set."""

    @property
    def classified(self) -> bool:
        """Whether Jev actually saw this comment."""
        return bool(self.answers)

    @property
    def pin_eligible(self) -> bool:
        """Replies cannot be pinned; they can still go in a carousel."""
        return not self.comment.is_reply

    @property
    def already_actioned(self) -> bool:
        """An editor has already pinned or picked this one.

        Proposing it again wastes their time, so it is kept out of the proposals. It is
        still classified: a comment an editor chose by hand is the closest thing to
        ground truth this pipeline has, and its score is the cheapest available check on
        whether the questions agree with the people they are meant to imitate.

        `is_top_comment` is deliberately not included. That flag is Viafoura's own
        algorithmic pick — the tool the brief reports as having promoted sarcasm — not a
        human decision, so it says nothing about quality.
        """
        return self.comment.is_pinned or self.comment.is_picked

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


def hard_exclusion(
    comment: Comment,
    signals: CodeSignals,
    config: ClassificationConfig,
) -> str | None:
    """A reason to skip Jev entirely, or None to go ahead.

    Applied before classification, so a four-word comment never costs an API call.
    """
    if comment.state and comment.state != "visible":
        return f"not visible (state: {comment.state})"
    if signals.word_count < config.min_words:
        return f"too short ({signals.word_count} words, minimum {config.min_words})"
    if signals.has_external_url:
        return "contains a link off telegraph.co.uk"
    if signals.caps_ratio > config.max_caps_ratio:
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
    client: JevClient,
    record: Classification,
    thread: ArticleThread,
    article_body: str,
    parent_text: dict[str, str],
    store: ThreadStore | None,
) -> None:
    """Send one comment's battery and store the answers on ``record``.

    Pacing, retries, concurrency and usage accounting all belong to `JevClient`; what is
    left here is the part that is about comments.
    """
    state = build_state(
        headline=thread.article.headline,
        standfirst=thread.article.standfirst,
        body=article_body,
        comment_text=record.comment.text,
        parent_text=parent_text.get(record.comment.uuid),
    )
    try:
        response = await client.ask(state, BATTERY)
        # Parsing is inside the try on purpose. A response missing a question id raises
        # KeyError here, and outside the try that escaped `asyncio.gather` and aborted
        # the whole run — discarding every answer already bought. One malformed
        # response must cost one comment, exactly like a failed call.
        answers = _read_answers(response)
    except Exception as exc:  # noqa: BLE001 - one bad comment must not kill the run
        record.error = f"{type(exc).__name__}: {exc}"
        logger.warning("Jev failed for %s: %s", record.comment.uuid, record.error)
        return
    record.answers = answers
    # The version that answered *this* comment. Taking it from the client's cumulative
    # set would label every later comment with every version the client had ever seen.
    record.model = response.model
    # Remembered here rather than in a loop after the gather: an answer is paid for the
    # moment it arrives, so it must be recorded the moment it arrives. Batching this at
    # the end meant a Ctrl-C partway through discarded everything bought so far.
    if store is not None:
        store.remember(
            record.comment.uuid,
            answers,
            response.model,
            text=record.comment.text,
        )


# ----------------------------------------------------------------- verdict and ranking


def apply_thresholds(record: Classification, config: ClassificationConfig) -> None:
    """Set ``excluded_reason`` and ``flags`` from the Jev answers."""
    if not record.classified:
        return

    for qid, limit in config.exclude_at.items():
        value = record.answers.get(qid, 0.0)
        if isinstance(value, (int, float)) and value >= limit:
            record.excluded_reason = f"{qid} {value:.2f} (excludes at {limit})"
            return

    on_topic = record.noul("on_topic")
    if on_topic < config.on_topic_exclude_below:
        record.excluded_reason = (
            f"off topic {on_topic:.2f} (excludes below {config.on_topic_exclude_below})"
        )
        return

    for qid, limit in config.flag_at.items():
        value = record.answers.get(qid, 0.0)
        if isinstance(value, (int, float)) and value >= limit:
            record.flags.append(f"{qid} {value:.2f}")
    if on_topic < config.on_topic_flag_below:
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


def quality_score(
    record: Classification,
    shares: dict[str, float],
    config: ClassificationConfig,
) -> float:
    """Combine the answers into one number, with the configured weights.

    Each part is on 0–1 before weighting, so the weights mean what they say.
    """
    weights = config.weights
    experience = record.normalised("personal_experience") * record.noul(
        "experience_relevant",
    )
    contribution = max(
        record.noul("proposes_solution"),
        record.noul("reasoned_argument"),
    )
    representativeness = shares.get(record.stance, 0.0)
    return (
        weights["experience"] * experience
        + weights["tone"] * record.normalised("tone")
        + weights["readability"] * record.normalised("readability")
        + weights["contribution"] * contribution
        + weights["standalone"] * record.noul("standalone")
        + weights["representativeness"] * representativeness
    )


# ------------------------------------------------------------------- cache fingerprint


def battery_fingerprint(article_max_words: int) -> str:
    """Identifies the inputs a stored answer was computed against.

    A comment's *text* is immutable, which is why answers can be cached at all. The rest
    of the state is not: retune a question, rename one, add one, or change how much
    article body is sent, and a stored answer no longer means what a fresh one means.

    Reusing it then is worse than wrong. `Classification.noul()` and `score()` return
    0.0 for a missing key, so a renamed question would quietly drag every cached comment
    down the ranking with no error and nothing in the report to show it.

    So the store records this fingerprint and discards everything when it changes. The
    cost is re-billing a thread after a tuning change, which is the correct price: the
    alternative is a report that is subtly wrong and says nothing about it.
    """
    material = msgspec.json.encode(
        {
            "questions": sorted(BATTERY),
            # The SDK's question types are pydantic models (since typesafe-sdk 0.7),
            # which msgspec cannot encode directly.
            "battery": {
                qid: question.model_dump(mode="json")
                for qid, question in sorted(BATTERY.items())
            },
            "article_max_words": article_max_words,
        },
    )
    return hashlib.sha256(material).hexdigest()[:16]


# ------------------------------------------------------------------------ entry point


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Everything the report needs."""

    records: list[Classification]
    shares: dict[str, float]
    spend: Spend
    """What this run cost and which model version answered, from the client."""
    newly_classified: int = 0
    """Comments sent to Jev this run."""
    reused: int = 0
    """Comments whose answers came from the store, costing nothing."""
    restored_models: frozenset[str] = frozenset()
    """Model versions recorded against the answers restored from the store.

    Without this a run that reused everything would report no model at all, because
    `Spend` only knows about requests this run actually made.
    """

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
        return self.spend.estimated_cost_usd

    @property
    def model(self) -> str:
        """Every model version behind this report, whenever it answered.

        Answers restored from the store were bought on an earlier run, so the versions
        that produced them belong here too — otherwise a fully cached run would claim no
        model at all, and a run spanning a version change would name only the new one.
        """
        seen = self.spend.models | self.restored_models
        return ", ".join(sorted(seen)) or "no successful requests"


async def classify_thread(
    *,
    client: JevClient,
    thread: ArticleThread,
    config: ClassificationConfig,
    article_max_words: int,
    store: ThreadStore | None = None,
) -> ClassificationResult:
    """Run the whole classification stage over one article's comments.

    The client decides how fast the requests go out and how many are in flight, so this
    fans out with a plain `gather` and measures what the stretch cost afterwards. That is
    what lets one client serve several articles without their costs running together.
    """
    article_body = thread.article.capped_body(article_max_words)
    parent_text = thread.parent_text

    records = [
        Classification(comment=c, signals=compute_signals(c.text))
        for c in thread.comments
    ]
    for record in records:
        reason = hard_exclusion(record.comment, record.signals, config)
        if reason:
            record.excluded_reason = reason

    eligible = [r for r in records if r.excluded_reason is None]

    # Answers already held are restored rather than bought again. This is the whole
    # saving: a comment's answers cannot change, so a second run pays only for what is
    # genuinely new.
    reused = 0
    restored_models: set[str] = set()
    to_classify: list[Classification] = []
    for record in eligible:
        remembered = store.get(record.comment.uuid) if store else None
        if remembered is not None:
            record.answers = dict(remembered.answers)
            # Set on the record too, not just collected for the run-level total: a
            # restored comment knows which version answered it, and leaving the field
            # empty is a trap for whatever reads it next.
            record.model = remembered.model
            restored_models.add(remembered.model)
            # Entries written before the text was stored get it now, at no cost.
            store.fill_text(record.comment.uuid, record.comment.text)
            reused += 1
        else:
            to_classify.append(record)

    logger.info(
        "%d comments: %d skipped by code-side rules, %d already classified, "
        "%d to send to Jev",
        len(records),
        len(records) - len(eligible),
        reused,
        len(to_classify),
    )

    before = client.spent()
    await asyncio.gather(
        *(
            _classify_one(
                client=client,
                record=record,
                thread=thread,
                article_body=article_body,
                parent_text=parent_text,
                store=store,
            )
            for record in to_classify
        ),
    )

    # Thresholds are re-derived every run, including for restored answers, so an edit
    # to `[classification]` in config.toml takes effect on the whole thread without
    # re-billing a single comment. Only `[article] max_words` costs money to change.
    for record in eligible:
        apply_thresholds(record, config)

    shares = stance_shares(records)
    for record in records:
        if record.classified:
            record.quality_score = quality_score(record, shares, config)

    return ClassificationResult(
        records=records,
        shares=shares,
        spend=client.since(before),
        newly_classified=len(to_classify),
        reused=reused,
        restored_models=frozenset(restored_models),
    )
