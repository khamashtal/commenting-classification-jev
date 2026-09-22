"""What the pipeline remembers between runs: one JSON file per article.

A comment's Jev answers depend on the article body, the comment text and its parent text,
all three immutable once posted. So an answer, once obtained, is correct forever, and
re-asking is pure waste — a thousand-comment thread polled every five minutes costs $0.13
a day with this store and $38 without it.

**The set of classified uuids is the whole mechanism.** There is deliberately no
timestamp cursor. Viafoura releases comments from moderation out of creation order, so a
comment created at 10:00 can appear at 10:45, behind a 10:30 watermark, and a timestamp
cursor drops it silently and permanently. Fetching the current thread every run and
skipping the uuids already answered has no such failure mode, costs nothing extra —
Viafoura is free, Jev is what bills — and needs no cursor at all. `last_run_at` is
recorded for the report, never read as a cursor.

One file per container, not one big file: two articles never contend, and a corrupt file
costs one thread rather than all of them. Writes go through a temporary file in the same
directory and an atomic `os.replace`, so a crash mid-write leaves the previous run's
state intact rather than a truncated file.

The limits of this shape, and what replaces it: there is no protection against two runs
on the same article at once (the second would clobber the first), and the whole map is
read into memory. At roughly 1 KB a comment that is fine for a POC. SQLite is the
migration when the pipeline polls many articles in parallel; see Part II of
`.claude/comment-classification-spec.md`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from processing.log_config import logger

# A container uuid is the file name, so it is checked rather than trusted. Viafoura
# supplies it today, but the FastAPI work will let a caller supply it, and `../` in a
# path segment is a directory traversal.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

SCHEMA_VERSION = 1
# A lock older than this is assumed to belong to a killed process rather than a live run.
# Comfortably longer than a full classification of a very large thread.
LOCK_STALE_SECONDS = 1800


def _timestamp() -> str:
    """Now, in UTC, to the second.

    UTC rather than local time because these files outlive the machine that wrote them
    and a local timestamp is meaningless on another one. To the second rather than the
    microsecond because six decimal places buried the `+00:00` that says so — the offset
    is the part a reader needs, and it is an hour out from British clocks for half the
    year, which reads as a bug until you spot it.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


class StoreError(RuntimeError):
    """The store could not be read or written."""


@dataclass(frozen=True, slots=True)
class StoredAnswers:
    """One comment's Jev answers, as first obtained."""

    answers: dict[str, Any]
    model: str
    classified_at: str

    def as_json(self) -> dict[str, Any]:
        return {
            "answers": self.answers,
            "model": self.model,
            "classified_at": self.classified_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> StoredAnswers:
        return cls(
            answers=dict(payload.get("answers") or {}),
            model=str(payload.get("model") or "unknown"),
            classified_at=str(payload.get("classified_at") or ""),
        )


@dataclass(slots=True)
class ThreadStore:
    """Everything remembered about one article's thread."""

    container_uuid: str
    article_url: str = ""
    """Which article this file is about. Recorded purely so a person opening `state/`
    can tell at a glance; nothing reads it."""
    article_headline: str = ""
    """As above. A uuid in a filename identifies nothing to a human."""
    fingerprint: str = ""
    """Identifies the battery and article settings these answers were computed against.

    A mismatch on load discards everything: see `classification.battery_fingerprint`.
    """
    first_run_at: str = ""
    last_run_at: str = ""
    runs: int = 0
    classified: dict[str, StoredAnswers] = field(default_factory=dict)

    def __contains__(self, comment_uuid: str) -> bool:
        return comment_uuid in self.classified

    def __len__(self) -> int:
        return len(self.classified)

    def get(self, comment_uuid: str) -> StoredAnswers | None:
        return self.classified.get(comment_uuid)

    def remember(
        self,
        comment_uuid: str,
        answers: dict[str, Any],
        model: str,
    ) -> None:
        """Record one comment's answers, if it has any.

        An empty answer dict means the call failed; storing it would cache the failure
        and the comment would never be retried.
        """
        if not answers:
            return
        self.classified[comment_uuid] = StoredAnswers(
            answers=answers,
            model=model,
            classified_at=_timestamp(),
        )

    def describe(self, *, url: str, headline: str) -> None:
        """Record which article this is, for whoever opens the file."""
        self.article_url = url
        self.article_headline = headline

    def note_run(self) -> None:
        """Stamp this run. Informational only — never used as a cursor."""
        now = _timestamp()
        self.first_run_at = self.first_run_at or now
        self.last_run_at = now
        self.runs += 1


def _acquire_lock(container_uuid: str, directory: Path) -> tuple[Path, str]:
    """Take the lock and return its path and this holder's token.

    The token is what makes releasing safe. A lock file alone is not enough: a holder
    that overran `LOCK_STALE_SECONDS` has its lock broken by the next run, and when it
    eventually finishes it would delete *that* run's live lock, letting a fourth process
    in alongside it. Ownership has to be proved at release, not assumed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = store_path(container_uuid, directory).with_suffix(".lock")

    if path.exists():
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:  # pragma: no cover - removed by another run in between
            age = 0.0
        if age > LOCK_STALE_SECONDS:
            logger.warning(
                "Breaking a stale lock on %s (%.0fs old); a previous run was killed, "
                "or is running far longer than expected.",
                path.name,
                age,
            )
            path.unlink(missing_ok=True)

    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        msg = (
            f"Another run is already working on {container_uuid}. Wait for it to "
            f"finish: running both would pay Jev twice for the same comments, and one "
            f"set of answers would be lost."
        )
        raise StoreError(msg) from exc

    token = f"{os.getpid()}:{uuid4().hex}"
    try:
        os.write(handle, token.encode())
    finally:
        os.close(handle)
    return path, token


def _release_lock(path: Path, token: str) -> None:
    """Release the lock, but only if it is still ours.

    A holder whose lock was broken as stale must not delete its successor's lock. The
    read-then-unlink is not atomic, so this narrows the window rather than closing it;
    closing it properly needs `flock` or the SQLite move the spec already plans. It turns
    a silent double-spend into a vanishingly unlikely one.
    """
    try:
        if path.read_text(encoding="utf-8") != token:
            logger.warning(
                "Not releasing %s: it was broken as stale and now belongs to another "
                "run. This run took longer than LOCK_STALE_SECONDS (%ds).",
                path.name,
                LOCK_STALE_SECONDS,
            )
            return
    except OSError:  # already gone, or unreadable: nothing of ours to remove
        return
    path.unlink(missing_ok=True)


@contextmanager
def locked(container_uuid: str, directory: Path) -> Iterator[None]:
    """Hold an exclusive lock on one thread's state for the duration of a run.

    `os.replace` makes the *write* atomic; it does nothing for the read-modify-write
    around it. Two overlapping runs on one article both load the same snapshot, both pay
    Jev for the same new comments, and the second save erases the first's answers — a
    silent double-spend, and the normal case the moment this is a service handling two
    requests for the same article.

    A run that cannot take the lock raises rather than waiting: the caller can retry, and
    an unnoticed queue of runs on one article is no better than an unnoticed collision.
    A lock older than `LOCK_STALE_SECONDS` is assumed to belong to a killed process and
    is broken — safely, because release checks ownership.
    """
    path, token = _acquire_lock(container_uuid, directory)
    try:
        yield
    finally:
        _release_lock(path, token)


@asynccontextmanager
async def locked_async(container_uuid: str, directory: Path) -> AsyncIterator[None]:
    """`locked`, with its syscalls off the event loop.

    Small and local, but every other disk operation in `workflow.run` is offloaded and
    an exception here would be the odd one out — the kind of inconsistency that makes
    the next person wonder which rule applies.
    """
    path, token = await asyncio.to_thread(_acquire_lock, container_uuid, directory)
    try:
        yield
    finally:
        await asyncio.to_thread(_release_lock, path, token)


def sweep_temp_files(directory: Path, older_than_seconds: float = 3600) -> int:
    """Delete temp files a killed process left behind. Returns how many went.

    `mkstemp` creates the file before the write, so a SIGKILL between the two leaves it
    in `state/` forever; nothing else would ever remove it.
    """
    if not directory.exists():
        return 0
    removed = 0
    cutoff = time.time() - older_than_seconds
    for path in directory.glob(".*.tmp"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:  # pragma: no cover - racing another sweep is harmless
            continue
    return removed


def _sync_directory(directory: Path) -> None:
    """Flush a directory entry so a completed rename survives a power loss."""
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - not every platform allows this
        return
    try:
        os.fsync(handle)
    except OSError:  # pragma: no cover - nor does every filesystem support it
        pass
    finally:
        os.close(handle)


def safe_key(container_uuid: str) -> str:
    """Return ``container_uuid`` if it is safe to put in a filename, else raise.

    Shared with the report path so both are checked by the same rule. Viafoura supplies
    this value today; the FastAPI work will let a caller supply it, and `../` in a path
    segment is a directory traversal.
    """
    if not _SAFE_KEY.match(container_uuid):
        msg = f"Unsafe container uuid for a file name: {container_uuid!r}"
        raise StoreError(msg)
    return container_uuid


def store_path(container_uuid: str, directory: Path) -> Path:
    """Where one thread's state lives, with the name checked rather than trusted."""
    return directory / f"{safe_key(container_uuid)}.json"


def load_store(
    container_uuid: str,
    directory: Path,
    fingerprint: str = "",
) -> ThreadStore:
    """Read a thread's state, or start an empty one.

    Every failure mode here ends the same way: an empty store. Losing the cache costs
    money and nothing else; refusing to run costs the user their report. So a missing
    file, an unreadable one, JSON of the wrong shape, an unknown schema version and a
    stale fingerprint are all recoverable, and all logged loudly enough to notice.
    """
    empty = ThreadStore(container_uuid=container_uuid, fingerprint=fingerprint)
    path = store_path(container_uuid, directory)
    if not path.exists():
        return empty

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # ValueError covers JSONDecodeError
        logger.warning(
            "Could not read %s (%s); starting from empty. Everything will be "
            "reclassified, which costs money but loses nothing.",
            path,
            exc,
        )
        return empty

    # Valid JSON is not the same as the right shape: `null`, a list, or a string where a
    # number belongs all parse cleanly and then fail much further in.
    if not isinstance(payload, dict):
        logger.warning("%s is valid JSON but not an object; starting from empty.", path)
        return empty

    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        logger.warning(
            "%s has schema_version %r, expected %d; starting from empty.",
            path,
            version,
            SCHEMA_VERSION,
        )
        return empty

    stored_fingerprint = str(payload.get("fingerprint") or "")
    if fingerprint and stored_fingerprint != fingerprint:
        logger.warning(
            "%s was written against a different battery or article setting "
            "(%s, now %s); starting from empty. Everything will be reclassified, "
            "because reusing those answers would silently misrank this thread.",
            path,
            stored_fingerprint or "none recorded",
            fingerprint,
        )
        return empty

    raw = payload.get("classified")
    classified = (
        {
            uuid: StoredAnswers.from_json(record)
            for uuid, record in raw.items()
            if isinstance(record, dict) and isinstance(uuid, str)
        }
        if isinstance(raw, dict)
        else {}
    )
    return ThreadStore(
        container_uuid=container_uuid,
        article_url=_text(payload.get("article_url")),
        article_headline=_text(payload.get("article_headline")),
        fingerprint=fingerprint or stored_fingerprint,
        first_run_at=_text(payload.get("first_run_at")),
        last_run_at=_text(payload.get("last_run_at")),
        runs=_count(payload.get("runs")),
        classified=classified,
    )


def _text(value: object) -> str:
    """A string field from an untrusted file, however it was written."""
    return value if isinstance(value, str) else ""


def _count(value: object) -> int:
    """A non-negative integer field, tolerating whatever the file actually holds."""
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def save_store(store: ThreadStore, directory: Path) -> Path:
    """Write a thread's state atomically.

    The temporary file is created in the destination directory so `os.replace` is a
    rename within one filesystem, which is atomic. Writing elsewhere and moving across
    devices is not, and would reintroduce the truncated-file case this avoids.
    """
    path = store_path(store.container_uuid, directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "container_uuid": store.container_uuid,
        # First in the file, after the ids, because this is what a person opening it is
        # looking for. Everything below is for the machine.
        "article_url": store.article_url,
        "article_headline": store.article_headline,
        "fingerprint": store.fingerprint,
        "first_run_at": store.first_run_at,
        "last_run_at": store.last_run_at,
        "runs": store.runs,
        "classified": {
            uuid: record.as_json() for uuid, record in store.classified.items()
        },
    }

    handle, tmp_name = tempfile.mkstemp(
        dir=directory,
        prefix=f".{store.container_uuid}.",
        suffix=".tmp",
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            # No indent: machine-written, machine-read, rewritten in full every run.
            # Pretty-printing cost ~40% of the file size for nobody's benefit.
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        # The contents are durable after the fsync above; the *rename* is not until the
        # directory entry is flushed too.
        _sync_directory(directory)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        msg = f"Could not write {path}: {exc}"
        raise StoreError(msg) from exc
    return path
