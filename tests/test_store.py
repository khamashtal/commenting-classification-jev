"""The store: what survives between runs, and what happens when it does not."""

from __future__ import annotations

import json

import pytest

from processing.store import (
    SCHEMA_VERSION,
    StoreError,
    ThreadStore,
    load_store,
    save_store,
    store_path,
)


def test_round_trip_preserves_answers(state_dir) -> None:
    store = ThreadStore(container_uuid="container-1")
    store.remember("c-1", {"tone": 1.8, "sarcasm": 0.02}, "jev-1.13.0")
    store.note_run()
    save_store(store, state_dir)

    reloaded = load_store("container-1", state_dir)
    assert "c-1" in reloaded
    assert reloaded.get("c-1").answers == {"tone": 1.8, "sarcasm": 0.02}
    assert reloaded.get("c-1").model == "jev-1.13.0"
    assert reloaded.runs == 1


def test_missing_file_is_an_empty_store(state_dir) -> None:
    store = load_store("never-seen", state_dir)
    assert len(store) == 0
    assert store.runs == 0


def test_corrupt_file_degrades_to_empty_rather_than_raising(state_dir) -> None:
    """Losing the cache costs money; losing the run costs the user. Prefer the money."""
    path = store_path("container-1", state_dir)
    path.write_text("{not json at all", encoding="utf-8")

    store = load_store("container-1", state_dir)
    assert len(store) == 0


def test_unknown_schema_version_degrades_to_empty(state_dir) -> None:
    path = store_path("container-1", state_dir)
    path.write_text(
        json.dumps({"schema_version": 99, "classified": {"c-1": {"answers": {}}}}),
        encoding="utf-8",
    )
    assert len(load_store("container-1", state_dir)) == 0


def test_failed_classification_is_not_remembered() -> None:
    """Caching an empty answer would make a transient failure permanent."""
    store = ThreadStore(container_uuid="container-1")
    store.remember("c-1", {}, "jev-1.13.0")
    assert "c-1" not in store


def test_run_counter_and_first_seen() -> None:
    store = ThreadStore(container_uuid="container-1")
    store.note_run()
    first = store.first_run_at
    store.note_run()

    assert store.runs == 2
    assert store.first_run_at == first, "first_run_at must not move on later runs"
    assert store.last_run_at >= first


@pytest.mark.parametrize(
    "unsafe",
    ["../escape", "a/b", "..", "", "x" * 200, "with space", "semi;colon"],
)
def test_path_traversal_is_refused(unsafe: str, state_dir) -> None:
    """The uuid becomes a filename. A request-supplied one must not escape the directory."""
    with pytest.raises(StoreError):
        store_path(unsafe, state_dir)


def test_save_is_atomic_and_leaves_no_temp_files(state_dir) -> None:
    store = ThreadStore(container_uuid="container-1")
    store.remember("c-1", {"tone": 1.0}, "jev-1.13.0")
    save_store(store, state_dir)
    save_store(store, state_dir)

    files = sorted(p.name for p in state_dir.iterdir())
    assert files == ["container-1.json"], f"temp files left behind: {files}"


def test_previous_state_survives_a_failed_write(state_dir, monkeypatch) -> None:
    """A crash mid-write must leave *this* file's previous contents intact.

    The earlier version failed a write to container-2 and then asserted container-1 was
    fine — a file the failing write never touched, so a naive `path.write_text` passed
    it. This fails the write to the very file it then checks.
    """
    store = ThreadStore(container_uuid="container-1")
    store.remember("c-1", {"tone": 1.0}, "jev-1.13.0")
    save_store(store, state_dir)

    def boom(*_args: object, **_kwargs: object) -> None:
        msg = "simulated crash during rename"
        raise OSError(msg)

    # Fail at the rename, after the temp file has been written in full.
    monkeypatch.setattr("processing.store.os.replace", boom)
    store.remember("c-2", {"tone": 2.0}, "jev-1.13.0")
    with pytest.raises(StoreError):
        save_store(store, state_dir)
    monkeypatch.undo()

    reloaded = load_store("container-1", state_dir)
    assert reloaded.get("c-1").answers == {"tone": 1.0}, "old contents were lost"
    assert "c-2" not in reloaded, "a failed write must not land partially"
    assert sorted(p.name for p in state_dir.iterdir()) == ["container-1.json"], (
        "the temp file was left behind after a failed rename"
    )


def test_written_file_is_valid_json_with_the_schema_version(state_dir) -> None:
    store = ThreadStore(container_uuid="container-1")
    store.remember("c-1", {"tone": 1.8}, "jev-1.13.0")
    path = save_store(store, state_dir)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["classified"]["c-1"]["answers"]["tone"] == 1.8


def test_unicode_survives_the_round_trip(state_dir) -> None:
    store = ThreadStore(container_uuid="container-1")
    store.remember("c-1", {"note": "café — naïve “quotes”"}, "jev-1.13.0")
    save_store(store, state_dir)
    assert load_store("container-1", state_dir).get("c-1").answers["note"] == (
        "café — naïve “quotes”"
    )


def test_article_metadata_survives_the_round_trip(state_dir) -> None:
    """A uuid in a filename identifies nothing to a person opening state/."""
    store = ThreadStore(container_uuid="container-1")
    store.describe(url="https://t.co.uk/news/x/", headline="A headline")
    store.remember("c-1", {"tone": 1.0}, "jev-1.13.0")
    save_store(store, state_dir)

    reloaded = load_store("container-1", state_dir)
    assert reloaded.article_url == "https://t.co.uk/news/x/"
    assert reloaded.article_headline == "A headline"


def test_timestamps_are_utc_with_an_explicit_offset(state_dir) -> None:
    """Local time would be meaningless on another machine; a bare time is ambiguous."""
    from datetime import UTC, datetime

    store = ThreadStore(container_uuid="container-1")
    store.remember("c-1", {"tone": 1.0}, "jev-1.13.0")
    store.note_run()
    save_store(store, state_dir)

    reloaded = load_store("container-1", state_dir)
    for stamp in (
        reloaded.first_run_at,
        reloaded.last_run_at,
        reloaded.get("c-1").classified_at,
    ):
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None, f"{stamp} is naive"
        assert parsed.utcoffset().total_seconds() == 0, f"{stamp} is not UTC"
        assert stamp.endswith("+00:00"), f"{stamp} does not show its offset"
        # To the second: six decimal places hid the offset that matters.
        assert "." not in stamp, f"{stamp} still carries microseconds"
        assert abs((datetime.now(UTC) - parsed).total_seconds()) < 60


def test_comment_text_is_stored_and_survives_a_round_trip(state_dir) -> None:
    store = ThreadStore(container_uuid="container-1")
    store.remember("c-1", {"tone": 1.8}, "jev-1.13.0", text="A reasoned point.")
    save_store(store, state_dir)

    raw = json.loads(store_path("container-1", state_dir).read_text(encoding="utf-8"))
    assert raw["classified"]["c-1"]["text"] == "A reasoned point."
    assert load_store("container-1", state_dir).get("c-1").text == "A reasoned point."


def test_entry_without_text_loads_and_is_filled_in_but_never_overwritten(
    state_dir,
) -> None:
    """Files written before the text was stored must still load, and gain it later."""
    store = ThreadStore(container_uuid="container-1")
    store.remember("c-1", {"tone": 1.0}, "jev-1.13.0")
    save_store(store, state_dir)

    reloaded = load_store("container-1", state_dir)
    assert reloaded.get("c-1").text == ""
    reloaded.fill_text("c-1", "The original text.")
    reloaded.fill_text("c-1", "Something else.")
    assert reloaded.get("c-1").text == "The original text."
    assert reloaded.get("c-1").answers == {"tone": 1.0}
