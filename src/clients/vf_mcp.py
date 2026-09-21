"""Viafoura Comments MCP client.

A thin Python wrapper around the official MCP Python SDK (``mcp``) that talks to the
Viafoura Comments MCP server and pulls the comments of one article (a "content
container" in Viafoura terms).

The server is protected by OAuth 2.0 (authorization code + PKCE, dynamic client
registration). Its sign-in page is a plain HTML form that takes a Viafoura API key, so
this client completes the whole flow headlessly: no browser, no local callback server.
The SDK's ``OAuthClientProvider`` still owns registration, PKCE, token exchange, token
refresh and persistence; this module only supplies the "log in with the API key" step.

Configuration (environment variables, ``.env`` is loaded if present):

- ``VF_MCP_API_KEY`` or ``vf_key``   the Viafoura API key (required)
- ``VF_MCP_URL``                     server URL, default ``https://comments-mcp.viafoura.co/mcp``
- ``VF_MCP_TOKEN_STORE``             where OAuth tokens/client registration are cached,
                                     default ``~/.jev_ai/vf_mcp_tokens.json``
- ``VF_SECTION_UUID``                the site's Viafoura section UUID, used by the public
                                     API fallback; discovered automatically if unset

Identifiers: every method takes the article's ``content_container_uuid`` or its Viafoura
``container_id`` (for the Telegraph that is the page id, e.g. ``A65xRHy7KY6g``). A URL is
**not** accepted: resolving one used to mean fetching the article page and reading its
``vf:container_id`` meta tag, which is scraping and returns HTTP 402 on every paywalled
article. CAPI returns the same id as ``metadata.page-id``; see ``processing.fetch``.

Usage::

    from clients.vf_mcp import ViafouraMCPClient

    async with ViafouraMCPClient() as vf:
        everything = await vf.get_comments(container_uuid)              # all comments
        top10      = await vf.get_comments(container_uuid, limit=10)    # top 10 by likes
        recent     = await vf.get_comments_in_range(container_uuid, since=last_run)

Synchronous convenience wrappers (``fetch_comments``, ``fetch_comments_in_range``) are
provided at the bottom of the module for scripts that are not already running an event
loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid as _uuid
from collections.abc import AsyncIterator, Coroutine, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp import Client
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from mcp.types import CallToolResult

DEFAULT_SERVER_URL = "https://comments-mcp.viafoura.co/mcp"
DEFAULT_TOKEN_STORE = Path.home() / ".jev_ai" / "vf_mcp_tokens.json"
# Viafoura's public Live Comments API. Used only as a fallback to resolve a
# container_id to its UUID, because the MCP server's lookup tools reject every id.
DEFAULT_LIVECOMMENTS_URL = "https://livecomments.viafoura.co"
# The server never actually redirects a browser here; it is only the redirect_uri we
# register and that the authorize endpoint echoes back with the code attached.
_REDIRECT_URI = "http://localhost:8765/callback"
_PAGE_SIZE = 100  # server maximum for get_comments.limit
_REPLY_LIMIT_MAX = 50  # server maximum for get_comments.reply_limit

SortOrder = Literal[
    "newest",
    "oldest",
    "num_likes_desc",
    "num_replies_desc",
    "newest_pick",
    "is_top_comment",
]
RankedBy = Literal["most_liked", "most_replied", "trending"]

_RANKED_BY_TO_SORT: dict[str, SortOrder] = {
    "most_liked": "num_likes_desc",
    "most_replied": "num_replies_desc",
}

# Anything json.loads can produce; what MCP tools hand back once decoded.
type JsonValue = (
    dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None
)


# --------------------------------------------------------------------------- errors


class ViafouraMCPError(RuntimeError):
    """Base error for this client."""


class ContainerNotFoundError(ViafouraMCPError):
    """The server could not resolve the given container identifier."""


class ToolCallError(ViafouraMCPError):
    """The MCP server returned ``is_error`` for a tool call."""

    def __init__(self, tool: str, arguments: dict[str, Any], message: str) -> None:
        super().__init__(f"{tool}({arguments}) failed: {message}")
        self.tool = tool
        self.arguments = arguments
        self.message = message


# --------------------------------------------------------------------------- models


@dataclass(slots=True)
class Comment:
    """One Viafoura comment, top-level or reply, with the raw payload attached."""

    uuid: str
    container_uuid: str
    parent_uuid: str
    thread_uuid: str
    is_reply: bool
    text: str
    created_at: datetime
    actor_uuid: str | None
    likes: int
    dislikes: int
    total_replies: int
    is_pinned: bool
    is_picked: bool
    is_top_comment: bool
    state: str | None
    article_title: str | None
    article_url: str | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Comment:
        container_uuid = payload.get("content_container_uuid", "")
        parent_uuid = payload.get("parent_uuid", "")
        meta = payload.get("metadata") or {}
        ms = payload.get("date_created") or payload.get("time") or 0
        return cls(
            uuid=payload["content_uuid"],
            container_uuid=container_uuid,
            parent_uuid=parent_uuid,
            thread_uuid=payload.get("thread_uuid", ""),
            is_reply=bool(parent_uuid) and parent_uuid != container_uuid,
            text=payload.get("content", "") or "",
            created_at=_from_epoch_ms(ms),
            actor_uuid=payload.get("actor_uuid"),
            likes=int(payload.get("total_likes") or 0),
            dislikes=int(payload.get("total_dislikes") or 0),
            total_replies=int(payload.get("total_replies") or 0),
            is_pinned=bool(payload.get("is_pinned")),
            is_picked=bool(payload.get("is_picked")),
            is_top_comment=bool(payload.get("is_top_comment")),
            state=payload.get("state"),
            article_title=meta.get("origin_title"),
            article_url=meta.get("origin_url"),
            raw=payload,
        )


@dataclass(slots=True)
class CommentPage:
    """One page of ``get_comments`` as returned by the server."""

    comments: list[Comment]
    more_available: bool

    @property
    def top_level(self) -> list[Comment]:
        return [c for c in self.comments if not c.is_reply]


# --------------------------------------------------------------------------- helpers


def _from_epoch_ms(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _to_utc(value: datetime | str | int | float | None) -> datetime | None:
    """Normalise a datetime / ISO-8601 string / epoch seconds to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Heuristic: epoch milliseconds are > 10^11 for any date after 1973.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _text_of(result: CallToolResult) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _resolve_api_key(explicit: str | None) -> str:
    if explicit:
        return explicit
    key = os.environ.get("VF_MCP_API_KEY") or os.environ.get("vf_key")
    if not key:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:  # pragma: no cover - python-dotenv is a project dependency
            pass
        key = os.environ.get("VF_MCP_API_KEY") or os.environ.get("vf_key")
    if not key:
        raise ViafouraMCPError(
            "Viafoura API key not found. Set VF_MCP_API_KEY (or vf_key) in the environment or .env",
        )
    return key


# --------------------------------------------------------------------------- OAuth plumbing


class FileTokenStorage(TokenStorage):
    """Persist OAuth tokens and the dynamic client registration in a JSON file.

    The file is keyed by server URL so one store can serve several servers. It is
    created with owner-only permissions because it holds bearer tokens.
    """

    def __init__(self, path: Path, server_url: str) -> None:
        self._path = path
        self._key = server_url

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.chmod(0o600)
        tmp.replace(self._path)

    def _entry(self) -> dict[str, Any]:
        return self._load().get(self._key, {})

    def _update(self, **fields: object) -> None:
        data = self._load()
        entry = data.get(self._key, {})
        entry.update(fields)
        data[self._key] = entry
        self._save(data)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._entry().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._update(tokens=tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._entry().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._update(client_info=client_info.model_dump(mode="json", exclude_none=True))

    def clear(self) -> None:
        data = self._load()
        data.pop(self._key, None)
        self._save(data)


class _HeadlessApiKeyLogin:
    """Completes the authorize step of the OAuth flow by submitting the API key form.

    The SDK calls ``redirect`` with the authorization URL it would normally open in a
    browser. We POST the sign-in form to that URL instead and capture the redirect
    ``Location`` that carries the authorization code. The SDK then calls ``callback``
    to collect the code, exactly as it would after a browser round trip.
    """

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._result: AuthorizationCodeResult | None = None

    async def redirect(self, authorization_url: str) -> None:
        async with httpx2.AsyncClient(timeout=self._timeout) as http:
            response = await http.post(
                authorization_url,
                data={"api_key": self._api_key},
            )
        if response.status_code not in (301, 302, 303, 307, 308):
            body = response.text.strip()
            hint = (
                " Check that VF_MCP_API_KEY is a valid Viafoura Comments MCP key."
                if response.status_code in (200, 400, 401, 403)
                else ""
            )
            raise ViafouraMCPError(
                f"Viafoura sign-in did not redirect (HTTP {response.status_code}).{hint} Response: {body[:300]}",
            )
        location = response.headers.get("location", "")
        query = parse_qs(urlparse(location).query)
        if "error" in query:
            raise ViafouraMCPError(
                f"Viafoura sign-in refused: {query.get('error_description', query['error'])[0]}",
            )
        code = query.get("code", [None])[0]
        if not code:
            raise ViafouraMCPError(
                f"Viafoura sign-in redirect carried no authorization code: {location}",
            )
        self._result = AuthorizationCodeResult(
            code=code,
            state=query.get("state", [None])[0],
            iss=query.get("iss", [None])[0],
        )

    async def callback(self) -> AuthorizationCodeResult:
        if self._result is None:
            raise ViafouraMCPError(
                "OAuth callback requested before the sign-in step completed",
            )
        result, self._result = self._result, None
        return result


def build_oauth_provider(
    api_key: str,
    server_url: str = DEFAULT_SERVER_URL,
    token_store: Path | str | None = None,
    client_name: str = "jev-ai vf_mcp",
) -> OAuthClientProvider:
    """Create the SDK OAuth provider wired to the headless API-key sign-in."""
    store_path = Path(
        token_store or os.environ.get("VF_MCP_TOKEN_STORE") or DEFAULT_TOKEN_STORE,
    ).expanduser()
    login = _HeadlessApiKeyLogin(api_key)
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name=client_name,
            redirect_uris=[_REDIRECT_URI],  # type: ignore[list-item]
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            # S106 false positive: this names the OAuth auth *method*, not a secret.
            token_endpoint_auth_method="client_secret_post",  # noqa: S106
        ),
        storage=FileTokenStorage(store_path, server_url),
        redirect_handler=login.redirect,
        callback_handler=login.callback,
    )


# --------------------------------------------------------------------------- the client


class ViafouraMCPClient:
    """Async client for the Viafoura Comments MCP server.

    Open it once with ``async with`` and reuse it: the MCP session and the OAuth token
    live for the lifetime of the context. Every public method accepts the article's
    ``content_container_uuid``, its Viafoura ``container_id`` (for the Telegraph this is
    the page id, e.g. ``A65xRHy7KY6g``) or the article URL. Ids and URLs are resolved to
    the UUID once and cached: the MCP lookup tool is tried first, then Viafoura's public
    Live Comments API, which needs the site's ``section_uuid`` (``VF_SECTION_UUID`` or
    discovered from the trending list on first use).
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        server_url: str | None = None,
        token_store: Path | str | None = None,
        client_name: str = "jev-ai vf_mcp",
        request_timeout: float = 300.0,
        section_uuid: str | None = None,
        livecomments_url: str | None = None,
    ) -> None:
        self._api_key = _resolve_api_key(api_key)
        self._server_url = (
            server_url or os.environ.get("VF_MCP_URL") or DEFAULT_SERVER_URL
        )
        self._token_store = token_store
        self._client_name = client_name
        self._request_timeout = request_timeout
        self._section_uuid = section_uuid or os.environ.get("VF_SECTION_UUID")
        self._livecomments_url = (
            livecomments_url
            or os.environ.get("VF_LIVECOMMENTS_URL")
            or DEFAULT_LIVECOMMENTS_URL
        ).rstrip("/")
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._public_http: httpx2.AsyncClient | None = None
        self._container_cache: dict[str, str] = {}  # uuid / id / url -> uuid
        self._container_id_cache: dict[str, str] = {}  # url -> container_id

    # ---- lifecycle -------------------------------------------------------------

    async def __aenter__(self) -> ViafouraMCPClient:
        if self._client is not None:
            raise ViafouraMCPError("ViafouraMCPClient is already open")
        auth = build_oauth_provider(
            self._api_key,
            self._server_url,
            self._token_store,
            self._client_name,
        )
        http = httpx2.AsyncClient(
            auth=auth,
            timeout=httpx2.Timeout(30.0, read=self._request_timeout),
        )
        # Unauthenticated client for Viafoura's public API and for article pages.
        public_http = httpx2.AsyncClient(timeout=httpx2.Timeout(30.0))
        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(http)
            await stack.enter_async_context(public_http)
            client = Client(streamable_http_client(self._server_url, http_client=http))
            await stack.enter_async_context(client)
        except BaseException:
            await stack.aclose()
            raise
        self._stack, self._client, self._public_http = stack, client, public_http
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        stack, self._stack, self._client = self._stack, None, None
        self._public_http = None
        if stack is not None:
            await stack.aclose()

    @property
    def mcp(self) -> Client:
        """The underlying SDK client, for tools this wrapper does not expose."""
        if self._client is None:
            raise ViafouraMCPError(
                "ViafouraMCPClient is not open; use 'async with ViafouraMCPClient() as vf:'",
            )
        return self._client

    # ---- raw tool access -------------------------------------------------------

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> JsonValue:
        """Call any server tool and return its JSON payload (or text if not JSON)."""
        arguments = arguments or {}
        result = await self.mcp.call_tool(name, arguments)
        text = _text_of(result)
        if result.is_error:
            if "not found" in text.lower():
                raise ContainerNotFoundError(text.strip() or "Container not found")
            raise ToolCallError(name, arguments, text.strip() or "unknown error")
        if result.structured_content is not None:
            return result.structured_content
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    async def list_tools(self) -> list[str]:
        return [tool.name for tool in (await self.mcp.list_tools()).tools]

    # ---- containers ------------------------------------------------------------

    async def resolve_container_uuid(self, container: str) -> str:
        """Return the ``content_container_uuid`` for a UUID, a ``container_id`` or an article URL.

        Resolution order: UUID passthrough, cache, the MCP ``get_content_container``
        tool, then Viafoura's public Live Comments API. Raises
        :class:`ContainerNotFoundError` when nothing knows the id.
        """
        if _is_uuid(container):
            return container
        if container in self._container_cache:
            return self._container_cache[container]
        container_id = await self._to_container_id(container)
        if container_id in self._container_cache:
            found = self._container_cache[container_id]
        else:
            found = await self._resolve_via_mcp(container_id)
            if found is None:
                found = await self._resolve_via_public_api(container_id)
            self._container_cache[container_id] = found
        self._container_cache[container] = found
        return found

    async def get_container(self, container: str) -> dict[str, Any]:
        """Container details for a ``container_id`` or article URL.

        Tries the MCP ``get_content_container`` tool first and falls back to the public
        Live Comments API's ``container`` record (id, uuid, counts, settings).
        """
        container_id = await self._to_container_id(container)
        try:
            payload = await self.call_tool(
                "get_content_container",
                {"container_id": container_id},
            )
        except ContainerNotFoundError:
            payload = None
        if isinstance(payload, dict):
            return payload
        if _is_uuid(container_id):
            raise ContainerNotFoundError(
                "Container details need the Viafoura container_id (page id) or the "
                f"article URL; the public API cannot look up the UUID {container_id}",
            )
        details = await self._public_container_details(container_id)
        record = details.get("container")
        return record if isinstance(record, dict) else details

    async def section_uuid(self) -> str:
        """The Viafoura section (site) UUID, configured or discovered from the trending list."""
        if self._section_uuid:
            return self._section_uuid
        payload = await self.call_tool("get_trending_containers", {"limit": 1})
        found = _find_key(payload, "section_uuid")
        if not isinstance(found, str) or not found:
            raise ViafouraMCPError(
                "Could not discover the Viafoura section_uuid; set VF_SECTION_UUID",
            )
        self._section_uuid = found
        return found

    async def _to_container_id(self, container: str) -> str:
        """Pass an id through; refuse a URL.

        Resolving a URL used to mean fetching the article page and reading its
        ``vf:container_id`` meta tag. That is scraping, it breaks on every paywalled
        article (HTTP 402), and it is unnecessary: CAPI returns the same id as
        ``metadata.page-id``. `processing.fetch` reads it from there and passes the id
        in, so nothing in this project needs the page.
        """
        if _is_url(container):
            msg = (
                f"{container!r} is a URL. This client does not fetch article pages. "
                "Resolve the URL to a Telegraph page id first — CAPI returns it as "
                "`metadata.page-id`, which `processing.fetch.fetch_article` puts on "
                "`Article.page_id` — and pass that id instead."
            )
            raise ViafouraMCPError(msg)
        return container

    async def _resolve_via_mcp(self, container_id: str) -> str | None:
        try:
            payload = await self.call_tool(
                "get_content_container",
                {"container_id": container_id},
            )
        except ContainerNotFoundError:
            return None
        found = _find_key(payload, "content_container_uuid")
        return found if isinstance(found, str) and _is_uuid(found) else None

    async def _public_container_details(self, container_id: str) -> dict[str, Any]:
        """``GET /v4/livecomments/{section_uuid}?container_id=…&limit=0`` on the public API."""
        if self._public_http is None:
            raise ViafouraMCPError("ViafouraMCPClient is not open")
        section = await self.section_uuid()
        response = await self._public_http.get(
            f"{self._livecomments_url}/v4/livecomments/{section}",
            params={"container_id": container_id, "limit": 0},
        )
        if response.status_code == 404:
            raise ContainerNotFoundError(
                f"Viafoura knows no container with container_id {container_id!r}",
            )
        if response.status_code != 200:
            raise ViafouraMCPError(
                f"Viafoura public API returned HTTP {response.status_code} for "
                f"container_id {container_id!r}: {response.text[:200]}",
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ViafouraMCPError("Unexpected payload from the Viafoura public API")
        return payload

    async def _resolve_via_public_api(self, container_id: str) -> str:
        details = await self._public_container_details(container_id)
        found = _find_key(details, "content_container_uuid")
        if not isinstance(found, str) or not _is_uuid(found):
            raise ContainerNotFoundError(
                f"Viafoura returned no content_container_uuid for {container_id!r}",
            )
        return found

    # ---- paging primitive ------------------------------------------------------

    async def fetch_page(
        self,
        container: str,
        *,
        limit: int = _PAGE_SIZE,
        sorted_by: SortOrder = "newest",
        include_replies: bool = True,
        reply_limit: int = _REPLY_LIMIT_MAX,
        starting_from: str | None = None,
        filtered_by: Literal["none", "is_picked", "is_top_content"] | None = None,
    ) -> CommentPage:
        """One raw page of ``get_comments``. ``limit`` counts top-level comments only."""
        container_uuid = await self.resolve_container_uuid(container)
        args: dict[str, Any] = {
            "content_container_uuid": container_uuid,
            "limit": max(1, min(limit, _PAGE_SIZE)),
            "reply_limit": max(0, min(reply_limit, _REPLY_LIMIT_MAX))
            if include_replies
            else 0,
            "sorted_by": sorted_by,
        }
        if starting_from:
            args["starting_from"] = starting_from
        if filtered_by and filtered_by != "none":
            args["filtered_by"] = filtered_by
        payload = await self.call_tool("get_comments", args)
        items = payload.get("contents", []) if isinstance(payload, dict) else []
        return CommentPage(
            comments=[Comment.from_payload(item) for item in items],
            more_available=bool(payload.get("more_available"))
            if isinstance(payload, dict)
            else False,
        )

    async def iter_comments(
        self,
        container: str,
        *,
        sorted_by: SortOrder = "newest",
        include_replies: bool = True,
        reply_limit: int = _REPLY_LIMIT_MAX,
        filtered_by: Literal["none", "is_picked", "is_top_content"] | None = None,
        max_pages: int | None = None,
    ) -> AsyncIterator[Comment]:
        """Stream every comment of an article, page by page, in server order.

        Replies are yielded right after their parent when ``include_replies`` is on.
        The pagination cursor is the last top-level comment of the previous page.
        """
        cursor: str | None = None
        pages = 0
        seen: set[str] = set()
        while True:
            page = await self.fetch_page(
                container,
                limit=_PAGE_SIZE,
                sorted_by=sorted_by,
                include_replies=include_replies,
                reply_limit=reply_limit,
                starting_from=cursor,
                filtered_by=filtered_by,
            )
            for comment in page.comments:
                if comment.uuid in seen:
                    continue
                seen.add(comment.uuid)
                yield comment
            pages += 1
            top_level = page.top_level
            if not page.more_available or not top_level:
                return
            if max_pages is not None and pages >= max_pages:
                return
            next_cursor = top_level[-1].uuid
            if next_cursor == cursor:
                return  # defensive: the server did not advance
            cursor = next_cursor

    # ---- the three retrieval modes ---------------------------------------------

    async def get_all_comments(
        self,
        container: str,
        *,
        include_replies: bool = True,
        sorted_by: SortOrder = "newest",
        max_pages: int | None = None,
    ) -> list[Comment]:
        """Every comment on the article (top-level plus nested replies)."""
        return [
            c
            async for c in self.iter_comments(
                container,
                sorted_by=sorted_by,
                include_replies=include_replies,
                max_pages=max_pages,
            )
        ]

    async def get_top_comments(
        self,
        container: str,
        n: int,
        *,
        ranked_by: RankedBy = "most_liked",
    ) -> list[Comment]:
        """The top ``n`` top-level comments ranked by likes, replies or Viafoura's trending signal.

        Replies are never included here: "top N" means N comments. ``most_liked`` and
        ``most_replied`` page through ``get_comments`` with the matching sort, which works
        with either identifier. ``trending`` is only offered by the server's
        ``get_top_comments`` tool, which needs a Viafoura ``container_id``.
        """
        if n <= 0:
            return []
        if ranked_by == "trending":
            payload = await self.call_tool(
                "get_top_comments",
                {
                    "container_id": container,
                    "ranked_by": "trending",
                    "limit": min(n, _PAGE_SIZE),
                },
            )
            items = (
                _find_key(payload, "contents") or _find_key(payload, "comments") or []
            )
            return [
                c
                for c in (Comment.from_payload(item) for item in items)
                if not c.is_reply
            ][:n]
        sort = _RANKED_BY_TO_SORT[ranked_by]
        out: list[Comment] = []
        async for comment in self.iter_comments(
            container,
            sorted_by=sort,
            include_replies=False,
        ):
            if comment.is_reply:
                continue
            out.append(comment)
            if len(out) >= n:
                break
        return out

    async def get_comments_in_range(
        self,
        container: str,
        *,
        since: datetime | str | int | float | None = None,
        until: datetime | str | int | float | None = None,
        include_replies: bool = True,
        max_pages: int | None = None,
    ) -> list[Comment]:
        """Comments created in ``[since, until)``, newest first.

        The server has no time filter, so this pages newest-first and stops at the first
        top-level comment older than ``since`` (pinned comments are skipped for that test
        because the server floats them to the top regardless of date). Replies are
        included only when they hang off a top-level comment inside the window, since the
        server nests them under their parent rather than sorting them on their own.
        """
        since_dt, until_dt = _to_utc(since), _to_utc(until)
        if since_dt is None and until_dt is None:
            return await self.get_all_comments(
                container,
                include_replies=include_replies,
                max_pages=max_pages,
            )

        out: list[Comment] = []
        async for comment in self.iter_comments(
            container,
            sorted_by="newest",
            include_replies=include_replies,
            max_pages=max_pages,
        ):
            if since_dt is not None and comment.created_at < since_dt:
                if not comment.is_reply and not comment.is_pinned:
                    break  # everything after this top-level comment is older still
                continue
            if until_dt is not None and comment.created_at >= until_dt:
                continue
            out.append(comment)
        return out

    async def get_comments(
        self,
        container: str,
        limit: int | None = None,
        *,
        ranked_by: RankedBy = "most_liked",
        since: datetime | str | int | float | None = None,
        until: datetime | str | int | float | None = None,
        include_replies: bool = True,
    ) -> list[Comment]:
        """The one-call entry point.

        - no ``limit`` and no time bounds: every comment on the article, replies included
          unless ``include_replies=False``;
        - ``limit=N``: the top N top-level comments ranked by ``ranked_by`` (default likes),
          never replies;
        - ``since``/``until``: comments posted inside that window, newest first (``limit``
          then caps the number returned).
        """
        if since is not None or until is not None:
            comments = await self.get_comments_in_range(
                container,
                since=since,
                until=until,
                include_replies=include_replies,
            )
            return comments[:limit] if limit is not None else comments
        if limit is None:
            return await self.get_all_comments(
                container,
                include_replies=include_replies,
            )
        return await self.get_top_comments(container, limit, ranked_by=ranked_by)


def _find_key(payload: JsonValue, key: str) -> JsonValue:
    """Depth-first search for ``key`` in nested dicts/lists."""
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


# --------------------------------------------------------------------------- sync helpers


def _run[T](coro: Coroutine[object, object, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()  # avoid a "coroutine was never awaited" warning on the error path
    raise ViafouraMCPError(
        "An event loop is already running; use the async ViafouraMCPClient directly",
    )


def fetch_comments(
    container: str,
    limit: int | None = None,
    *,
    ranked_by: RankedBy = "most_liked",
    since: datetime | str | int | float | None = None,
    until: datetime | str | int | float | None = None,
    include_replies: bool = True,
    api_key: str | None = None,
) -> list[Comment]:
    """Synchronous one-shot version of :meth:`ViafouraMCPClient.get_comments`."""

    async def go() -> list[Comment]:
        async with ViafouraMCPClient(api_key) as vf:
            return await vf.get_comments(
                container,
                limit,
                ranked_by=ranked_by,
                since=since,
                until=until,
                include_replies=include_replies,
            )

    return _run(go())


def fetch_comments_in_range(
    container: str,
    since: datetime | str | int | float | None,
    until: datetime | str | int | float | None = None,
    *,
    include_replies: bool = True,
    api_key: str | None = None,
) -> list[Comment]:
    """Synchronous one-shot version of :meth:`ViafouraMCPClient.get_comments_in_range`."""

    async def go() -> list[Comment]:
        async with ViafouraMCPClient(api_key) as vf:
            return await vf.get_comments_in_range(
                container,
                since=since,
                until=until,
                include_replies=include_replies,
            )

    return _run(go())


def comments_to_dicts(comments: Iterable[Comment]) -> list[dict[str, Any]]:
    """Flatten comments to plain dicts (ISO timestamps), handy for JSON/CSV export."""
    rows = []
    for c in comments:
        rows.append(
            {
                "uuid": c.uuid,
                "container_uuid": c.container_uuid,
                "parent_uuid": c.parent_uuid,
                "is_reply": c.is_reply,
                "created_at": c.created_at.isoformat(),
                "actor_uuid": c.actor_uuid,
                "likes": c.likes,
                "dislikes": c.dislikes,
                "total_replies": c.total_replies,
                "is_pinned": c.is_pinned,
                "is_picked": c.is_picked,
                "is_top_comment": c.is_top_comment,
                "article_title": c.article_title,
                "article_url": c.article_url,
                "text": c.text,
            },
        )
    return rows


__all__ = [
    "Comment",
    "CommentPage",
    "ContainerNotFoundError",
    "FileTokenStorage",
    "ToolCallError",
    "ViafouraMCPClient",
    "ViafouraMCPError",
    "build_oauth_provider",
    "comments_to_dicts",
    "fetch_comments",
    "fetch_comments_in_range",
]
