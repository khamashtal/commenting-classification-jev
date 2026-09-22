"""The money test: a second run must not pay for a comment it already has."""

from __future__ import annotations

from conftest import (
    CLASSIFICATION,
    GOOD_ANSWERS,
    WEAK_ANSWERS,
    FakeJev,
    make_classification_config,
    make_comment,
    make_thread,
)

from processing.classification import classify_thread
from processing.config import ClassificationConfig
from processing.store import ThreadStore, load_store, save_store


async def _run(  # noqa: ANN202
    jev: FakeJev,
    comments,  # noqa: ANN001
    store: ThreadStore | None,
    config: ClassificationConfig = CLASSIFICATION,
):
    return await classify_thread(
        client=jev,
        thread=make_thread(comments),
        config=config,
        article_max_words=600,
        store=store,
    )


async def test_second_run_sends_nothing_to_jev(state_dir) -> None:
    comments = [
        make_comment(
            f"c-{i}",
            f"A perfectly ordinary comment number {i}, long "
            f"enough to clear the fifteen word minimum easily.",
        )
        for i in range(5)
    ]

    store = load_store("container-1", state_dir)
    jev = FakeJev()
    first = await _run(jev, comments, store)
    store.note_run()
    save_store(store, state_dir)

    assert len(jev.calls) == 5
    assert first.newly_classified == 5
    assert first.reused == 0

    # Second run, same comments, a fresh client and a store read back from disk.
    store2 = load_store("container-1", state_dir)
    jev2 = FakeJev()
    second = await _run(jev2, comments, store2)

    assert jev2.calls == [], "a second run must not call Jev for known comments"
    assert second.reused == 5
    assert second.newly_classified == 0
    assert second.spend.input_tokens == 0
    assert second.estimated_cost_usd == 0.0


async def test_only_new_comments_are_sent(state_dir) -> None:
    old = [
        make_comment(
            f"c-{i}",
            f"An older comment {i}, written at some length so it "
            f"comfortably clears the minimum word count rule.",
        )
        for i in range(3)
    ]
    store = load_store("container-1", state_dir)
    await _run(FakeJev(), old, store)
    save_store(store, state_dir)

    fresh = make_comment(
        "c-new",
        "A brand new comment, also long enough to pass the "
        "fifteen word minimum without any trouble at all.",
    )
    jev = FakeJev()
    result = await _run(jev, [*old, fresh], load_store("container-1", state_dir))

    assert jev.calls == [fresh.text]
    assert result.newly_classified == 1
    assert result.reused == 3


async def test_restored_answers_produce_the_same_scores(state_dir) -> None:
    """A reused answer must rank exactly as it did when it was bought."""
    comments = [
        make_comment(
            "c-good",
            "My wife waited fourteen months for a scan that was "
            "promised in eighteen weeks, and nobody ever explained.",
        ),
        make_comment(
            "c-weak",
            "I broadly agree with this article although the costings "
            "seem a little optimistic to me on reflection.",
        ),
    ]
    answers = {comments[0].text: GOOD_ANSWERS, comments[1].text: WEAK_ANSWERS}

    store = load_store("container-1", state_dir)
    first = await _run(FakeJev(answers_for=answers), comments, store)
    save_store(store, state_dir)
    second = await _run(FakeJev(), comments, load_store("container-1", state_dir))

    before = {r.comment.uuid: r.quality_score for r in first.records}
    after = {r.comment.uuid: r.quality_score for r in second.records}
    assert before == after


async def test_a_failed_comment_is_retried_next_run(state_dir) -> None:
    """A transient failure must not be cached as 'done'."""
    ok = make_comment(
        "c-ok",
        "A comment that classifies without any trouble at all, "
        "comfortably past the fifteen word minimum.",
    )
    bad = make_comment(
        "c-bad",
        "A comment whose classification call will fail this "
        "time around, but which should be retried later.",
    )

    store = load_store("container-1", state_dir)
    await _run(FakeJev(fail_on={bad.text}), [ok, bad], store)
    save_store(store, state_dir)

    assert "c-ok" in store
    assert "c-bad" not in store, "a failure must not be remembered as an answer"

    jev = FakeJev()
    await _run(jev, [ok, bad], load_store("container-1", state_dir))
    assert jev.calls == [bad.text], (
        "only the previously failed comment should be retried"
    )


async def test_code_excluded_comments_never_reach_jev(state_dir) -> None:
    """The cheapest saving: exclusions that need no model at all."""
    short = make_comment("c-short", "Too short.")
    # Deliberately long enough to pass the word count, so it can only be excluded by
    # the URL rule — otherwise this test would pass without exercising it.
    linked = make_comment(
        "c-url",
        "You should all read this https://example.com/thing "
        "because it explains the whole situation far more "
        "clearly than the article above manages to do.",
    )
    fine = make_comment(
        "c-fine",
        "A perfectly reasonable comment of sufficient length "
        "to be classified properly by the pipeline without "
        "tripping any of the code-side exclusion rules.",
    )

    jev = FakeJev()
    result = await _run(
        jev,
        [short, linked, fine],
        load_store("container-1", state_dir),
    )

    assert jev.calls == [fine.text]
    assert result.newly_classified == 1
    excluded = {r.comment.uuid for r in result.excluded}
    assert excluded == {"c-short", "c-url"}


async def test_thresholds_are_reapplied_to_restored_answers(state_dir) -> None:
    """Retuning a threshold must take effect on old comments without re-billing."""
    comment = make_comment(
        "c-1",
        "A comment that will be re-judged when the sarcasm "
        "threshold moves underneath it after the first run.",
    )
    store = load_store("container-1", state_dir)
    await _run(FakeJev(default={**GOOD_ANSWERS, "sarcasm": 0.5}), [comment], store)
    save_store(store, state_dir)
    assert not (await _run(FakeJev(), [comment], store)).excluded

    # The retune is a different config handed to the same stored answers, which is
    # exactly what editing `config.toml` between two runs does.
    retuned = make_classification_config(
        exclude_at={**CLASSIFICATION.exclude_at, "sarcasm": 0.4},
    )
    jev = FakeJev()
    result = await _run(
        jev,
        [comment],
        load_store("container-1", state_dir),
        retuned,
    )
    assert jev.calls == [], "retuning must not cost a single call"
    assert [r.comment.uuid for r in result.excluded] == ["c-1"]


async def test_model_is_still_named_when_everything_is_reused(state_dir) -> None:
    """A fully cached run must not report 'no successful requests' as its model."""
    comment = make_comment(
        "c-1",
        "A comment of entirely sufficient length to be classified by the pipeline "
        "without tripping any of the code-side exclusion rules at all.",
    )
    store = load_store("container-1", state_dir)
    await _run(FakeJev(model="jev-1.13.0"), [comment], store)
    save_store(store, state_dir)

    second = await _run(FakeJev(), [comment], load_store("container-1", state_dir))
    assert second.newly_classified == 0
    assert second.model == "jev-1.13.0"


async def test_a_version_change_mid_thread_names_both(state_dir) -> None:
    """An alias moving between runs must be visible, not silently overwritten."""
    old = make_comment(
        "c-old",
        "An older comment, long enough to be classified properly by the pipeline "
        "under whichever model version happens to be current.",
    )
    store = load_store("container-1", state_dir)
    await _run(FakeJev(model="jev-1.13.0"), [old], store)
    save_store(store, state_dir)

    new = make_comment(
        "c-new",
        "A newer comment, also long enough to be classified properly once the model "
        "alias has advanced to a later version entirely.",
    )
    result = await _run(
        FakeJev(model="jev-1.14.0"),
        [old, new],
        load_store("container-1", state_dir),
    )
    assert result.model == "jev-1.13.0, jev-1.14.0"
