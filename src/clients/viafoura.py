"""Viafoura Live Comments client: the public REST API, over plain HTTP, with no auth.

This replaces the Comments MCP server entirely. Everything this project needs is
available anonymously:

===========================  ====================================================
Resolve a page id to a uuid  ``GET /v4/livecomments/{section}?container_id=…&limit=0``
The thread                   ``GET /v4/livecomments/{section}/{container_uuid}/comments``
Article discovery            ``GET /v4/livecomments/{section}/trending``
===========================  ====================================================

**No API key is required to read.** The OpenAPI definition marks both read endpoints
``{"TokenInCookie": ["optional"]}`` and ``{"SignedRequest": ["optional"]}``; a token only
personalises the response. So the Viafoura API key, the OAuth authorization-code + PKCE
dance, the dynamic client registration, the headless form login and the on-disk token
cache are all gone, and with them a secret this project no longer has to handle.

Writes are a different matter and stay out of scope: pinning needs
``{"TokenInCookie": ["mod"]}``, a JWT belonging to a Viafoura account holding the
moderator role — the same role that can delete comments and ban users.

Identifiers
-----------
Every method takes the article's ``content_container_uuid`` or its Viafoura
``container_id`` (for the Telegraph, the page id, e.g. ``A65xRHy7KY6g``). **A URL is
refused.** Resolving one used to mean fetching the article page and reading its
``vf:container_id`` meta tag, which is scraping and returns HTTP 402 on every paywalled
article. CAPI returns the same id as ``metadata.page-id``; ``processing.fetch`` reads it
from there.

Two traps, both silent
----------------------
The API accepts unknown query parameters without complaint, so both of these return
HTTP 200 and plausible-looking data while doing the wrong thing. Both cost real
debugging time, and the guards against them are load-bearing, not defensive padding.

1. **``starting_from`` is ignored on the flat ``?container_id=…`` form**, which returns
   page 1 forever. Paging therefore happens only on the nested path, which is why
   :meth:`ViafouraClient.fetch_page` demands a uuid rather than accepting any reference.
2. **An unknown cursor is silently ignored.** Comment uuids are genuinely UUIDv7 — the
   embedded 48-bit millisecond timestamp matches ``date_created`` exactly — so it looks
   possible to fabricate cursors at chosen times and fetch slices of one thread
   concurrently. The server resolves ``starting_from`` by uuid lookup, and an unknown
   uuid falls back to page 1 with HTTP 200. A parallel pager built that way returns the
   same page N times and looks like it worked. The ``seen`` set and the "cursor did not
   advance" check in :meth:`ViafouraClient.iter_comments` are what catch it.

Paging one article cannot be parallelised: ``offset``, ``page``, ``after`` and ``cursor``
are all ignored, and cursors cannot be synthesised. Paging across *different* articles
shares no cursor and parallelises freely, which is what the concurrency bound here is
for.

Usage::

    async with aiohttp.ClientSession() as session:
        vf = ViafouraClient(session, settings.viafoura)
        comments = await vf.get_comments(page_id)

The session is passed in rather than created: it is shared with CAPI, so the process
keeps one connection pool, and its lifetime belongs to whoever owns the process — a
``main()`` or a FastAPI lifespan handler. This client owns no resource of its own and so
is deliberately not a context manager; there is nothing to close.
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid as _uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import aiohttp

from processing.config import ViafouraConfig
from processing.log_config import logger

# Server maxima, both established by measurement rather than from the documentation.
# `limit` is documented as having no maximum; `limit=101` returns HTTP 400.
PAGE_SIZE_MAX = 100
REPLY_LIMIT_MAX = 50
# `limit` counts top-level comments only, so `reply_limit` never changes the page count.

# The trending endpoint's own maxima.
TRENDING_LIMIT_MAX = 1000
TRENDING_WINDOW_HOURS_MAX = 48
TRENDING_ARTICLE_DAYS_MAX = 30

SortOrder = Literal[
    "newest",
    "oldest",
    "num_likes_desc",
    "num_replies_desc",
    "newest_pick",
    "is_top_comment",
]
CommentFilter = Literal["none", "is_picked", "is_top_content"]

# Named, because a bare 200 or 404 in a comparison reads as a magic number and the
# linter is right to say so.
_OK = 200
_NOT_FOUND = 404
# Worth another go: a timeout, "too early", throttling, and the 5xx family. Anything
# else — a 400 from a bad parameter, a 403 — will fail identically however often it is
# retried, and retrying only delays the error that explains the problem.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


# --------------------------------------------------------------------------- errors


class ViafouraError(RuntimeError):
    """Base error for this client."""


class ContainerNotFoundError(ViafouraError):
    """Viafoura knows no container with the given identifier."""


class ViafouraHTTPError(ViafouraError):
    """The API answered with a status this client cannot use.

    Carries the status and a short excerpt of the body. The excerpt matters: Viafoura's
    errors are plain-text Vert.x exceptions rather than JSON, so the status alone is
    often not enough to tell a bad parameter from a missing one.
    """

    def __init__(
        self,
        status: int,
        url: str,
        body: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"Viafoura returned HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body
        self.retry_after = retry_after
        """Seconds the server asked us to wait, if it sent a usable `Retry-After`."""


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
        """Parse one comment. The public API's payload is identical to the MCP one."""
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

    @property
    def is_edited(self) -> bool:
        """Whether the author has edited this comment since posting.

        Worth knowing because the store assumes a comment's text never changes, which is
        the assumption that makes cached Jev answers safe. An edited comment breaks it.
        Nothing acts on this yet; it is surfaced so the assumption stays visible.
        """
        return bool(self.raw.get("is_edited"))


@dataclass(slots=True)
class CommentPage:
    """One page of comments, exactly as the server returned it."""

    comments: list[Comment]
    more_available: bool

    @property
    def top_level(self) -> list[Comment]:
        return [c for c in self.comments if not c.is_reply]


@dataclass(slots=True)
class TrendingArticle:
    """One article from the trending endpoint, for the calibration harvest.

    Carries ``content_container_uuid`` directly, so discovering an article and reading
    its thread no longer needs a resolution hop in between.
    """

    container_id: str
    container_uuid: str
    url: str | None
    title: str | None
    published_at: datetime | None
    total_comments: int
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TrendingArticle:
        meta = payload.get("metadata") or {}
        published = payload.get("date_published")
        return cls(
            container_id=payload.get("container_id", ""),
            container_uuid=payload.get("content_container_uuid", ""),
            url=payload.get("origin_url") or meta.get("origin_url"),
            title=payload.get("origin_title") or meta.get("origin_title"),
            published_at=_from_epoch_ms(published) if published else None,
            total_comments=int(payload.get("total_visible_contents") or 0),
            raw=payload,
        )


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


def is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _retry_after_seconds(header: str | None, cap: float) -> float | None:
    """Seconds to wait from a ``Retry-After`` header, capped.

    Only the delta-seconds form is honoured. The HTTP-date form is legal but rare here,
    and mis-parsing one into a very long sleep is worse than falling back to the normal
    backoff. The cap exists for the same reason: a header is a hint from a server that
    is already misbehaving, not an instruction to stall the whole run.
    """
    if not header:
        return None
    try:
        seconds = float(header.strip())
    except ValueError:
        return None
    return min(max(seconds, 0.0), cap)


# --------------------------------------------------------------------------- the client


class ViafouraClient:
    """Reads one Telegraph section's comments from Viafoura's public API.

    Build one at startup and pass it down. It holds no connection of its own — the
    ``aiohttp.ClientSession`` is shared with CAPI — only a resolution cache and a
    concurrency bound.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        config: ViafouraConfig,
    ) -> None:
        self._session = session
        self._config = config
        self._base_url = config.base_url.rstrip("/")
        self._section = config.section_uuid
        # Explicit rather than a single total: a stalled read and a refused connection
        # are different failures, and `total` alone cannot distinguish them.
        self._timeout = aiohttp.ClientTimeout(
            total=config.timeout_seconds,
            connect=min(10.0, config.timeout_seconds),
            sock_read=config.timeout_seconds,
        )
        # Clamped here rather than trusted: the file can ask for 500, the server
        # refuses anything over 100 with an HTTP 400, and a config that quietly means
        # something other than what it says is worse than one that is corrected loudly.
        self._page_size = max(1, min(config.page_size, PAGE_SIZE_MAX))
        self._reply_limit = max(0, min(config.reply_limit, REPLY_LIMIT_MAX))
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._uuid_cache: dict[str, str] = {}

    # ---- transport -------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any]) -> Any:  # noqa: ANN401
        """One GET, retrying the statuses that are worth retrying.

        No rate limit is documented anywhere for this API, so the policy is deliberately
        conservative: a handful of attempts, exponential backoff with full jitter, and
        `Retry-After` honoured when the server sends one. Jitter matters because the
        harvest will run many articles at once, and a fleet that all backs off by
        exactly the same amount simply collides again.

        The sleep happens outside the concurrency semaphore on purpose: a request that
        is waiting must not hold a slot another article could be using.
        """
        url = f"{self._base_url}{path}"
        delay = self._config.backoff_initial_seconds
        wait = 0.0

        for attempt in range(self._config.max_retries + 1):
            if attempt:
                await asyncio.sleep(wait)
            last = attempt == self._config.max_retries
            try:
                return await self._attempt(url, params)
            except ViafouraHTTPError as exc:
                # The last attempt re-raises the real error rather than a stored copy,
                # so the traceback points at the request that actually failed.
                if exc.status not in _RETRYABLE_STATUSES or last:
                    raise
                retry_after, reason = exc.retry_after, f"HTTP {exc.status}"
            except (aiohttp.ClientError, TimeoutError) as exc:
                # A reset connection, a DNS blip or a read timeout never reaches a
                # status, so the clause above cannot see it. Paging a 4,000-comment
                # thread keeps a connection busy for fifteen seconds, which is exactly
                # the window where one of these lands; without this, a single blip
                # aborts the whole walk.
                if last:
                    msg = (
                        f"Viafoura request to {url} failed after "
                        f"{attempt + 1} attempts: {type(exc).__name__}: {exc}"
                    )
                    raise ViafouraError(msg) from exc
                retry_after, reason = None, type(exc).__name__

            # Full jitter: uniform over [0, delay], not delay give or take a little. It
            # spreads a synchronised fleet far better than equal-sized waits do, which
            # matters once the harvest runs many articles at once.
            wait = (
                retry_after if retry_after is not None else random.uniform(0.0, delay)  # noqa: S311 - jitter, not a secret
            )
            delay = min(delay * 2, self._config.backoff_max_seconds)
            logger.warning(
                "Viafoura %s on %s; retry %d of %d in %.1fs",
                reason,
                path,
                attempt + 1,
                self._config.max_retries,
                wait,
            )
        return None  # unreachable: max_retries >= 0, so the loop always runs once

    async def _attempt(self, url: str, params: dict[str, Any]) -> Any:  # noqa: ANN401
        async with self._semaphore:
            async with self._session.get(
                url,
                params=params,
                timeout=self._timeout,
            ) as response:
                # Read as text first: Viafoura's errors are plain-text Vert.x
                # exceptions, not JSON, so `response.json()` would raise the wrong
                # error and hide the status that explains what went wrong.
                body = await response.text()
                if response.status == _NOT_FOUND:
                    msg = f"Viafoura knows nothing at {url} with {params}"
                    raise ContainerNotFoundError(msg)
                if response.status != _OK:
                    raise ViafouraHTTPError(
                        response.status,
                        url,
                        body,
                        retry_after=_retry_after_seconds(
                            response.headers.get("Retry-After"),
                            self._config.backoff_max_seconds,
                        ),
                    )
                return _decode(body, url)

    # ---- containers ------------------------------------------------------------

    async def resolve_container_uuid(self, container: str) -> str:
        """Return the ``content_container_uuid`` for a uuid or a Viafoura ``container_id``.

        A uuid passes straight through, an id is looked up once and cached, and a URL is
        refused outright — see the module docstring on why no page is ever fetched.
        """
        if is_uuid(container):
            return container
        container_id = self._require_id(container)
        cached = self._uuid_cache.get(container_id)
        if cached is not None:
            return cached

        details = await self.container_details(container_id)
        found = details.get("content_container_uuid")
        if not isinstance(found, str) or not is_uuid(found):
            msg = (
                f"Viafoura returned no content_container_uuid for {container_id!r}; "
                f"got {found!r}"
            )
            raise ContainerNotFoundError(msg)
        self._uuid_cache[container_id] = found
        return found

    async def container_details(self, container_id: str) -> dict[str, Any]:
        """The container record: its uuid and the visible/pinned/picked counts.

        ``limit=0`` asks for no comments, which makes this the cheap way to ask how many
        comments — and how many *pinned* comments — an article has before deciding to
        download the thread. That is what makes a calibration harvest affordable.

        **The counts are approximate.** Measured across three live threads on
        2026-09-22, ``total_visible_content`` ran 0.6–0.9% above what a complete walk
        returns (4715 vs 4689 visible, 3628 vs 3603, 3622 vs 3591), in every case after
        the walk had ended on the server's own ``more_available=false``. Treat it as a
        size estimate for deciding whether an article is worth downloading, never as an
        assertion that a walk was complete.

        Note this is the flat form of the endpoint, where `starting_from` is silently
        ignored (trap 1). It is only ever used for counts, never for paging.
        """
        payload = await self._get(
            f"/v4/livecomments/{self._section}",
            {"container_id": self._require_id(container_id), "limit": 0},
        )
        if not isinstance(payload, dict):
            msg = f"Unexpected payload for container_id {container_id!r}: {payload!r:.200}"
            raise ViafouraError(msg)
        record = payload.get("container")
        return record if isinstance(record, dict) else payload

    def _require_id(self, container: str) -> str:
        """Pass an identifier through; refuse a URL.

        Resolving a URL used to mean fetching the article page and reading its
        ``vf:container_id`` meta tag. That is scraping, it returns HTTP 402 on every
        paywalled article, and it is unnecessary: CAPI returns the same id as
        ``metadata.page-id``, which ``processing.fetch`` puts on ``Article.page_id``.
        """
        if _is_url(container):
            msg = (
                f"{container!r} is a URL. This client does not fetch article pages. "
                "Resolve the URL to a Telegraph page id first \u2014 CAPI returns it as "
                "`metadata.page-id`, which `processing.fetch.fetch_article` puts on "
                "`Article.page_id` \u2014 and pass that id instead."
            )
            raise ViafouraError(msg)
        return container

    # ---- paging ----------------------------------------------------------------

    async def fetch_page(
        self,
        container_uuid: str,
        *,
        limit: int | None = None,
        sorted_by: SortOrder = "newest",
        reply_limit: int | None = None,
        starting_from: str | None = None,
        filtered_by: CommentFilter | None = None,
    ) -> CommentPage:
        """One page of the thread. ``limit`` counts top-level comments only.

        Takes a **uuid**, not any identifier, and the signature is strict about it on
        purpose: this is the nested path, and it is the only form on which
        ``starting_from`` works. On the flat ``?container_id=`` form the cursor is
        accepted and ignored, and paging silently returns page 1 for ever (trap 1).
        """
        if not is_uuid(container_uuid):
            msg = (
                f"fetch_page needs a content_container_uuid, got {container_uuid!r}. "
                "Call resolve_container_uuid() first: paging only works on the nested "
                "path, and the flat one ignores the cursor without saying so."
            )
            raise ViafouraError(msg)

        params: dict[str, Any] = {
            "limit": self._page_size
            if limit is None
            else max(1, min(limit, PAGE_SIZE_MAX)),
            "reply_limit": self._reply_limit
            if reply_limit is None
            else max(0, min(reply_limit, REPLY_LIMIT_MAX)),
            "sorted_by": sorted_by,
        }
        if starting_from:
            params["starting_from"] = starting_from
        if filtered_by and filtered_by != "none":
            params["filtered_by"] = filtered_by

        payload = await self._get(
            f"/v4/livecomments/{self._section}/{container_uuid}/comments",
            params,
        )
        if not isinstance(payload, dict):
            msg = f"Unexpected comments payload for {container_uuid}: {payload!r:.200}"
            raise ViafouraError(msg)
        items = payload.get("contents") or []
        return CommentPage(
            comments=[Comment.from_payload(item) for item in items],
            more_available=bool(payload.get("more_available")),
        )

    async def iter_comments(
        self,
        container: str,
        *,
        sorted_by: SortOrder = "newest",
        include_replies: bool = True,
        reply_limit: int | None = None,
        filtered_by: CommentFilter | None = None,
        max_pages: int | None = None,
    ) -> AsyncIterator[Comment]:
        """Stream every comment of one article, page by page, in server order.

        Replies arrive inline, immediately after their parent, when ``include_replies``
        is on. The cursor is the last top-level comment of the previous page.

        Two guards below are the ones that catch trap 2, where the server answers an
        unrecognised cursor with page 1 and HTTP 200 rather than an error: the ``seen``
        set, so a repeated page yields nothing twice, and the cursor-did-not-advance
        check, so a thread that stops moving terminates instead of looping for ever.
        """
        container_uuid = await self.resolve_container_uuid(container)
        cursor: str | None = None
        pages = 0
        seen: set[str] = set()

        while True:
            page = await self.fetch_page(
                container_uuid,
                sorted_by=sorted_by,
                reply_limit=reply_limit if include_replies else 0,
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
                logger.warning(
                    "Viafoura cursor did not advance on %s after %d pages; stopping",
                    container_uuid,
                    pages,
                )
                return
            cursor = next_cursor

    # ---- the retrieval modes ---------------------------------------------------

    async def get_all_comments(
        self,
        container: str,
        *,
        include_replies: bool = True,
        sorted_by: SortOrder = "newest",
        max_pages: int | None = None,
    ) -> list[Comment]:
        """Every comment on the article, top-level plus nested replies."""
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
        sorted_by: SortOrder = "num_likes_desc",
    ) -> list[Comment]:
        """The first ``n`` top-level comments in ``sorted_by`` order.

        Replies are never included: "top N" means N comments, not N threads.

        Pinned comments float to the top of **every** sort, so they are always inside
        the window however small it is — and so the result is not strictly ordered by
        ``sorted_by``. Verified live: the top ten by likes came back as 631, 1079, 768,
        …, the 631 being the pinned one. That is the server's behaviour, not a bug, and
        it is the reason a small ``limit`` can never miss an editor's existing pick.
        """
        if n <= 0:
            return []
        out: list[Comment] = []
        async for comment in self.iter_comments(
            container,
            sorted_by=sorted_by,
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
        top-level comment older than ``since``. **An ``until`` with no ``since`` therefore
        walks the whole thread**, because everything older than ``until`` is inside the
        window and there is nothing to stop at. That is correct, not a bug, but on a
        liveblog thread it is the full 15-second walk; pass ``since`` as well if a bound
        matters. Pinned comments are excluded from that
        test because the server floats them to the top regardless of their date, and
        treating one as "the oldest so far" would cut the walk short. Replies are kept
        only when they hang off a top-level comment inside the window, since the server
        nests them under their parent rather than sorting them on their own.
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
        sorted_by: SortOrder = "num_likes_desc",
        since: datetime | str | int | float | None = None,
        until: datetime | str | int | float | None = None,
        include_replies: bool = True,
    ) -> list[Comment]:
        """The one-call entry point.

        - no ``limit`` and no time bounds: every comment, replies included unless
          ``include_replies=False``;
        - ``limit=N``: the first N top-level comments in ``sorted_by`` order, never
          replies;
        - ``since``/``until``: comments posted inside that window, newest first, with
          ``limit`` then capping how many come back.
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
        return await self.get_top_comments(container, limit, sorted_by=sorted_by)

    # ---- discovery -------------------------------------------------------------

    async def trending(
        self,
        *,
        limit: int = 100,
        comment_window_hours: int = TRENDING_WINDOW_HOURS_MAX,
        article_window_days: int | None = None,
    ) -> list[TrendingArticle]:
        """Articles with recent comment activity, busiest first.

        The two windows are different things, and conflating them is what produced the
        "Viafoura only goes back 48 hours" belief: ``comment_window_hours`` bounds
        **comment activity** and caps at 48, while ``article_window_days`` bounds
        **article age** and reaches 30 days. A month of articles is what makes gathering
        real pinned examples for calibration viable.

        Each result carries ``content_container_uuid`` already, so reading a discovered
        article's thread costs no extra resolution call.
        """
        params: dict[str, Any] = {
            "limit": max(1, min(limit, TRENDING_LIMIT_MAX)),
            # Required, and its enum holds exactly this one value. Omitting it is a 400.
            "sorted_by": "total_visible_contents",
            "content_window_hours": max(
                1,
                min(comment_window_hours, TRENDING_WINDOW_HOURS_MAX),
            ),
        }
        if article_window_days is not None:
            params["content_container_window_days"] = max(
                1,
                min(article_window_days, TRENDING_ARTICLE_DAYS_MAX),
            )

        payload = await self._get(f"/v4/livecomments/{self._section}/trending", params)
        # `{"sorted_by": ..., "trending": [...]}` — the list is under `trending`, not
        # the `contents` the comments endpoint uses. Verified live 2026-09-22; guessing
        # it returned an empty list and no error, which is why this is not a guess.
        items = payload.get("trending") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            msg = (
                "Viafoura's trending response had no `trending` list; got "
                f"{sorted(payload) if isinstance(payload, dict) else type(payload)}"
            )
            raise ViafouraError(msg)
        return [
            TrendingArticle.from_payload(item)
            for item in items
            if isinstance(item, dict)
        ]


def _decode(body: str, url: str) -> Any:  # noqa: ANN401
    try:
        return json.loads(body)
    except ValueError as exc:
        msg = f"Viafoura returned a non-JSON 200 for {url}: {body[:200]}"
        raise ViafouraError(msg) from exc


def comments_to_dicts(comments: Iterable[Comment]) -> list[dict[str, Any]]:
    """Flatten comments to plain dicts with ISO timestamps, for a JSON dump."""
    return [
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
        }
        for c in comments
    ]


__all__ = [
    "Comment",
    "CommentFilter",
    "CommentPage",
    "ContainerNotFoundError",
    "SortOrder",
    "TrendingArticle",
    "ViafouraClient",
    "ViafouraError",
    "ViafouraHTTPError",
    "comments_to_dicts",
    "is_uuid",
]
