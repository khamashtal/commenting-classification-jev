"""The report: does it survive hostile comment text, and does it say true things."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import (
    CLASSIFICATION,
    FakeJev,
    make_comment,
    make_settings,
    make_thread,
)

from processing import workflow as w
from processing.classification import classify_thread
from processing.store import ThreadStore

STARTED = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


async def _report(comments, answers_for=None, store=None):  # noqa: ANN001, ANN202
    thread = make_thread(comments)
    result = await classify_thread(
        client=FakeJev(answers_for=answers_for or {}),
        thread=thread,
        config=CLASSIFICATION,
        article_max_words=600,
        store=store,
    )
    report = w.render_report(
        thread,
        result,
        settings=make_settings(),
        started=STARTED,
        store=store,
    )
    return report, result


class TestInjection:
    """Comment text is user-generated and lands in a Markdown table."""

    async def test_table_pipe_cannot_break_a_row(self) -> None:
        # Pinned so the comment reaches a table at all. Without that, the earlier
        # version of this loop inspected zero rows and passed with the escaping removed.
        text = (
            "An otherwise sensible comment | with a pipe | and another | that would "
            "split this row into extra columns if it were not escaped properly."
        )
        report, _ = await _report([make_comment("c-1", text, is_pinned=True)])
        rows = [
            ln for ln in report.splitlines() if ln.startswith("|") and "`c-1`" in ln
        ]
        assert rows, "the comment must reach a table for this test to mean anything"
        for row in rows:
            assert "\\|" in row, f"comment pipes are not escaped: {row}"
            unescaped = row.count("|") - row.count("\\|")
            assert unescaped == 5, f"row split into extra columns: {row}"

    async def test_newlines_do_not_escape_a_table_cell(self) -> None:
        text = (
            "A comment that contains\na newline and then continues for long enough "
            "to pass the minimum word count rule without any difficulty at all."
        )
        report, _ = await _report([make_comment("c-1", text, is_pinned=True)])
        rows = [ln for ln in report.splitlines() if "`c-1`" in ln]
        assert rows, "the comment should appear in the actioned table"
        assert all(ln.startswith("|") and ln.endswith("|") for ln in rows)

    async def test_markdown_in_a_comment_does_not_restructure_the_report(self) -> None:
        text = (
            "## A fake heading in a comment\n\n- and a list\n\nplus enough words "
            "here to get past the minimum count rule that the pipeline applies."
        )
        report, _ = await _report([make_comment("c-1", text)])
        # Quoted, so every line of the comment is prefixed and cannot become a heading.
        assert "\n## A fake heading in a comment" not in report

    async def test_an_unmatched_code_fence_cannot_swallow_the_report(self) -> None:
        # A single unmatched fence is the dangerous input. A balanced pair proved
        # nothing: the count stays even whatever the renderer does with it.
        text = (
            "A comment containing one ``` unmatched fence marker, and otherwise "
            "enough words to be classified normally by this pipeline here."
        )
        report, _ = await _report([make_comment("c-1", text)])
        after = report.split("`c-1`", 1)[1]
        assert "## " in after, "content after the comment was swallowed by a fence"


class TestContent:
    async def test_uuid_is_present_for_every_proposal(self) -> None:
        """Editors act on the comment in Viafoura, so the uuid is the handle."""
        report, _ = await _report([make_comment("c-abc")])
        assert "`c-abc`" in report

    async def test_reply_is_marked_carousel_only(self) -> None:
        report, _ = await _report([make_comment("c-1", is_reply=True)])
        assert "carousel only (reply)" in report

    async def test_actioned_comment_is_out_of_proposals_but_shown(self) -> None:
        report, _ = await _report([make_comment("c-pinned", is_pinned=True)])
        assert "## Already pinned or picked" in report
        proposals = report.split("## Already pinned")[0]
        assert "c-pinned" not in proposals

    async def test_all_fourteen_questions_are_accounted_for(self) -> None:
        """Eight feed the score table, six are gates. Nothing is silently dropped."""
        report, _ = await _report([make_comment("c-1")])
        for name in (
            "experience",
            "tone",
            "contribution",
            "readability",
            "standalone",
            "representativeness",
        ):
            assert name in report, f"{name} missing from the score table"
        for gate in (
            "on-topic",
            "sarcasm",
            "attack",
            "hostility",
            "profanity",
            "unverified claims",
        ):
            assert gate in report, f"{gate} missing from the gate line"

    async def test_contributions_sum_to_the_total(self) -> None:
        import re

        report, result = await _report([make_comment("c-1")])
        rows = re.findall(r"^\| (\d\.\d{3}) \| (?!\*\*)", report, re.M)
        total = sum(float(v) for v in rows)
        assert total == pytest.approx(result.shortlist[0].quality_score, abs=0.002)

    async def test_empty_thread_renders_without_raising(self) -> None:
        report, _ = await _report([])
        assert "# Comment classification" in report

    async def test_everything_excluded_renders_without_raising(self) -> None:
        report, _ = await _report([make_comment("c-1", "Too short.")])
        assert "## Excluded" in report


API = make_settings().api


class TestSelection:
    def test_score_threshold_bounds(self) -> None:
        class Fake:
            def __init__(self, score: float) -> None:
                self.quality_score = score
                self.already_actioned = False

        # Plenty above the bar: capped at `max_results`.
        many = [Fake(0.9 - 0.01 * i) for i in range(40)]
        chosen, cleared = w._proposed(many, API)
        assert len(chosen) == API.max_results
        assert cleared == 40

        # Nothing above the bar: still returns the best few rather than nothing.
        weak = [Fake(0.3), Fake(0.2), Fake(0.1)]
        chosen, cleared = w._proposed(weak, API)
        assert cleared == 0
        assert len(chosen) == 3

        assert w._proposed([], API) == ([], 0)

    def test_actioned_comments_are_never_proposed(self) -> None:
        class Fake:
            def __init__(self, score: float, actioned: bool) -> None:
                self.quality_score = score
                self.already_actioned = actioned

        chosen, _ = w._proposed([Fake(0.9, True), Fake(0.8, False)], API)
        assert all(not c.already_actioned for c in chosen)


class TestRunHistory:
    async def test_second_run_report_shows_the_run_count(self) -> None:
        store = ThreadStore(container_uuid="container-1")
        store.note_run()
        store.note_run()
        report, _ = await _report([make_comment("c-1")], store=store)
        assert "run 2" in report

    async def test_first_run_has_no_history_clutter(self) -> None:
        store = ThreadStore(container_uuid="container-1")
        store.note_run()
        report, _ = await _report([make_comment("c-1")], store=store)
        assert "run 1" not in report
