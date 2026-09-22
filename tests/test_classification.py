"""Scoring, exclusions and the deterministic signals that keep work off the model."""

from __future__ import annotations

import pytest
from conftest import (
    CLASSIFICATION,
    GOOD_ANSWERS,
    SARCASTIC_ANSWERS,
    WEAK_ANSWERS,
    FakeJev,
    make_comment,
    make_record,
    make_thread,
)

from processing.classification import (
    classify_thread,
    compute_signals,
    hard_exclusion,
    quality_score,
    stance_shares,
)


class TestCodeSignals:
    """Anything countable stays in code, because Jev cannot count."""

    def test_word_count(self) -> None:
        assert compute_signals("one two three").word_count == 3

    def test_caps_ratio_ignores_punctuation_and_digits(self) -> None:
        assert compute_signals("AB!! 123").caps_ratio == 1.0
        assert compute_signals("ab!! 123").caps_ratio == 0.0

    def test_empty_text_does_not_divide_by_zero(self) -> None:
        assert compute_signals("").caps_ratio == 0.0

    @pytest.mark.parametrize(
        ("text", "external"),
        [
            ("see https://www.telegraph.co.uk/news/x", False),
            ("see https://telegraph.co.uk/news/x", False),
            ("see https://example.com/x", True),
            ("see www.example.com/x", True),
            ("no link here at all", False),
            # The check must not be fooled by a lookalike domain.
            ("see https://telegraph.co.uk.evil.com/x", True),
        ],
    )
    def test_external_url_detection(self, text: str, external: bool) -> None:
        assert compute_signals(text).has_external_url is external


class TestHardExclusion:
    """Rules that run before Jev, so a hopeless comment never costs a call."""

    def test_short_comment_excluded(self) -> None:
        comment = make_comment(text="Too short entirely.")
        reason = hard_exclusion(comment, compute_signals(comment.text), CLASSIFICATION)
        assert reason is not None
        assert str(CLASSIFICATION.min_words) in reason

    def test_external_link_excluded(self) -> None:
        text = (
            "A comment of perfectly adequate length, comfortably past the minimum "
            "word count, that happens to link to https://example.com/x as well."
        )
        assert "link" in (
            hard_exclusion(
                make_comment(text=text),
                compute_signals(text),
                CLASSIFICATION,
            )
            or ""
        )

    def test_shouting_excluded(self) -> None:
        text = (
            "THIS IS ENTIRELY IN CAPITALS AND GOES ON FOR RATHER A LONG TIME INDEED YES"
        )
        assert "capitals" in (
            hard_exclusion(
                make_comment(text=text),
                compute_signals(text),
                CLASSIFICATION,
            )
            or ""
        )

    def test_invisible_comment_excluded(self) -> None:
        comment = make_comment(state="pending")
        assert (
            hard_exclusion(comment, compute_signals(comment.text), CLASSIFICATION)
            is not None
        )

    def test_good_comment_passes(self) -> None:
        comment = make_comment()
        assert (
            hard_exclusion(comment, compute_signals(comment.text), CLASSIFICATION)
            is None
        )


class TestScoring:
    def test_score_is_bounded(self) -> None:
        record = make_record(make_comment(), GOOD_ANSWERS)
        score = quality_score(record, {"supportive": 1.0}, CLASSIFICATION)
        assert 0.0 <= score <= 1.0

    def test_better_answers_score_higher(self) -> None:
        shares = {"supportive": 1.0}
        good = quality_score(
            make_record(make_comment(), GOOD_ANSWERS),
            shares,
            CLASSIFICATION,
        )
        weak = quality_score(
            make_record(make_comment(), WEAK_ANSWERS),
            shares,
            CLASSIFICATION,
        )
        assert good > weak

    def test_missing_answers_score_zero_rather_than_raising(self) -> None:
        assert quality_score(make_record(make_comment(), {}), {}, CLASSIFICATION) == 0.0

    def test_stance_shares_sum_to_one(self) -> None:
        records = [make_record(make_comment(f"c-{i}"), GOOD_ANSWERS) for i in range(4)]
        shares = stance_shares(records)
        assert sum(shares.values()) == pytest.approx(1.0)

    def test_stance_shares_of_nothing_is_empty(self) -> None:
        assert stance_shares([]) == {}

    def test_minority_stance_scores_lower_but_is_not_excluded(self) -> None:
        """The brief wants dissent ranked down, never removed."""
        record = make_record(make_comment(), GOOD_ANSWERS)
        majority = quality_score(record, {"supportive": 0.9}, CLASSIFICATION)
        minority = quality_score(record, {"supportive": 0.1}, CLASSIFICATION)
        assert majority > minority > 0.0


class TestThresholds:
    async def test_sarcasm_excludes(self) -> None:
        comment = make_comment()
        result = await classify_thread(
            client=FakeJev(default=SARCASTIC_ANSWERS),
            thread=make_thread([comment]),
            config=CLASSIFICATION,
            article_max_words=600,
        )
        assert [r.comment.uuid for r in result.excluded] == [comment.uuid]
        assert "sarcasm" in (result.records[0].excluded_reason or "")

    async def test_middle_band_is_flagged_not_excluded(self) -> None:
        answers = {
            **GOOD_ANSWERS,
            "sarcasm": (CLASSIFICATION.exclude_at["sarcasm"] + 0.4) / 2,
        }
        result = await classify_thread(
            client=FakeJev(default=answers),
            thread=make_thread([make_comment()]),
            config=CLASSIFICATION,
            article_max_words=600,
        )
        assert not result.excluded
        assert result.flagged


class TestEligibility:
    async def test_reply_is_carousel_only(self) -> None:
        result = await classify_thread(
            client=FakeJev(),
            thread=make_thread([make_comment(is_reply=True)]),
            config=CLASSIFICATION,
            article_max_words=600,
        )
        assert result.records[0].pin_eligible is False

    @pytest.mark.parametrize(("pinned", "picked"), [(True, False), (False, True)])
    async def test_editor_actioned_comments_are_marked(
        self,
        pinned: bool,
        picked: bool,
    ) -> None:
        result = await classify_thread(
            client=FakeJev(),
            thread=make_thread([make_comment(is_pinned=pinned, is_picked=picked)]),
            config=CLASSIFICATION,
            article_max_words=600,
        )
        assert result.records[0].already_actioned is True

    async def test_viafouras_own_top_comment_flag_is_not_an_editor_action(self) -> None:
        """The brief reports that tool promoting sarcasm; it carries no quality signal."""
        comment = make_comment()
        comment.is_top_comment = True
        result = await classify_thread(
            client=FakeJev(),
            thread=make_thread([comment]),
            config=CLASSIFICATION,
            article_max_words=600,
        )
        assert result.records[0].already_actioned is False


class TestRunIntegrity:
    async def test_one_failure_does_not_kill_the_run(self) -> None:
        good = make_comment(
            "c-good",
            "A comment that will classify perfectly well and "
            "comfortably exceeds the minimum word count that "
            "the code-side rules impose before any call.",
        )
        bad = make_comment(
            "c-bad",
            "A comment whose classification is going to fail "
            "during this particular run of the pipeline.",
        )
        result = await classify_thread(
            client=FakeJev(fail_on={bad.text}),
            thread=make_thread([good, bad]),
            config=CLASSIFICATION,
            article_max_words=600,
        )
        assert len(result.shortlist) == 1
        failed = [r for r in result.records if r.error]
        assert [r.comment.uuid for r in failed] == ["c-bad"]

    async def test_empty_thread_does_not_raise(self) -> None:
        result = await classify_thread(
            client=FakeJev(),
            thread=make_thread([]),
            config=CLASSIFICATION,
            article_max_words=600,
        )
        assert result.records == []
        assert result.shortlist == []

    async def test_article_body_is_capped(self) -> None:
        """The article is re-sent per comment, so the cap is a cost control."""
        thread = make_thread([make_comment()])
        assert len(thread.article.capped_body(50).split()) <= 51  # plus the […] marker

    async def test_shortlist_is_ordered_by_score(self) -> None:
        comments = [
            make_comment(
                "c-good",
                "My wife waited fourteen months for a scan promised "
                "in eighteen weeks and nobody ever explained why.",
            ),
            make_comment(
                "c-weak",
                "I broadly agree with this piece although the "
                "costings look somewhat optimistic to me here.",
            ),
        ]
        result = await classify_thread(
            client=FakeJev(
                answers_for={
                    comments[0].text: GOOD_ANSWERS,
                    comments[1].text: WEAK_ANSWERS,
                },
            ),
            thread=make_thread(comments),
            config=CLASSIFICATION,
            article_max_words=600,
        )
        # Asserting the list is sorted restates what `shortlist` does and passes even
        # if quality_score returns a constant. Assert the ordering by identity instead.
        ranked = [r.comment.uuid for r in result.shortlist]
        assert ranked == ["c-good", "c-weak"]
        assert result.shortlist[0].quality_score > result.shortlist[1].quality_score
