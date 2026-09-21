"""Orchestrator: fetch an article and its comments, classify them with Jev, write a report.

Run from anywhere in the project (`clients` and `processing` are installed into the
venv by `uv sync`, so no PYTHONPATH is needed):

    uv run python src/processing/workflow.py
    uv run python src/processing/workflow.py <article URL>
    uv run python src/processing/workflow.py <page id or container UUID>

Settings and the three long-lived clients are created here and passed down, so nothing in
`fetch.py` or `classification.py` reaches for a global. Moving this behind FastAPI means
building them in a lifespan handler instead; the stages below do not change.

The Markdown report is a temporary way to inspect the pipeline while the question wording
and weights are tuned. When the output becomes JSON for an API, the rendering half of this
module is deleted rather than refactored.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from clients.jev import JevClient
from clients.vf_mcp import RankedBy, ViafouraMCPClient
from processing.classification import (
    EXCLUDE_AT,
    ON_TOPIC_EXCLUDE_BELOW,
    WEIGHTS,
    Classification,
    ClassificationResult,
    classify_thread,
)
from processing.fetch import ArticleThread, fetch_thread
from processing.log_config import logger
from processing.settings import Settings, load_settings

# --------------------------------------------------------------------------- run config

# A URL, a Telegraph page id (e.g. A65xRHy7KY6g), or a Viafoura container UUID.
ARTICLE = "https://www.telegraph.co.uk/news/2026/09/21/calais-charity-migrants-posing-children-claim-asylum/"

# How many comments to pull before classifying. A number keeps a tuning run cheap and
# fast; `None` pulls the article's whole thread.
#
# The two are not just different counts. A number asks Viafoura for the top N *top-level*
# comments by `RANKED_BY`; `None` pages the entire thread and includes replies, which are
# then classified like anything else (`standalone` and the parent text in the state exist
# for that case). So `None` both widens the population and changes its composition —
# expect more comments than the article's headline count suggests, and a lower mean
# quality, since ranking no longer does any filtering first.
TOP_N_COMMENTS: int | None = None
# How Viafoura picks that top N: most_liked, most_replied or trending.
RANKED_BY: RankedBy = "most_liked"
# Words of article body sent with every comment. The article dominates token cost, and
# Jev loses accuracy as the state fills with material a question does not need.
ARTICLE_MAX_WORDS = 600
# Jev requests in flight. This bounds open sockets, not the request rate — the rate is
# the client's job (`clients/jev.py`), which is why this can now sit well above 10. At
# the 18 req/s ceiling, 64 in flight keeps the rate limiter, not this, as the bottleneck
# for any mean latency up to ~3.5s.
CONCURRENCY = 64
# Per-request HTTP timeout, seconds. The SDK default is 10.
REQUEST_TIMEOUT = 30.0
# --- what gets proposed ---------------------------------------------------------------
#
# The brief asks for "more than enough for a carousel/pinning, but not so many that
# sifting these comments becomes a burden in itself", so the cut is by score, bounded at
# both ends.
#
# Only propose comments scoring at least this. The one threshold we have real evidence
# for is thin: on the trial thread the comment the team had actually pinned scored 0.946
# and the runner-up 0.454, so a bar at 0.70 would have returned a single comment — not a
# carousel. 0.50 is a starting guess, to be set properly against the pinned-vs-approved
# spreadsheet. Raise it as the questions sharpen.
MIN_SCORE = 0.50
# Never propose more than this, however many clear the bar. Stops a 1,000-comment
# liveblog thread from producing a report nobody will read.
MAX_PROPOSED = 25
# If fewer than this clear the bar, show the best anyway, marked as below it. A weak
# thread should still give an editor something to judge rather than an empty report.
MIN_PROPOSED = 5
# Unpinned: `None` falls through to the SDK client default, `jev-latest`, so model
# improvements arrive without a code change. The version that actually answered is read
# back off each response and named in the report, so a run stays identifiable after the
# fact. Set this to a version string (e.g. "jev-1.13.0") to freeze it — thresholds
# calibrated against one version can shift when the alias advances.
MODEL: str | None = None
# Where the report goes. Anchored to the project root, not the working directory, so
# the path is the same whether the script is run from the root or from this folder.
OUTPUT_DIR = Path(__file__).resolve().parents[2] / "output"


# ------------------------------------------------------------------------- the pipeline


async def run(
    article_ref: str,
    settings: Settings,
) -> tuple[ArticleThread, ClassificationResult]:
    """Fetch and classify. Owns every client for the duration of the run."""
    async with (
        aiohttp.ClientSession() as session,
        ViafouraMCPClient(settings.viafoura_api_key) as vf,
        JevClient(
            settings.typesafe_api_key,
            model=MODEL,
            concurrency=CONCURRENCY,
            timeout=REQUEST_TIMEOUT,
        ) as jev,
    ):
        thread = await fetch_thread(
            vf=vf,
            session=session,
            settings=settings,
            article_ref=article_ref,
            limit=TOP_N_COMMENTS,
            ranked_by=RANKED_BY,
        )
        result = await classify_thread(
            client=jev,
            thread=thread,
            article_max_words=ARTICLE_MAX_WORDS,
        )
    return thread, result


# ----------------------------------------------------------------------------- the report


def _proposed(shortlist: list[Classification]) -> tuple[list[Classification], int]:
    """The comments to put in front of an editor, and how many cleared `MIN_SCORE`.

    Comments an editor has already pinned or picked are dropped here rather than
    excluded earlier, so they still appear in the calibration section below.

    Score is the cut; the two bounds only stop it degenerating. A thread where nothing
    clears the bar still returns its best, so the report says "here is the best of a poor
    thread" rather than nothing at all — the count in the heading is what tells them
    which situation they are in.
    """
    fresh = [r for r in shortlist if not r.already_actioned]
    cleared = [r for r in fresh if r.quality_score >= MIN_SCORE]
    chosen = cleared if len(cleared) >= MIN_PROPOSED else fresh[:MIN_PROPOSED]
    return chosen[:MAX_PROPOSED], len(cleared)


def _selection() -> str:
    """How the fetched comments were chosen, for the report's summary table."""
    if TOP_N_COMMENTS is None:
        return "whole thread, replies included"
    return f"top {TOP_N_COMMENTS} by {RANKED_BY}"


def _one_line(text: str, width: int = 160) -> str:
    """Collapse a comment to a single line for a table or a summary."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= width else collapsed[: width - 1] + "…"


def _signal_line(record: Classification) -> str:
    """The Jev numbers behind one comment's score, compactly.

    Used where a table would drown the page: the flagged and excluded tables.
    """
    parts = [
        f"experience {record.score('personal_experience'):.2f}/3",
        f"relevant {record.noul('experience_relevant'):.2f}",
        f"tone {record.score('tone'):.2f}/2",
        f"readability {record.score('readability'):.2f}/2",
        f"solution {record.noul('proposes_solution'):.2f}",
        f"reasoned {record.noul('reasoned_argument'):.2f}",
        f"standalone {record.noul('standalone'):.2f}",
        f"on-topic {record.noul('on_topic'):.2f}",
        f"sarcasm {record.noul('sarcasm'):.2f}",
        f"claims {record.score('unverified_claim'):.2f}/2",
    ]
    return " · ".join(parts)


def _score_table(record: Classification, shares: dict[str, float]) -> list[str]:
    """Break one comment's score into what each answer contributed.

    The point is auditability: a flat list of ten numbers cannot tell you whether a
    comment ranks where it does because of its personal experience or because its stance
    happens to be the thread's majority. Rows are sorted by contribution, so the first
    line is always the reason.
    """
    stance_share = shares.get(record.stance, 0.0)
    rows = [
        (
            "experience",
            record.normalised("personal_experience")
            * record.noul("experience_relevant"),
            WEIGHTS["experience"],
            f"{record.score('personal_experience'):.1f}/3 "
            f"x relevant {record.noul('experience_relevant'):.2f}",
        ),
        (
            "tone",
            record.normalised("tone"),
            WEIGHTS["tone"],
            f"{record.score('tone'):.1f}/2",
        ),
        (
            "contribution",
            max(record.noul("proposes_solution"), record.noul("reasoned_argument")),
            WEIGHTS["contribution"],
            f"reasoned {record.noul('reasoned_argument'):.2f} / "
            f"solution {record.noul('proposes_solution'):.2f}",
        ),
        (
            "readability",
            record.normalised("readability"),
            WEIGHTS["readability"],
            f"{record.score('readability'):.1f}/2",
        ),
        (
            "standalone",
            record.noul("standalone"),
            WEIGHTS["standalone"],
            "reads on its own",
        ),
        (
            "representativeness",
            stance_share,
            WEIGHTS["representativeness"],
            f'"{record.stance}" = {stance_share:.0%} of thread',
        ),
    ]
    rows.sort(key=lambda row: row[1] * row[2], reverse=True)
    lines = [
        "| Adds | Signal | Jev's answer | 0–1 | x weight |",
        "| ---: | --- | --- | ---: | ---: |",
    ]
    lines += [
        f"| {value * weight:.3f} | {name} | {detail} | {value:.2f} | {weight:.2f} |"
        for name, value, weight, detail in rows
    ]
    lines.append(f"| **{record.quality_score:.3f}** | **total** | | | |")
    return lines


def _gate_line(record: Classification) -> str:
    """The questions that could have excluded this comment, and how close they came.

    Together with `_score_table` this accounts for all fourteen questions in the battery:
    eight feed the score, six are gates.
    """
    gates = [
        ("on-topic", record.noul("on_topic"), f"min {ON_TOPIC_EXCLUDE_BELOW}"),
        ("sarcasm", record.noul("sarcasm"), f"max {EXCLUDE_AT['sarcasm']}"),
        (
            "attack",
            record.noul("personal_attack"),
            f"max {EXCLUDE_AT['personal_attack']}",
        ),
        (
            "hostility",
            record.noul("group_hostility"),
            f"max {EXCLUDE_AT['group_hostility']}",
        ),
        (
            "profanity",
            record.noul("profanity_or_threat"),
            f"max {EXCLUDE_AT['profanity_or_threat']}",
        ),
        (
            "unverified claims",
            record.score("unverified_claim"),
            f"max {EXCLUDE_AT['unverified_claim']}/2",
        ),
    ]
    return " · ".join(f"{name} {value:.2f} ({limit})" for name, value, limit in gates)


def render_report(
    thread: ArticleThread,
    result: ClassificationResult,
    *,
    started: datetime,
) -> str:
    """Build the whole Markdown report."""
    lines: list[str] = []
    article = thread.article
    records = result.records
    shortlist = result.shortlist
    flagged = result.flagged
    excluded = result.excluded
    skipped = [r for r in excluded if not r.classified]
    failed = [r for r in records if r.error]

    lines += [
        f"# Comment classification — {article.headline}",
        "",
        f"<{article.url}>",
        "",
        "| | |",
        "| --- | --- |",
        f"| Container | `{thread.container_uuid}` |",
        f"| Run | {started:%Y-%m-%d %H:%M} UTC |",
        f"| Model | `{result.model}` |",
        f"| Article body sent | {min(article.body_word_count, ARTICLE_MAX_WORDS)} of {article.body_word_count} words |",
        f"| Comments fetched | {len(records)} ({_selection()}) |",
        f"| Skipped before Jev | {len(skipped)} |",
        f"| Classified | {sum(r.classified for r in records)} |",
        f"| Excluded by Jev | {len(excluded) - len(skipped)} |",
        f"| Flagged for review | {len(flagged)} |",
        f"| Shortlisted | {len(shortlist)} |",
        f"| Input tokens | {result.spend.input_tokens:,} |",
        f"| Estimated cost | ${result.estimated_cost_usd:.4f} |",
    ]
    if failed:
        lines.append(f"| Failed | {len(failed)} |")
    lines.append("")

    # ---- thread summary
    lines += ["## Thread summary", ""]
    if result.shares:
        lines += ["| Stance | Share of classified comments |", "| --- | --- |"]
        for stance, share in sorted(
            result.shares.items(),
            key=lambda kv: kv[1],
            reverse=True,
        ):
            lines.append(f"| {stance} | {share:.0%} |")
        lines += [
            "",
            "Representativeness in the score below is a comment's stance share, so a "
            "comment in the majority camp scores higher. Dissenting comments are ranked "
            "lower but never excluded.",
            "",
        ]
    else:
        lines += [
            "No comments were classified, so there is no stance distribution.",
            "",
        ]

    weights = ", ".join(f"{k} {v:.0%}" for k, v in WEIGHTS.items())
    lines += [f"Score weights: {weights}.", ""]

    # ---- shortlist
    proposed, cleared = _proposed(shortlist)
    if cleared >= len(proposed):
        heading = f"## Proposed ({len(proposed)} scoring {MIN_SCORE:.2f} or above)"
    else:
        heading = (
            f"## Proposed ({len(proposed)}: only {cleared} scored {MIN_SCORE:.2f} "
            f"or above, so the next best are shown too)"
        )
    lines += [heading, ""]
    if not shortlist:
        lines += ["Nothing survived the exclusion rules.", ""]
    for rank, record in enumerate(proposed, start=1):
        comment = record.comment
        eligibility = (
            "pin or carousel" if record.pin_eligible else "carousel only (reply)"
        )
        below = "" if record.quality_score >= MIN_SCORE else f" — below {MIN_SCORE:.2f}"
        lines += [
            f"### {rank}. Score {record.quality_score:.3f}{below} — {eligibility}",
            "",
            f"*{comment.created_at:%d %b %H:%M} · {comment.likes} likes · "
            f"{comment.total_replies} replies · {record.signals.word_count} words · "
            f"stance: {record.stance}*",
            "",
            # Editors act on the comment in Viafoura's own UI, so the uuid is the
            # handle they need; there is no username in the payload to show instead.
            f"`{comment.uuid}`",
            "",
        ]
        lines += ["> " + line for line in _quote(comment.text)]
        lines += ["", *_score_table(record, result.shares), ""]
        lines += [f"**Gates passed:** {_gate_line(record)}", ""]
        if record.flags:
            lines += [f"**Flagged:** {', '.join(record.flags)}", ""]

    # ---- already pinned or picked, as a check on the questions
    # From every record, not just the shortlist: a comment an editor pinned that this
    # pipeline excluded is the most useful row in the report, and it is the one a
    # shortlist-only view would hide.
    actioned = [r for r in records if r.already_actioned]
    if actioned:
        lines += [
            "## Already pinned or picked",
            "",
            "Kept out of the proposals — an editor has acted on these. Shown because "
            "they are the closest thing to ground truth available: if the questions are "
            "working, these should score near the top of the proposals above. A low "
            "score, and especially an **excluded** verdict, is a tuning signal rather "
            "than an editing one: it means this pipeline would have missed a comment a "
            "person chose.",
            "",
            "| Score | Verdict | UUID | Comment |",
            "| --- | --- | --- | --- |",
        ]
        for record in sorted(actioned, key=lambda r: r.quality_score, reverse=True):
            if record.excluded_reason:
                verdict = f"**excluded**: {record.excluded_reason}"
            elif record.quality_score >= MIN_SCORE:
                verdict = "would propose"
            else:
                verdict = f"below {MIN_SCORE:.2f}"
            lines.append(
                f"| {record.quality_score:.3f} | {verdict} | `{record.comment.uuid}` | "
                f"{_cell(record.comment.text)} |",
            )
        lines.append("")

    # ---- flagged
    lines += ["## Flagged for review", ""]
    if not flagged:
        lines += ["Nothing landed in the middle band.", ""]
    else:
        lines += [
            "Kept in the shortlist, but a signal is close to its exclusion threshold. "
            "These are the cases most worth your judgment.",
            "",
            "| Score | Flags | UUID | Comment |",
            "| --- | --- | --- | --- |",
        ]
        for record in sorted(flagged, key=lambda r: r.quality_score, reverse=True):
            flags = ", ".join(record.flags)
            lines.append(
                f"| {record.quality_score:.3f} | {flags} | `{record.comment.uuid}` | "
                f"{_cell(record.comment.text)} |",
            )
        lines.append("")

    # ---- excluded
    lines += ["## Excluded", ""]
    if not excluded:
        lines += ["Nothing was excluded.", ""]
    else:
        lines += [
            "Grouped by reason. Scan for anything you would have kept.",
            "",
            "| Reason | Comment |",
            "| --- | --- |",
        ]
        for record in sorted(excluded, key=lambda r: r.excluded_reason or ""):
            lines.append(
                f"| {record.excluded_reason} | {_cell(record.comment.text)} |",
            )
        lines.append("")

    if failed:
        lines += ["## Failed", "", "| Error | Comment |", "| --- | --- |"]
        lines += [f"| {r.error} | {_cell(r.comment.text)} |" for r in failed]
        lines.append("")

    thresholds = ", ".join(f"{k} ≥ {v}" for k, v in EXCLUDE_AT.items())
    lines += [
        "---",
        "",
        f"Exclusion thresholds: {thresholds}, on_topic < 0.35. "
        "Edit them in `processing/classification.py`; edit the question wording in "
        "`processing/questions.py`.",
        "",
    ]
    return "\n".join(lines)


def _quote(text: str) -> list[str]:
    """Comment text as Markdown blockquote lines, preserving paragraph breaks."""
    paragraphs = [" ".join(p.split()) for p in text.split("\n") if p.strip()]
    out: list[str] = []
    for i, paragraph in enumerate(paragraphs):
        if i:
            out.append("")
        out.append(paragraph)
    return out or [""]


def _cell(text: str) -> str:
    """Comment text safe for a Markdown table cell."""
    return _one_line(text.replace("|", "\\|"), width=120)


def _write_report(report: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    return path


# ------------------------------------------------------------------------- entry point


async def main(article_ref: str) -> None:
    started = datetime.now(UTC)
    settings = load_settings()

    thread, result = await run(article_ref, settings)

    report = render_report(thread, result, started=started)
    name = f"classification_{thread.container_uuid}_{started:%Y%m%d-%H%M}.md"
    # A small synchronous write, off the event loop, after every request has finished.
    path = await asyncio.to_thread(_write_report, report, OUTPUT_DIR / name)

    shortlist = result.shortlist
    logger.info(
        "Classified %d comments: %d shortlisted, %d flagged, %d excluded. "
        "%d input tokens, about $%.4f.",
        sum(r.classified for r in result.records),
        len(shortlist),
        len(result.flagged),
        len(result.excluded),
        result.spend.input_tokens,
        result.estimated_cost_usd,
    )
    print(f"\nReport: {path}")
    if shortlist:
        print(f"Top comment (score {shortlist[0].quality_score:.3f}):")
        print(f"  {_one_line(shortlist[0].comment.text, 140)}")
    print(f"Done in {(datetime.now(UTC) - started).total_seconds():.1f}s")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ARTICLE))
