"""Orchestrator: fetch an article and its comments, classify them with Jev, write a report.

Run from anywhere in the project (`clients` and `processing` are installed into the
venv by `uv sync`, so no PYTHONPATH is needed):

    uv run python src/processing/workflow.py
    uv run python src/processing/workflow.py <article URL>
    uv run python src/processing/workflow.py <page id or container UUID>

Settings and the three long-lived clients are created here and passed down, so nothing in
`fetch.py` or `classification.py` reaches for a global. Moving this behind FastAPI means
building them in a lifespan handler instead; the stages below do not change.

Thresholds, weights, limits and paths are not in this file: they live in `config.toml`,
read once by `load_settings()`. What is left below is the handful of knobs that belong to
*this* command-line harness — which article to run, and how much of its thread to pull.

The Markdown report is a temporary way to inspect the pipeline while the question wording
and weights are tuned. When the output becomes JSON for an API, the rendering half of this
module is deleted rather than refactored.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from clients.jev import JevClient, JevLimits
from clients.viafoura import SortOrder, ViafouraClient
from processing.classification import (
    Classification,
    ClassificationResult,
    battery_fingerprint,
    classify_thread,
)
from processing.config import ApiConfig, ClassificationConfig
from processing.fetch import ArticleThread, fetch_thread
from processing.log_config import logger
from processing.settings import Settings, load_settings
from processing.store import (
    ThreadStore,
    load_store,
    locked_async,
    safe_key,
    save_store,
    sweep_temp_files,
)

# --------------------------------------------------------------------------- run config
#
# Only what belongs to this command-line harness. Everything that is policy — thresholds,
# weights, limits, the model, the paths — is in `config.toml` and arrives as `Settings`.

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
# How Viafoura orders that top N. `num_likes_desc` and `num_replies_desc` are the
# useful ones here; `newest` and `oldest` take the first or last N instead.
SORTED_BY: SortOrder = "num_likes_desc"


# ------------------------------------------------------------------------- the pipeline


async def run(
    article_ref: str,
    settings: Settings,
) -> tuple[ArticleThread, ClassificationResult, ThreadStore]:
    """Fetch and classify. Owns every client and the store for the duration of the run."""
    async with (
        aiohttp.ClientSession() as session,
        JevClient(
            settings.typesafe_api_key,
            model=settings.jev.model,
            limits=JevLimits(
                requests_per_minute=settings.jev.requests_per_minute,
                tokens_per_second=settings.jev.tokens_per_second,
            ),
            concurrency=settings.jev.concurrency,
            timeout=settings.jev.timeout_seconds,
        ) as jev,
    ):
        # Viafoura shares the session rather than opening one of its own: the public
        # API needs no credential and no handshake, so there is nothing for it to own.
        vf = ViafouraClient(session, settings.viafoura)
        thread = await fetch_thread(
            vf=vf,
            session=session,
            settings=settings,
            article_ref=article_ref,
            limit=TOP_N_COMMENTS,
            sorted_by=SORTED_BY,
        )
        # The lock makes the read-modify-write around the store safe: without it two
        # runs on one article both pay for the same comments and one set of answers is
        # lost. The fingerprint stops a tuning change reusing answers computed against
        # the old questions.
        state_dir = settings.paths.state_dir
        async with locked_async(thread.container_uuid, state_dir):
            await asyncio.to_thread(sweep_temp_files, state_dir)
            store = await asyncio.to_thread(
                load_store,
                thread.container_uuid,
                state_dir,
                battery_fingerprint(settings.article.max_words),
            )
            # So a person opening state/<uuid>.json can tell what it is.
            store.describe(url=thread.article.url, headline=thread.article.headline)
            try:
                result = await classify_thread(
                    client=jev,
                    thread=thread,
                    config=settings.classification,
                    article_max_words=settings.article.max_words,
                    store=store,
                )
            finally:
                # In `finally` because this is the one moment the store matters: a
                # Ctrl-C or an error partway through a long run would otherwise discard
                # every answer already paid for. `classify_thread` records each answer
                # as it arrives, so whatever was bought before the interruption is kept.
                store.note_run()
                await asyncio.to_thread(save_store, store, state_dir)
    return thread, result, store


# ----------------------------------------------------------------------------- the report


def _run_history(store: ThreadStore | None) -> str:
    """How many times this article has been through the pipeline, for the header."""
    if store is None or store.runs <= 1:
        return ""
    first = store.first_run_at[:16].replace("T", " ")
    return f" · run {store.runs}, first seen {first} UTC"


def _proposed(
    shortlist: list[Classification],
    api: ApiConfig,
) -> tuple[list[Classification], int]:
    """The comments to put in front of an editor, and how many cleared the bar.

    Comments an editor has already pinned or picked are dropped here rather than
    excluded earlier, so they still appear in the calibration section below.

    Score is the cut; the two bounds only stop it degenerating. A thread where nothing
    clears the bar still returns its best, so the report says "here is the best of a poor
    thread" rather than nothing at all — the count in the heading is what tells them
    which situation they are in.
    """
    fresh = [r for r in shortlist if not r.already_actioned]
    cleared = [r for r in fresh if r.quality_score >= api.default_min_score]
    chosen = cleared if len(cleared) >= api.min_results else fresh[: api.min_results]
    return chosen[: api.max_results], len(cleared)


def _selection() -> str:
    """How the fetched comments were chosen, for the report's summary table."""
    if TOP_N_COMMENTS is None:
        return "whole thread, replies included"
    return f"top {TOP_N_COMMENTS} by {SORTED_BY}"


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


def _score_table(
    record: Classification,
    shares: dict[str, float],
    weights: Mapping[str, float],
) -> list[str]:
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
            weights["experience"],
            f"{record.score('personal_experience'):.1f}/3 "
            f"x relevant {record.noul('experience_relevant'):.2f}",
        ),
        (
            "tone",
            record.normalised("tone"),
            weights["tone"],
            f"{record.score('tone'):.1f}/2",
        ),
        (
            "contribution",
            max(record.noul("proposes_solution"), record.noul("reasoned_argument")),
            weights["contribution"],
            f"reasoned {record.noul('reasoned_argument'):.2f} / "
            f"solution {record.noul('proposes_solution'):.2f}",
        ),
        (
            "readability",
            record.normalised("readability"),
            weights["readability"],
            f"{record.score('readability'):.1f}/2",
        ),
        (
            "standalone",
            record.noul("standalone"),
            weights["standalone"],
            "reads on its own",
        ),
        (
            "representativeness",
            stance_share,
            weights["representativeness"],
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


def _gate_line(record: Classification, config: ClassificationConfig) -> str:
    """The questions that could have excluded this comment, and how close they came.

    Together with `_score_table` this accounts for all fourteen questions in the battery:
    eight feed the score, six are gates.
    """
    excludes = config.exclude_at
    gates = [
        ("on-topic", record.noul("on_topic"), f"min {config.on_topic_exclude_below}"),
        ("sarcasm", record.noul("sarcasm"), f"max {excludes['sarcasm']}"),
        (
            "attack",
            record.noul("personal_attack"),
            f"max {excludes['personal_attack']}",
        ),
        (
            "hostility",
            record.noul("group_hostility"),
            f"max {excludes['group_hostility']}",
        ),
        (
            "profanity",
            record.noul("profanity_or_threat"),
            f"max {excludes['profanity_or_threat']}",
        ),
        (
            "unverified claims",
            record.score("unverified_claim"),
            f"max {excludes['unverified_claim']}/2",
        ),
    ]
    return " · ".join(f"{name} {value:.2f} ({limit})" for name, value, limit in gates)


def render_report(
    thread: ArticleThread,
    result: ClassificationResult,
    *,
    settings: Settings,
    started: datetime,
    store: ThreadStore | None = None,
) -> str:
    """Build the whole Markdown report."""
    config = settings.classification
    api = settings.api
    min_score = api.default_min_score
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
        f"| Run | {started:%Y-%m-%d %H:%M} UTC{_run_history(store)} |",
        f"| Model | `{result.model}` |",
        f"| Article body sent | {min(article.body_word_count, settings.article.max_words)} of {article.body_word_count} words |",
        f"| Comments fetched | {len(records)} ({_selection()}) |",
        f"| Skipped before Jev | {len(skipped)} |",
        f"| Classified | {sum(r.classified for r in records)} "
        f"({result.newly_classified} new this run, {result.reused} reused) |",
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

    summary = ", ".join(f"{k} {v:.0%}" for k, v in config.weights.items())
    lines += [f"Score weights: {summary}.", ""]

    # ---- shortlist
    proposed, cleared = _proposed(shortlist, api)
    if cleared >= len(proposed):
        heading = f"## Proposed ({len(proposed)} scoring {min_score:.2f} or above)"
    else:
        heading = (
            f"## Proposed ({len(proposed)}: only {cleared} scored {min_score:.2f} "
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
        below = "" if record.quality_score >= min_score else f" — below {min_score:.2f}"
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
        lines += ["", *_score_table(record, result.shares, config.weights), ""]
        lines += [f"**Gates passed:** {_gate_line(record, config)}", ""]
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
            elif record.quality_score >= min_score:
                verdict = "would propose"
            else:
                verdict = f"below {min_score:.2f}"
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

    thresholds = ", ".join(f"{k} ≥ {v}" for k, v in config.exclude_at.items())
    lines += [
        "---",
        "",
        f"Exclusion thresholds: {thresholds}, "
        f"on_topic < {config.on_topic_exclude_below}. "
        "Edit them in `config.toml`; edit the question wording in "
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

    thread, result, store = await run(article_ref, settings)

    report = render_report(
        thread,
        result,
        settings=settings,
        started=started,
        store=store,
    )
    # One report per article, rewritten in place. A timestamped name per run left a pile
    # of near-identical files and no obvious current one; the run history is in the
    # report itself instead.
    # Validated rather than trusted: this becomes a filename, and `_write_report`
    # creates parent directories. Safe today only because `store_path` happens to
    # validate the same value first — which is an accident, not a guarantee.
    name = f"classification_{safe_key(thread.container_uuid)}.md"
    # A small synchronous write, off the event loop, after every request has finished.
    path = await asyncio.to_thread(
        _write_report,
        report,
        settings.paths.output_dir / name,
    )

    shortlist = result.shortlist
    logger.info(
        "%d comments classified (%d new, %d reused from the store): %d shortlisted, "
        "%d flagged, %d excluded. %d input tokens, about $%.4f.",
        sum(r.classified for r in result.records),
        result.newly_classified,
        result.reused,
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
