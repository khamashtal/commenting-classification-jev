"""Application settings: read the environment once at startup, hold it in memory.

Nothing here re-reads ``.env`` after startup. ``load_settings()`` is called once by
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
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Field name -> the environment variables it may come from, in order of preference.
# Aliases exist because the project's .env predates this module.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "capi_url": ("CAPI_URL",),
    "content_reader_apigee_key": ("CONTENT_READER_APIGEE_KEY",),
    "typesafe_api_key": ("TYPESAFE_API_KEY", "api_key"),
    "viafoura_api_key": ("VF_MCP_API_KEY", "vf_key"),
}
_OPTIONAL: dict[str, tuple[str, ...]] = {
    "vf_section_uuid": ("VF_SECTION_UUID",),
    "environment": ("ENVIRONMENT",),
}


class SettingsError(RuntimeError):
    """Settings are missing, or were used before startup loaded them."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Every external credential and endpoint the application needs.

    Frozen, so nothing can change configuration half way through a run. Values are
    secrets: never log or print a ``Settings`` instance.
    """

    capi_url: str
    content_reader_apigee_key: str
    typesafe_api_key: str
    viafoura_api_key: str
    vf_section_uuid: str | None = None
    environment: str = "dev"

    def __repr__(self) -> str:
        """Redacted, so an accidental log line or traceback cannot leak a key."""
        return (
            f"Settings(capi_url={self.capi_url!r}, environment={self.environment!r}, "
            f"vf_section_uuid={self.vf_section_uuid!r}, secrets=<redacted>)"
        )


_settings: Settings | None = None


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


def load_settings(*, reload: bool = False) -> Settings:
    """Read the environment once and cache the result. Call this at startup.

    Args:
        reload: Re-read the environment even if settings were already loaded. Only
            useful in tests; production startup calls this exactly once.

    Returns:
        The cached :class:`Settings`.

    Raises:
        SettingsError: One or more required variables are missing. The message names
            all of them at once, so a single fix clears it.
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

    _settings = Settings(**values)
    return _settings


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
