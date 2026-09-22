"""The shape of ``config.toml``, and how one is read and checked.

This module deliberately imports nothing from the rest of the project. ``settings.py``
pairs what is here with what is in ``.env``; ``classification.py`` consumes
:class:`ClassificationConfig`. Keeping the dataclasses in a module of their own means
neither of those has to import the other.

Two rules hold for everything below.

**Fail loudly.** Every value is type-checked as it is read and every failure names the
key path that caused it. A configuration file is edited by hand, and a mistyped key that
falls back to a default is the same class of bug as a mistyped question id: it changes
what the pipeline does and says nothing.

**Know which keys cost money.** ``[article] max_words`` feeds the store fingerprint, so
changing it discards every stored answer and re-bills the thread. The weights and
thresholds under ``[classification]`` do not: they are applied at scoring time, so
recalibrating them is free. The two sit next to each other and behave completely
differently.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

# The weight names `classification.quality_score` combines. These are not question ids:
# three of them are composites — `experience` is personal_experience x
# experience_relevant, `contribution` is the better of two answers, and
# `representativeness` comes from the thread rather than from any one comment. The set
# must match exactly, because a weight that is missing scores every comment low in
# silence and a weight that is spare does nothing at all.
WEIGHT_KEYS: frozenset[str] = frozenset(
    {
        "experience",
        "tone",
        "readability",
        "contribution",
        "standalone",
        "representativeness",
    },
)


class ConfigError(RuntimeError):
    """``config.toml`` is missing, malformed, or says something impossible."""


# ------------------------------------------------------------------------ the sections


@dataclass(frozen=True, slots=True)
class ViafouraConfig:
    """Where the comments come from and how hard we pull on them.

    No rate limit is documented anywhere for the public API, which is exactly why the
    retry and concurrency values are settings rather than constants: if Viafoura turns
    out to object to the harvest, the fix is a file, not a release.
    """

    section_uuid: str
    base_url: str
    page_size: int
    reply_limit: int
    timeout_seconds: float
    max_concurrent_requests: int
    max_retries: int
    backoff_initial_seconds: float
    backoff_max_seconds: float


@dataclass(frozen=True, slots=True)
class JevConfig:
    """How the Jev client is built. The published limits live here so a change to them
    is an edit to a file rather than to the client.
    """

    model: str | None
    """`None` leaves the version to the API (``jev-latest``). An empty string in the
    TOML means `None`, because TOML has no null."""

    concurrency: int
    timeout_seconds: float
    requests_per_minute: int
    tokens_per_second: int


@dataclass(frozen=True, slots=True)
class ArticleConfig:
    """How much of the article travels with every comment.

    ``max_words`` is the one value in this file that costs money to change: it is part
    of the store fingerprint, so a different number discards every stored answer.
    """

    max_words: int


@dataclass(frozen=True, slots=True)
class ClassificationConfig:
    """Every threshold and weight the classification stage applies.

    Passed into the stage rather than read from it, so the same functions serve a CLI
    run and an HTTP request, and a test can hand them a policy of its own.
    """

    min_words: int
    max_caps_ratio: float
    sweet_spot: tuple[int, int]
    on_topic_exclude_below: float
    on_topic_flag_below: float
    weights: Mapping[str, float]
    exclude_at: Mapping[str, float]
    flag_at: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ApiConfig:
    """The HTTP surface. ``host`` and ``port`` are the one pair the environment may
    override; see ``settings.load_settings``.
    """

    host: str
    port: int
    cors_origins: tuple[str, ...]
    default_min_score: float
    max_results: int
    min_results: int
    lock_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class PathsConfig:
    """Where the pipeline writes. Relative paths are resolved against the project root,
    so they mean the same thing from any working directory.
    """

    state_dir: Path
    feedback_dir: Path
    output_dir: Path


@dataclass(frozen=True, slots=True)
class Config:
    """A parsed ``config.toml``."""

    viafoura: ViafouraConfig
    jev: JevConfig
    article: ArticleConfig
    classification: ClassificationConfig
    api: ApiConfig
    paths: PathsConfig


# --------------------------------------------------------------------------- the reader


class _Reader:
    """Pulls typed values out of one TOML table, naming the key path on any failure."""

    __slots__ = ("_path", "_table")

    def __init__(self, table: Mapping[str, Any], path: str) -> None:
        self._table = table
        self._path = path

    def table(self, key: str) -> _Reader:
        value = self._get(key)
        if not isinstance(value, dict):
            self._fail(key, "a table", value)
        return _Reader(value, f"{self._path}{key}.")

    def str_(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str):
            self._fail(key, "a string", value)
        return value

    def int_(self, key: str, *, minimum: int | None = None) -> int:
        value = self._get(key)
        # `bool` is a subclass of `int`, and `true` is not a count of anything.
        if not isinstance(value, int) or isinstance(value, bool):
            self._fail(key, "an integer", value)
        if minimum is not None and value < minimum:
            self._fail(key, f"an integer >= {minimum}", value)
        return value

    def float_(self, key: str, *, minimum: float | None = None) -> float:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._fail(key, "a number", value)
        if minimum is not None and value < minimum:
            self._fail(key, f"a number >= {minimum}", value)
        return float(value)

    def str_tuple(self, key: str) -> tuple[str, ...]:
        value = self._get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self._fail(key, "a list of strings", value)
        return tuple(value)

    def int_pair(self, key: str) -> tuple[int, int]:
        value = self._get(key)
        ok = (
            isinstance(value, list)
            and len(value) == 2  # noqa: PLR2004 - a pair is two
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
        )
        if not ok:
            self._fail(key, "a list of two integers", value)
        low, high = value
        if low > high:
            self._fail(key, "a pair in ascending order", value)
        return (low, high)

    def float_table(self, key: str) -> Mapping[str, float]:
        """A table of question id -> number, read-only so nothing can edit policy later."""
        inner = self.table(key)
        # File order, not sorted: the report prints these, and the order they are
        # written in is the order they are meant to be read in.
        return MappingProxyType({name: inner.float_(name) for name in inner.keys()})

    def keys(self) -> list[str]:
        return list(self._table)

    def _get(self, key: str) -> Any:  # noqa: ANN401 - the value's type is what we check
        if key not in self._table:
            msg = f"config.toml is missing the key `{self._path}{key}`."
            raise ConfigError(msg)
        return self._table[key]

    def _fail(self, key: str, expected: str, value: object) -> None:
        msg = (
            f"config.toml key `{self._path}{key}` must be {expected}, "
            f"got {value!r} ({type(value).__name__})."
        )
        raise ConfigError(msg)


def _resolve(root: Path, value: str) -> Path:
    """A path from the file, anchored to the project root unless it is absolute."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path)


def parse_config(
    raw: Mapping[str, Any],
    *,
    root: Path,
    known_question_ids: frozenset[str],
) -> Config:
    """Turn a decoded TOML document into a :class:`Config`.

    Args:
        raw: The document, as `tomllib` returned it.
        root: The project root, for resolving the paths in ``[paths]``.
        known_question_ids: Every id in the Jev battery. The thresholds under
            ``[classification]`` are checked against this, because an id that matches
            no question is applied to nothing and reports nothing.

    Raises:
        ConfigError: A key is missing, has the wrong type, or names something unknown.
    """
    doc = _Reader(raw, "")

    viafoura = doc.table("viafoura")
    jev = doc.table("jev")
    article = doc.table("article")
    classification = doc.table("classification")
    api = doc.table("api")
    paths = doc.table("paths")

    model = jev.str_("model")
    weights = classification.float_table("weights")
    exclude_at = classification.float_table("exclude_at")
    flag_at = classification.float_table("flag_at")

    _check_question_ids("exclude_at", exclude_at, known_question_ids)
    _check_question_ids("flag_at", flag_at, known_question_ids)
    _check_weights(weights)

    return Config(
        viafoura=ViafouraConfig(
            section_uuid=viafoura.str_("section_uuid"),
            base_url=viafoura.str_("base_url").rstrip("/"),
            page_size=viafoura.int_("page_size", minimum=1),
            reply_limit=viafoura.int_("reply_limit", minimum=0),
            timeout_seconds=viafoura.float_("timeout_seconds", minimum=0.0),
            max_concurrent_requests=viafoura.int_(
                "max_concurrent_requests",
                minimum=1,
            ),
            max_retries=viafoura.int_("max_retries", minimum=0),
            backoff_initial_seconds=viafoura.float_(
                "backoff_initial_seconds",
                minimum=0.0,
            ),
            backoff_max_seconds=viafoura.float_("backoff_max_seconds", minimum=0.0),
        ),
        jev=JevConfig(
            model=model or None,
            concurrency=jev.int_("concurrency", minimum=1),
            timeout_seconds=jev.float_("timeout_seconds", minimum=0.0),
            requests_per_minute=jev.int_("requests_per_minute", minimum=1),
            tokens_per_second=jev.int_("tokens_per_second", minimum=1),
        ),
        article=ArticleConfig(max_words=article.int_("max_words", minimum=1)),
        classification=ClassificationConfig(
            min_words=classification.int_("min_words", minimum=0),
            max_caps_ratio=classification.float_("max_caps_ratio", minimum=0.0),
            sweet_spot=classification.int_pair("sweet_spot"),
            # Bounded like every other number in the table: these are probabilities,
            # and a negative one would exclude nothing while looking deliberate.
            on_topic_exclude_below=classification.float_(
                "on_topic_exclude_below",
                minimum=0.0,
            ),
            on_topic_flag_below=classification.float_(
                "on_topic_flag_below",
                minimum=0.0,
            ),
            weights=weights,
            exclude_at=exclude_at,
            flag_at=flag_at,
        ),
        api=ApiConfig(
            host=api.str_("host"),
            port=api.int_("port", minimum=1),
            cors_origins=api.str_tuple("cors_origins"),
            default_min_score=api.float_("default_min_score", minimum=0.0),
            max_results=api.int_("max_results", minimum=1),
            min_results=api.int_("min_results", minimum=0),
            lock_timeout_seconds=api.float_("lock_timeout_seconds", minimum=0.0),
        ),
        paths=PathsConfig(
            state_dir=_resolve(root, paths.str_("state_dir")),
            feedback_dir=_resolve(root, paths.str_("feedback_dir")),
            output_dir=_resolve(root, paths.str_("output_dir")),
        ),
    )


def _check_question_ids(
    section: str,
    table: Mapping[str, float],
    known: frozenset[str],
) -> None:
    """Every threshold must name a question that exists.

    Without this a renamed or mistyped id is applied to nothing: the comment it was
    meant to exclude is proposed, and no error is raised anywhere. It is the same
    silent failure the store fingerprint exists to prevent, one layer up.
    """
    unknown = sorted(set(table) - known)
    if unknown:
        msg = (
            f"config.toml `[classification.{section}]` names "
            f"{'questions' if len(unknown) > 1 else 'a question'} that do not exist: "
            f"{', '.join(unknown)}. Known ids: {', '.join(sorted(known))}."
        )
        raise ConfigError(msg)


def _check_weights(weights: Mapping[str, float]) -> None:
    """The weights must be exactly the set `quality_score` combines — no more, no less."""
    missing = sorted(WEIGHT_KEYS - set(weights))
    unknown = sorted(set(weights) - WEIGHT_KEYS)
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown {', '.join(unknown)}")
        msg = (
            f"config.toml `[classification.weights]` must name exactly "
            f"{', '.join(sorted(WEIGHT_KEYS))} ({'; '.join(parts)})."
        )
        raise ConfigError(msg)


def load_config(path: Path, *, known_question_ids: frozenset[str]) -> Config:
    """Read and check one ``config.toml``.

    Raises:
        ConfigError: The file is missing, is not valid TOML, or fails a check above.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = (
            f"No configuration file at {path}. It is committed to the repository; "
            f"restore it rather than running without one."
        )
        raise ConfigError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc
    except OSError as exc:
        msg = f"Could not read {path}: {exc}"
        raise ConfigError(msg) from exc

    return parse_config(
        raw,
        root=path.parent,
        known_question_ids=known_question_ids,
    )
