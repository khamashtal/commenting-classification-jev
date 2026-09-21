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
from typesafe_sdk import AsyncTypeSafeClient

from clients.vf_mcp import RankedBy, ViafouraMCPClient
from processing.classification import (
    EXCLUDE_AT,
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
ARTICLE = "https://www.telegraph.co.uk/health-fitness/conditions/ageing/longevity-lessons-britain-can-learn-from-singapore/"

# How many comments to pull before classifying. Keeps a tuning run cheap and fast.
TOP_N_COMMENTS = 50
# How Viafoura picks that top N: most_liked, most_replied or trending.
RANKED_BY: RankedBy = "most_liked"
# Words of article body sent with every comment. The article dominates token cost, and
# Jev loses accuracy as the state fills with material a question does not need.
ARTICLE_MAX_WORDS = 600
# Concurrent Jev requests. Well inside the 1,200 requests/minute limit.
CONCURRENCY = 10
# How many comments the report ranks in full.
SHORTLIST_N = 15
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
        AsyncTypeSafeClient(api_key=settings.typesafe_api_key) as jev,
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
            model=MODEL,
            article_max_words=ARTICLE_MAX_WORDS,
            concurrency=CONCURRENCY,
        )
    return thread, result


# ----------------------------------------------------------------------------- the report


def _one_line(text: str, width: int = 160) -> str:
    """Collapse a comment to a single line for a table or a summary."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= width else collapsed[: width - 1] + "…"


def _signal_line(record: Classification) -> str:
    """The Jev numbers behind one comment's score, compactly."""
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
        f"| Comments fetched | {len(records)} (top {TOP_N_COMMENTS} by {RANKED_BY}) |",
        f"| Skipped before Jev | {len(skipped)} |",
        f"| Classified | {sum(r.classified for r in records)} |",
        f"| Excluded by Jev | {len(excluded) - len(skipped)} |",
        f"| Flagged for review | {len(flagged)} |",
        f"| Shortlisted | {len(shortlist)} |",
        f"| Input tokens | {result.usage.get('input_tokens', 0):,} |",
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
    lines += [f"## Shortlist (top {min(SHORTLIST_N, len(shortlist))} by score)", ""]
    if not shortlist:
        lines += ["Nothing survived the exclusion rules.", ""]
    for rank, record in enumerate(shortlist[:SHORTLIST_N], start=1):
        comment = record.comment
        eligibility = (
            "pin or carousel" if record.pin_eligible else "carousel only (reply)"
        )
        lines += [
            f"### {rank}. Score {record.quality_score:.3f} — {eligibility}",
            "",
            f"*{comment.created_at:%d %b %H:%M} · {comment.likes} likes · "
            f"{comment.total_replies} replies · {record.signals.word_count} words · "
            f"stance: {record.stance}*",
            "",
        ]
        lines += ["> " + line for line in _quote(comment.text)]
        lines += ["", f"`{_signal_line(record)}`", ""]
        if record.flags:
            lines += [f"**Flagged:** {', '.join(record.flags)}", ""]

    # ---- flagged
    lines += ["## Flagged for review", ""]
    if not flagged:
        lines += ["Nothing landed in the middle band.", ""]
    else:
        lines += [
            "Kept in the shortlist, but a signal is close to its exclusion threshold. "
            "These are the cases most worth your judgment.",
            "",
            "| Score | Flags | Comment |",
            "| --- | --- | --- |",
        ]
        for record in sorted(flagged, key=lambda r: r.quality_score, reverse=True):
            flags = ", ".join(record.flags)
            lines.append(
                f"| {record.quality_score:.3f} | {flags} | {_cell(record.comment.text)} |",
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
        result.usage.get("input_tokens", 0),
        result.estimated_cost_usd,
    )
    print(f"\nReport: {path}")
    if shortlist:
        print(f"Top comment (score {shortlist[0].quality_score:.3f}):")
        print(f"  {_one_line(shortlist[0].comment.text, 140)}")
    print(f"Done in {(datetime.now(UTC) - started).total_seconds():.1f}s")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ARTICLE))
