"""Application settings: read the environment and ``config.toml`` once at startup.

Nothing here re-reads either source afterwards. ``load_settings()`` is called once by
whoever owns the process lifecycle, and every other module calls ``get_settings()``,
which is a plain attribute lookup.

Command line (today)::

    def main() -> None:
        settings = load_settings()
        ...

FastAPI (later)::

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        load_settings()          # once, before the first request
        yield

    @app.post("/classify")
    async def classify(...):
        settings = get_settings()   # no I/O, no environment access

Functions that need settings take them as an argument rather than calling
``get_settings()`` themselves. That keeps them pure, testable with a hand-built
``Settings``, and identical whether a CLI or a web request is driving them.

**Two sources, one rule: no key appears in both.**

``.env`` holds secrets and the endpoints that differ between accounts. ``config.toml``
holds everything that used to be a module constant — thresholds, weights, limits, paths.
A value lives in exactly one of them, so there is never a precedence question to answer.

The single documented exception is ``HOST`` and ``PORT``, which override ``[api] host``
and ``[api] port``. Cloud Run injects ``PORT`` and will not route to a container bound to
loopback, so a deployment moves off ``127.0.0.1`` without editing a committed file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

from processing.config import (
    ApiConfig,
    ArticleConfig,
    ClassificationConfig,
    Config,
    ConfigError,
    JevConfig,
    PathsConfig,
    ViafouraConfig,
    load_config,
)
from processing.questions import BATTERY

# Field name -> the environment variables it may come from, in order of preference.
# Aliases exist because the project's .env predates this module.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "capi_url": ("CAPI_URL",),
    "content_reader_apigee_key": ("CONTENT_READER_APIGEE_KEY",),
    "typesafe_api_key": ("TYPESAFE_API_KEY", "jev_api_key"),
    # Viafoura is deliberately absent. Its read endpoints are public, so this project
    # holds no Viafoura credential at all; `[viafoura] section_uuid` is the only thing
    # it needs, and that is not a secret.
}
_OPTIONAL: dict[str, tuple[str, ...]] = {
    "environment": ("ENVIRONMENT",),
}

# src/processing/settings.py -> the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.toml"


class SettingsError(RuntimeError):
    """Settings are missing, are invalid, or were used before startup loaded them."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the application was configured with, from both sources.

    Frozen, so nothing can change configuration half way through a run. The three
    top-level strings are secrets or account endpoints and come from ``.env``; the
    sections below come from ``config.toml``. Never log or print a ``Settings``.
    """

    capi_url: str
    content_reader_apigee_key: str
    typesafe_api_key: str
    viafoura: ViafouraConfig
    jev: JevConfig
    article: ArticleConfig
    classification: ClassificationConfig
    api: ApiConfig
    paths: PathsConfig
    environment: str = "dev"

    def __repr__(self) -> str:
        """Redacted, so an accidental log line or traceback cannot leak a key."""
        return (
            f"Settings(capi_url={self.capi_url!r}, environment={self.environment!r}, "
            f"section_uuid={self.viafoura.section_uuid!r}, secrets=<redacted>)"
        )


_settings: Settings | None = None


def _first_env(names: tuple[str, ...]) -> str | None:
    """The first of these variables that is set to something, or None.

    Whitespace-only counts as unset. A variable exported as "" or " " is a mistake
    rather than a value, and treating it as one would let a key made of spaces through
    the required-variable check below and on to the API.
    """
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _apply_host_port_override(api: ApiConfig) -> ApiConfig:
    """The one place the environment may overrule ``config.toml``.

    Cloud Run injects ``PORT`` and routes only to a container listening on every
    interface, so a deployment sets ``HOST=0.0.0.0`` and takes whatever ``PORT`` it is
    given. Locally neither variable is set and the committed loopback default stands.
    """
    host = _first_env(("HOST",))
    raw_port = _first_env(("PORT",))
    port = api.port
    if raw_port is not None:
        try:
            port = int(raw_port)
        except ValueError as exc:
            msg = f"PORT must be a whole number, got {raw_port!r}."
            raise SettingsError(msg) from exc
        if not 1 <= port <= 65535:  # noqa: PLR2004 - the range of a TCP port
            msg = f"PORT must be between 1 and 65535, got {port}."
            raise SettingsError(msg)
    return replace(api, host=host or api.host, port=port)


def load_settings(
    *,
    reload: bool = False,
    config_path: Path | None = None,
) -> Settings:
    """Read the environment and ``config.toml`` once, and cache the result.

    Call this at startup, from ``main()`` or a FastAPI lifespan handler.

    Args:
        reload: Re-read both sources even if settings were already loaded. Useful in
            tests; production startup calls this exactly once.
        config_path: Read this file instead of the project's ``config.toml``. For tests.

    Returns:
        The cached :class:`Settings`.

    Raises:
        SettingsError: A required variable is missing, or the configuration file is
            missing, malformed or says something impossible. Missing variables are all
            named at once, so a single fix clears them.
    """
    global _settings
    if _settings is not None and not reload:
        return _settings

    load_dotenv()

    values: dict[str, str] = {}
    missing: list[str] = []
    for field_name, env_names in _REQUIRED.items():
        value = _first_env(env_names)
        if value is None:
            missing.append(" or ".join(env_names))
        else:
            values[field_name] = value

    if missing:
        raise SettingsError(
            "Missing required settings: "
            + ", ".join(missing)
            + ". Add them to .env or the environment.",
        )

    for field_name, env_names in _OPTIONAL.items():
        value = _first_env(env_names)
        if value is not None:
            values[field_name] = value

    config = _load_config(config_path or CONFIG_PATH)

    _settings = Settings(
        **values,
        viafoura=config.viafoura,
        jev=config.jev,
        article=config.article,
        classification=config.classification,
        api=_apply_host_port_override(config.api),
        paths=config.paths,
    )
    return _settings


def _load_config(path: Path) -> Config:
    """Read the file, checking its question ids against the battery as it goes.

    The battery is passed in here rather than imported by ``config.py`` so that module
    stays free of project imports. A threshold naming a question that does not exist is
    refused: it would otherwise be applied to nothing, excluding no comment and
    reporting no error.
    """
    try:
        return load_config(path, known_question_ids=frozenset(BATTERY))
    except ConfigError as exc:
        raise SettingsError(str(exc)) from exc


def get_settings() -> Settings:
    """Return the settings loaded at startup.

    Raises:
        SettingsError: ``load_settings()`` has not run yet.
    """
    if _settings is None:
        raise SettingsError(
            "Settings have not been loaded. Call load_settings() at startup "
            "(in main(), or in the FastAPI lifespan handler).",
        )
    return _settings
