"""The paths where money is lost. Every one of these was a live defect.

Found by the pipeline-qa agent on 2026-09-22; each test failed against the code as it
stood, so each one is a regression guard rather than a restatement.
"""

from __future__ import annotations

import pytest
from conftest import FakeJev, FakeResponse, make_comment, make_thread

from processing.classification import battery_fingerprint, classify_thread
from processing.store import (
    LOCK_STALE_SECONDS,
    StoreError,
    ThreadStore,
    load_store,
    locked,
    save_store,
    store_path,
    sweep_temp_files,
)

LONG = (
    "A comment of entirely sufficient length to be classified by this pipeline "
    "without tripping any of the code-side exclusion rules at all."
)


class BrokenJev(FakeJev):
    """Returns a response missing one question id, as a server hiccup would."""

    def __init__(self, break_on: str) -> None:
        super().__init__()
        self.break_on = break_on

    async def ask(self, state, questions) -> FakeResponse:  # noqa: ANN001
        response = await super().ask(state, questions)
        if state["comment"]["text"] == self.break_on:
            del response.answers["tone"]
        return response


class TestMalformedResponse:
    async def test_one_bad_response_does_not_abort_the_run(self, state_dir) -> None:
        """Parsing used to sit outside the try, so a KeyError killed everything."""
        good = [make_comment(f"c-{i}", f"Comment number {i}. {LONG}") for i in range(4)]
        bad = make_comment("c-bad", f"The malformed one. {LONG}")
        store = load_store("container-1", state_dir)

        result = await classify_thread(
            client=BrokenJev(break_on=bad.text),
            thread=make_thread([*good, bad]),
            article_max_words=600,
            store=store,
        )

        assert len(result.shortlist) == 4, "the healthy comments must survive"
        assert [r.comment.uuid for r in result.records if r.error] == ["c-bad"]

    async def test_answers_already_paid_for_are_kept(self, state_dir) -> None:
        """The failure mode that cost the most: a whole run's spend discarded."""
        good = [make_comment(f"c-{i}", f"Comment number {i}. {LONG}") for i in range(4)]
        bad = make_comment("c-bad", f"The malformed one. {LONG}")
        store = load_store("container-1", state_dir)

        await classify_thread(
            client=BrokenJev(break_on=bad.text),
            thread=make_thread([*good, bad]),
            article_max_words=600,
            store=store,
        )
        save_store(store, state_dir)

        assert len(load_store("container-1", state_dir)) == 4


class TestInterruptedRun:
    async def test_answers_are_stored_as_they_arrive(self, state_dir) -> None:
        """Batching the writes until the end lost everything on a Ctrl-C."""
        comments = [
            make_comment(f"c-{i}", f"Comment number {i}. {LONG}") for i in range(5)
        ]
        store = load_store("container-1", state_dir)

        class Interrupted(BaseException):
            """Stands in for Ctrl-C: `_classify_one` catches Exception, not this."""

        class Interrupting(FakeJev):
            async def ask(self, state, questions) -> object:  # noqa: ANN001
                if len(self.calls) >= 3:
                    raise Interrupted
                return await super().ask(state, questions)

        with pytest.raises(Interrupted):
            await classify_thread(
                client=Interrupting(),
                thread=make_thread(comments),
                article_max_words=600,
                store=store,
            )

        # Whatever was bought before the interruption must be on hand to save.
        assert len(store) == 3
        save_store(store, state_dir)
        assert len(load_store("container-1", state_dir)) == 3


class TestFingerprint:
    async def test_a_changed_battery_discards_the_cache(self, state_dir) -> None:
        """Reusing answers from a different battery misranks silently."""
        comment = make_comment("c-1", LONG)
        store = ThreadStore(
            container_uuid="container-1",
            fingerprint=battery_fingerprint(600),
        )
        await classify_thread(
            client=FakeJev(),
            thread=make_thread([comment]),
            article_max_words=600,
            store=store,
        )
        save_store(store, state_dir)

        same = load_store("container-1", state_dir, battery_fingerprint(600))
        assert len(same) == 1, "an unchanged battery must reuse its answers"

        changed = load_store("container-1", state_dir, "a-different-fingerprint")
        assert len(changed) == 0, "a changed battery must not reuse stale answers"

    def test_article_length_is_part_of_the_fingerprint(self) -> None:
        assert battery_fingerprint(600) != battery_fingerprint(300)

    def test_the_fingerprint_is_stable_for_identical_inputs(self) -> None:
        assert battery_fingerprint(600) == battery_fingerprint(600)


class TestMalformedStoreFile:
    @pytest.mark.parametrize(
        "content",
        [
            "null",
            "[1, 2, 3]",
            '"a bare string"',
            "123",
            '{"schema_version": 1, "runs": "lots"}',
            '{"schema_version": 1, "classified": "not a dict"}',
            '{"schema_version": 1, "classified": {"c-1": "not a dict"}}',
            '{"schema_version": 1, "first_run_at": 12345}',
        ],
    )
    def test_valid_json_of_the_wrong_shape_does_not_kill_the_run(
        self,
        content: str,
        state_dir,
    ) -> None:
        """These parse cleanly and used to explode much further in."""
        store_path("container-1", state_dir).write_text(content, encoding="utf-8")
        store = load_store("container-1", state_dir)
        assert len(store) == 0
        assert isinstance(store.runs, int)


class TestLocking:
    def test_a_second_run_on_one_article_is_refused(self, state_dir) -> None:
        with (
            locked("container-1", state_dir),
            pytest.raises(StoreError, match="Another run"),
            locked("container-1", state_dir),
        ):
            pass

    def test_different_articles_do_not_block_each_other(self, state_dir) -> None:
        with locked("container-1", state_dir), locked("container-2", state_dir):
            pass

    def test_the_lock_is_released_even_on_failure(self, state_dir) -> None:
        def explode() -> None:
            with locked("container-1", state_dir):
                msg = "boom"
                raise ValueError(msg)

        with pytest.raises(ValueError, match="boom"):
            explode()
        with locked("container-1", state_dir):
            pass

    def test_a_stale_lock_is_broken(self, state_dir) -> None:
        import os
        import time

        path = store_path("container-1", state_dir).with_suffix(".lock")
        path.write_text("99999", encoding="utf-8")
        old = time.time() - LOCK_STALE_SECONDS - 60
        os.utime(path, (old, old))

        with locked("container-1", state_dir):
            pass  # a killed run must not block the next one forever


class TestTempFileSweep:
    def test_orphaned_temp_files_are_removed(self, state_dir) -> None:
        import os
        import time

        orphan = state_dir / ".container-1.abcd.tmp"
        orphan.write_text("partial", encoding="utf-8")
        old = time.time() - 7200
        os.utime(orphan, (old, old))

        assert sweep_temp_files(state_dir) == 1
        assert not orphan.exists()

    def test_a_fresh_temp_file_is_left_alone(self, state_dir) -> None:
        """It may belong to a run happening right now."""
        fresh = state_dir / ".container-1.efgh.tmp"
        fresh.write_text("in progress", encoding="utf-8")
        assert sweep_temp_files(state_dir) == 0
        assert fresh.exists()


class TestPerCommentModel:
    async def test_each_comment_records_the_model_that_answered_it(
        self,
        state_dir,
    ) -> None:
        """Using the client's cumulative set labelled comments with every version."""
        comments = [
            make_comment(f"c-{i}", f"Comment number {i}. {LONG}") for i in range(3)
        ]
        store = load_store("container-1", state_dir)
        await classify_thread(
            client=FakeJev(model="jev-1.13.0"),
            thread=make_thread(comments),
            article_max_words=600,
            store=store,
        )
        for comment in comments:
            assert store.get(comment.uuid).model == "jev-1.13.0"


class TestLockOwnership:
    """A holder whose lock was broken as stale must not delete its successor's.

    Found by the pipeline-qa agent in the first version of `locked()`: breaking a stale
    lock re-opened the exact double-spend race the lock exists to prevent.
    """

    def test_a_slow_holder_does_not_steal_the_new_lock(self, state_dir) -> None:
        import os
        import time

        from processing.store import _acquire_lock, _release_lock

        # A takes the lock and then overruns.
        path_a, token_a = _acquire_lock("container-1", state_dir)
        old = time.time() - LOCK_STALE_SECONDS - 60
        os.utime(path_a, (old, old))

        # B sees it as stale, breaks it, and is now the legitimate holder.
        path_b, token_b = _acquire_lock("container-1", state_dir)
        assert path_b == path_a

        # A finally finishes. It must not remove B's lock.
        _release_lock(path_a, token_a)
        assert path_b.exists(), "the slow holder deleted its successor's lock"

        # So a fourth run is still correctly refused while B works.
        with pytest.raises(StoreError, match="Another run"):
            _acquire_lock("container-1", state_dir)

        _release_lock(path_b, token_b)
        assert not path_b.exists()

    def test_the_rightful_holder_still_releases(self, state_dir) -> None:
        from processing.store import _acquire_lock, _release_lock

        path, token = _acquire_lock("container-1", state_dir)
        _release_lock(path, token)
        assert not path.exists()

    def test_two_holders_never_share_a_token(self, state_dir) -> None:
        from processing.store import _acquire_lock, _release_lock

        path_1, token_1 = _acquire_lock("container-1", state_dir)
        _release_lock(path_1, token_1)
        _, token_2 = _acquire_lock("container-1", state_dir)
        assert token_1 != token_2

    async def test_the_async_lock_excludes_too(self, state_dir) -> None:
        from processing.store import locked_async

        async with locked_async("container-1", state_dir):
            with pytest.raises(StoreError, match="Another run"):
                async with locked_async("container-1", state_dir):
                    pass


class TestRestoredMetadata:
    async def test_a_restored_comment_knows_which_model_answered_it(
        self,
        state_dir,
    ) -> None:
        """The value is in the store; dropping it leaves a trap for the next reader."""
        comment = make_comment("c-1", LONG)
        store = load_store("container-1", state_dir)
        await classify_thread(
            client=FakeJev(model="jev-1.13.0"),
            thread=make_thread([comment]),
            article_max_words=600,
            store=store,
        )
        save_store(store, state_dir)

        result = await classify_thread(
            client=FakeJev(),
            thread=make_thread([comment]),
            article_max_words=600,
            store=load_store("container-1", state_dir),
        )
        assert result.reused == 1
        assert result.records[0].model == "jev-1.13.0"
