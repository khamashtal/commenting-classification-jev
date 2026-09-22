"""The Viafoura client: paging, the two silent traps, and the retry policy.

Nothing here opens a socket. `FakeSession` stands in for `aiohttp.ClientSession` and
records every URL and parameter set, which is what lets the trap tests assert on the
*shape* of the request rather than only on what came back — the traps are failures of
the request, not of the response.
"""

from __future__ import annotations

import uuid as _uuid
from typing import Any

import aiohttp
import pytest
from conftest import make_settings

from clients.viafoura import (
    PAGE_SIZE_MAX,
    REPLY_LIMIT_MAX,
    Comment,
    ContainerNotFoundError,
    ViafouraClient,
    ViafouraError,
    ViafouraHTTPError,
)

CONTAINER_UUID = "01a0c441-0000-7000-8000-00000000c001"
PAGE_ID = "A65vpYj1jg7t"


def uuid_at(n: int) -> str:
    """A distinct, valid uuid per index, so the cursor tests read clearly."""
    return str(_uuid.UUID(int=n))


def comment_payload(
    uuid: str,
    *,
    parent: str = CONTAINER_UUID,
    created_ms: int = 1_758_000_000_000,
    is_pinned: bool = False,
    text: str = "A comment.",
) -> dict[str, Any]:
    return {
        "content_uuid": uuid,
        "content_container_uuid": CONTAINER_UUID,
        "parent_uuid": parent,
        "thread_uuid": uuid,
        "content": text,
        "date_created": created_ms,
        "actor_uuid": "actor-1",
        "total_likes": 3,
        "total_dislikes": 0,
        "total_replies": 0,
        "is_pinned": is_pinned,
        "is_picked": False,
        "is_top_comment": False,
        "state": "visible",
        "metadata": {
            "origin_title": "A headline",
            "origin_url": "https://www.telegraph.co.uk/news/x/",
        },
    }


def page(uuids: list[str], *, more: bool = False, **kwargs: Any) -> dict[str, Any]:
    return {
        "more_available": more,
        "contents": [comment_payload(u, **kwargs) for u in uuids],
    }


# --------------------------------------------------------------------------- the fake


class FakeResponse:
    def __init__(self, status: int, body: str, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    async def text(self) -> str:
        return self._body


class _Ctx:
    """What `session.get(...)` returns: an async context manager over one response.

    It can also be handed an exception, which it raises on entry. That is where aiohttp
    raises a reset connection or a read timeout — before any status exists — and it is
    the failure the retry loop must handle separately from an HTTP status.
    """

    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class FakeSession:
    """A stand-in for `aiohttp.ClientSession` that answers from a script.

    `replies` is a list of `(status, body, headers)`; the last one repeats for ever, so
    a test that wants "the server always says this" supplies exactly one.
    """

    def __init__(
        self,
        replies: list[tuple[int, str, dict[str, str]] | BaseException],
    ) -> None:
        self._replies = replies
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: object = None,  # noqa: ARG002 - the real signature takes it
    ) -> _Ctx:
        self.calls.append((url, dict(params or {})))
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        reply = self._replies[index]
        if isinstance(reply, BaseException):
            return _Ctx(reply)
        return _Ctx(FakeResponse(*reply))

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]

    @property
    def params(self) -> list[dict[str, Any]]:
        return [params for _, params in self.calls]


def json_replies(*payloads: object) -> list[tuple[int, str, dict[str, str]]]:
    import json

    return [(200, json.dumps(p), {}) for p in payloads]


async def _no_sleep(_seconds: float) -> None:
    """Stand in for `asyncio.sleep` so a backoff test costs no wall clock."""


def client(session: FakeSession, **overrides: Any) -> ViafouraClient:
    config = make_settings().viafoura
    if overrides:
        from dataclasses import replace

        config = replace(config, **overrides)
    return ViafouraClient(session, config)


# --------------------------------------------------------------------------- the tests


class TestTrapOne:
    """`starting_from` is ignored on the flat `?container_id=` form, silently.

    It returns page 1 for ever with HTTP 200. The guard is structural: `fetch_page`
    refuses anything that is not a uuid, so the flat form cannot be used for paging even
    by accident.
    """

    async def test_fetch_page_refuses_a_page_id(self) -> None:
        session = FakeSession(json_replies(page([])))
        with pytest.raises(ViafouraError, match="content_container_uuid"):
            await client(session).fetch_page(PAGE_ID)
        assert session.calls == [], "it must refuse before making a request"

    async def test_paging_only_ever_uses_the_nested_path(self) -> None:
        session = FakeSession(
            json_replies(
                page([uuid_at(1), uuid_at(2)], more=True),
                page([uuid_at(3)], more=False),
            ),
        )
        await client(session).get_all_comments(CONTAINER_UUID)
        for url in session.urls:
            assert url.endswith(f"/{CONTAINER_UUID}/comments"), url
            assert "container_id" not in url

    async def test_the_flat_form_is_used_only_for_counts(self) -> None:
        """`container_details` is the one caller of the flat form, and it asks for no
        comments at all — which is what makes it a cheap pinned-count probe.
        """
        session = FakeSession(
            json_replies({"content_container_uuid": CONTAINER_UUID, "container": {}}),
        )
        await client(session).container_details(PAGE_ID)
        assert session.params[0] == {"container_id": PAGE_ID, "limit": 0}


class TestTrapTwo:
    """An unknown cursor is answered with page 1 and HTTP 200, not an error.

    Comment uuids really are UUIDv7, so fabricating cursors at chosen times to page a
    thread concurrently looks possible. It is not: the server resolves `starting_from`
    by uuid lookup and falls back to page 1. A pager built that way loops for ever,
    returning the same page, and looks like it worked.
    """

    async def test_a_server_stuck_on_page_one_terminates(self) -> None:
        stuck = json_replies(page([uuid_at(1), uuid_at(2)], more=True))
        session = FakeSession(stuck)
        comments = await client(session).get_all_comments(CONTAINER_UUID)

        assert [c.uuid for c in comments] == [uuid_at(1), uuid_at(2)], (
            "the seen set must stop the repeated page being yielded twice"
        )
        assert len(session.calls) == 2, (
            "one page, one repeat, then the cursor-did-not-advance guard stops it"
        )

    async def test_a_duplicate_comment_across_pages_is_yielded_once(self) -> None:
        session = FakeSession(
            json_replies(
                page([uuid_at(1), uuid_at(2)], more=True),
                page([uuid_at(2), uuid_at(3)], more=False),
            ),
        )
        comments = await client(session).get_all_comments(CONTAINER_UUID)
        assert [c.uuid for c in comments] == [uuid_at(1), uuid_at(2), uuid_at(3)]


class TestPaging:
    async def test_the_cursor_is_the_last_top_level_uuid(self) -> None:
        session = FakeSession(
            json_replies(
                page([uuid_at(1), uuid_at(2)], more=True),
                page([uuid_at(3)], more=False),
            ),
        )
        await client(session).get_all_comments(CONTAINER_UUID)
        assert "starting_from" not in session.params[0]
        assert session.params[1]["starting_from"] == uuid_at(2)

    async def test_a_reply_is_never_the_cursor(self) -> None:
        """Replies arrive inline after their parent; using one as the cursor skips
        every top-level comment between it and the next page.
        """
        reply = uuid_at(9)
        first = page([uuid_at(1)], more=True)
        first["contents"].append(comment_payload(reply, parent=uuid_at(1)))
        session = FakeSession(json_replies(first, page([uuid_at(2)])))
        await client(session).get_all_comments(CONTAINER_UUID)
        assert session.params[1]["starting_from"] == uuid_at(1)

    async def test_paging_stops_when_the_server_says_there_is_no_more(self) -> None:
        session = FakeSession(json_replies(page([uuid_at(1)], more=False)))
        await client(session).get_all_comments(CONTAINER_UUID)
        assert len(session.calls) == 1

    async def test_max_pages_caps_the_walk(self) -> None:
        session = FakeSession(json_replies(page([uuid_at(1)], more=True)))
        await client(session).get_all_comments(CONTAINER_UUID, max_pages=1)
        assert len(session.calls) == 1

    async def test_an_empty_page_ends_the_walk(self) -> None:
        """`more_available` can be true with nothing in the page; believing it loops."""
        session = FakeSession(json_replies(page([], more=True)))
        assert await client(session).get_all_comments(CONTAINER_UUID) == []
        assert len(session.calls) == 1


class TestRequestShape:
    async def test_the_page_size_is_clamped_to_the_server_maximum(self) -> None:
        """Undocumented and hard: `limit=101` is an HTTP 400."""
        session = FakeSession(json_replies(page([])))
        await client(session).fetch_page(CONTAINER_UUID, limit=500)
        assert session.params[0]["limit"] == PAGE_SIZE_MAX

    async def test_the_reply_limit_is_clamped_too(self) -> None:
        session = FakeSession(json_replies(page([])))
        await client(session).fetch_page(CONTAINER_UUID, reply_limit=500)
        assert session.params[0]["reply_limit"] == REPLY_LIMIT_MAX

    async def test_excluding_replies_asks_for_none(self) -> None:
        session = FakeSession(json_replies(page([])))
        await client(session).get_all_comments(CONTAINER_UUID, include_replies=False)
        assert session.params[0]["reply_limit"] == 0

    async def test_no_cursor_is_sent_on_the_first_page(self) -> None:
        """Sending an empty cursor is not the same as sending none."""
        session = FakeSession(json_replies(page([])))
        await client(session).fetch_page(CONTAINER_UUID)
        assert "starting_from" not in session.params[0]

    async def test_the_filter_is_omitted_when_it_is_none(self) -> None:
        session = FakeSession(json_replies(page([])))
        await client(session).fetch_page(CONTAINER_UUID, filtered_by="none")
        assert "filtered_by" not in session.params[0]


class TestResolution:
    async def test_a_uuid_needs_no_lookup(self) -> None:
        session = FakeSession(json_replies({}))
        assert await client(session).resolve_container_uuid(CONTAINER_UUID) == (
            CONTAINER_UUID
        )
        assert session.calls == []

    async def test_a_page_id_is_looked_up_once_and_cached(self) -> None:
        session = FakeSession(
            json_replies({"container": {"content_container_uuid": CONTAINER_UUID}}),
        )
        vf = client(session)
        assert await vf.resolve_container_uuid(PAGE_ID) == CONTAINER_UUID
        assert await vf.resolve_container_uuid(PAGE_ID) == CONTAINER_UUID
        assert len(session.calls) == 1, "the second call must come from the cache"

    async def test_a_missing_uuid_in_the_record_is_an_error(self) -> None:
        session = FakeSession(json_replies({"container": {"total_visible_content": 3}}))
        with pytest.raises(ContainerNotFoundError):
            await client(session).resolve_container_uuid(PAGE_ID)

    async def test_a_url_is_refused(self) -> None:
        session = FakeSession(json_replies({}))
        with pytest.raises(ViafouraError, match="URL"):
            await client(session).resolve_container_uuid("https://t.co.uk/news/x/")
        assert session.calls == []


class TestErrors:
    async def test_404_is_a_container_not_found(self) -> None:
        session = FakeSession([(404, "not found", {})])
        with pytest.raises(ContainerNotFoundError):
            await client(session).container_details(PAGE_ID)

    async def test_a_non_json_200_is_reported_with_the_body(self) -> None:
        """Viafoura's errors are plain-text Vert.x exceptions, not JSON."""
        session = FakeSession([(200, "io.vertx.core.VertxException: boom", {})])
        with pytest.raises(ViafouraError, match="non-JSON"):
            await client(session).container_details(PAGE_ID)

    async def test_a_400_is_not_retried(self) -> None:
        """A bad parameter fails identically however many times it is sent."""
        session = FakeSession([(400, "limit exceeds maximum", {})])
        with pytest.raises(ViafouraHTTPError) as caught:
            await client(session).container_details(PAGE_ID)
        assert caught.value.status == 400
        assert len(session.calls) == 1

    async def test_the_body_is_carried_on_the_error(self) -> None:
        """The status alone rarely says which parameter the server disliked."""
        session = FakeSession([(400, "sorted_by is required", {})])
        with pytest.raises(ViafouraHTTPError, match="sorted_by is required"):
            await client(session).container_details(PAGE_ID)


class TestRetries:
    async def test_a_429_is_retried_and_then_succeeds(self) -> None:
        import json

        session = FakeSession(
            [
                (429, "slow down", {}),
                (200, json.dumps({"container": {"x": 1}}), {}),
            ],
        )
        await client(session).container_details(PAGE_ID)
        assert len(session.calls) == 2

    async def test_retries_are_exhausted_and_the_real_error_surfaces(self) -> None:
        session = FakeSession([(503, "unavailable", {})])
        with pytest.raises(ViafouraHTTPError) as caught:
            await client(session, max_retries=2).container_details(PAGE_ID)
        assert caught.value.status == 503
        assert len(session.calls) == 3, "the first attempt plus two retries"

    async def test_retry_after_is_honoured_and_capped(self, monkeypatch) -> None:
        """A server that is already misbehaving must not be able to stall the run."""
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("clients.viafoura.asyncio.sleep", record)
        import json

        session = FakeSession(
            [
                (429, "slow down", {"Retry-After": "600"}),
                (200, json.dumps({"container": {}}), {}),
            ],
        )
        await client(
            session,
            backoff_max_seconds=5.0,
            backoff_initial_seconds=0.5,
        ).container_details(PAGE_ID)
        assert slept == [5.0], "the header was honoured but capped at backoff_max"

    async def test_a_nonsense_retry_after_falls_back_to_the_backoff(
        self,
        monkeypatch,
    ) -> None:
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("clients.viafoura.asyncio.sleep", record)
        import json

        session = FakeSession(
            [
                (429, "slow down", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                (200, json.dumps({"container": {}}), {}),
            ],
        )
        await client(session, backoff_initial_seconds=2.0).container_details(PAGE_ID)
        assert len(slept) == 1
        assert 0.0 <= slept[0] <= 2.0, "full jitter over [0, backoff_initial]"


class TestTransportFailures:
    """A reset connection never reaches a status, so the status clause cannot see it.

    Paging a 4,000-comment thread keeps a connection busy for fifteen seconds, which is
    exactly the window where one of these lands. Before this was handled, a single blip
    aborted the whole walk.
    """

    @pytest.mark.parametrize(
        "failure",
        [
            aiohttp.ClientConnectionError("connection reset"),
            aiohttp.ClientOSError("broken pipe"),
            TimeoutError(),
        ],
    )
    async def test_a_transport_failure_is_retried(
        self,
        failure: BaseException,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("clients.viafoura.asyncio.sleep", _no_sleep)
        import json

        session = FakeSession([failure, (200, json.dumps({"container": {}}), {})])
        await client(session).container_details(PAGE_ID)
        assert len(session.calls) == 2

    async def test_a_persistent_transport_failure_names_the_cause(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("clients.viafoura.asyncio.sleep", _no_sleep)
        session = FakeSession([aiohttp.ClientConnectionError("connection reset")])
        with pytest.raises(ViafouraError, match="ClientConnectionError"):
            await client(session, max_retries=1).container_details(PAGE_ID)
        assert len(session.calls) == 2

    async def test_a_blip_mid_walk_does_not_abort_the_thread(
        self,
        monkeypatch,
    ) -> None:
        """The case that matters: page 1 arrives, page 2 blips, page 2 then succeeds."""
        monkeypatch.setattr("clients.viafoura.asyncio.sleep", _no_sleep)
        import json

        session = FakeSession(
            [
                (200, json.dumps(page([uuid_at(1)], more=True)), {}),
                TimeoutError(),
                (200, json.dumps(page([uuid_at(2)], more=False)), {}),
            ],
        )
        comments = await client(session).get_all_comments(CONTAINER_UUID)
        assert [c.uuid for c in comments] == [uuid_at(1), uuid_at(2)]


class TestConfiguredSizes:
    """`[viafoura] page_size` and `reply_limit` must actually reach the request.

    They were parsed, validated and documented before they were used, which is the
    "setting nothing reads" failure this project removed `full_sweep_after_hours` to
    avoid. These tests are what stop it coming back.
    """

    async def test_the_configured_page_size_is_sent(self) -> None:
        session = FakeSession(json_replies(page([])))
        await client(session, page_size=25).fetch_page(CONTAINER_UUID)
        assert session.params[0]["limit"] == 25

    async def test_the_configured_reply_limit_is_sent(self) -> None:
        session = FakeSession(json_replies(page([])))
        await client(session, reply_limit=5).fetch_page(CONTAINER_UUID)
        assert session.params[0]["reply_limit"] == 5

    async def test_the_walk_uses_the_configured_page_size_too(self) -> None:
        session = FakeSession(json_replies(page([])))
        await client(session, page_size=25).get_all_comments(CONTAINER_UUID)
        assert session.params[0]["limit"] == 25

    async def test_a_page_size_over_the_server_maximum_is_clamped(self) -> None:
        """The file may ask for 500; the server answers 101 with an HTTP 400."""
        session = FakeSession(json_replies(page([])))
        await client(session, page_size=500).fetch_page(CONTAINER_UUID)
        assert session.params[0]["limit"] == PAGE_SIZE_MAX


class TestTimeWindow:
    async def test_the_walk_stops_at_the_first_comment_older_than_since(self) -> None:
        old, new = 1_757_000_000_000, 1_758_000_000_000
        first = page([uuid_at(1)], more=True, created_ms=new)
        first["contents"].append(comment_payload(uuid_at(2), created_ms=old))
        session = FakeSession(json_replies(first, page([uuid_at(3)])))

        comments = await client(session).get_comments_in_range(
            CONTAINER_UUID,
            since=new - 1000,
        )
        assert [c.uuid for c in comments] == [uuid_at(1)]
        assert len(session.calls) == 1, "it must not fetch a second page"

    async def test_a_pinned_comment_does_not_end_the_walk(self) -> None:
        """Pinned comments float to the top of every sort regardless of date, so
        treating one as "the oldest so far" would cut the walk short.
        """
        old, new = 1_757_000_000_000, 1_758_000_000_000
        first = page([], more=True)
        first["contents"] = [
            comment_payload(uuid_at(1), created_ms=old, is_pinned=True),
            comment_payload(uuid_at(2), created_ms=new),
        ]
        session = FakeSession(json_replies(first, page([uuid_at(3)], created_ms=new)))

        comments = await client(session).get_comments_in_range(
            CONTAINER_UUID,
            since=new - 1000,
        )
        assert [c.uuid for c in comments] == [uuid_at(2), uuid_at(3)]


class TestTrending:
    async def test_the_required_sort_is_always_sent(self) -> None:
        """`sorted_by` is required and its enum holds exactly one value; omitting it
        is an HTTP 400.
        """
        session = FakeSession(
            json_replies({"sorted_by": "total_visible_contents", "trending": []}),
        )
        await client(session).trending()
        assert session.params[0]["sorted_by"] == "total_visible_contents"

    async def test_the_two_windows_are_clamped_to_their_own_maxima(self) -> None:
        """They are different things: hours bound comment activity, days bound article
        age. Conflating them is what produced the "48 hours" belief.
        """
        session = FakeSession(
            json_replies({"sorted_by": "total_visible_contents", "trending": []}),
        )
        await client(session).trending(
            limit=99_999,
            comment_window_hours=500,
            article_window_days=365,
        )
        params = session.params[0]
        assert params["limit"] == 1000
        assert params["content_window_hours"] == 48
        assert params["content_container_window_days"] == 30

    async def test_the_article_window_is_omitted_unless_asked_for(self) -> None:
        session = FakeSession(
            json_replies({"sorted_by": "total_visible_contents", "trending": []}),
        )
        await client(session).trending()
        assert "content_container_window_days" not in session.params[0]

    async def test_a_missing_trending_list_is_an_error_not_an_empty_result(
        self,
    ) -> None:
        """This one is from life: the list is under `trending`, not `contents`. The
        first implementation guessed, and an empty list came back with no error at all.
        """
        session = FakeSession(json_replies({"sorted_by": "total_visible_contents"}))
        with pytest.raises(ViafouraError, match="no `trending` list"):
            await client(session).trending()

    async def test_a_result_carries_the_uuid_so_no_lookup_is_needed(self) -> None:
        session = FakeSession(
            json_replies(
                {
                    "sorted_by": "total_visible_contents",
                    "trending": [
                        {
                            "container_id": PAGE_ID,
                            "content_container_uuid": CONTAINER_UUID,
                            "origin_url": "https://www.telegraph.co.uk/news/x/",
                            "origin_title": "A headline",
                            "date_published": 1_758_000_000_000,
                            "total_visible_contents": 2680,
                        },
                    ],
                },
            ),
        )
        articles = await client(session).trending()
        assert articles[0].container_uuid == CONTAINER_UUID
        assert articles[0].total_comments == 2680
        assert articles[0].published_at is not None


class TestCommentParsing:
    def test_a_top_level_comment_is_not_a_reply(self) -> None:
        comment = Comment.from_payload(comment_payload(uuid_at(1)))
        assert not comment.is_reply

    def test_a_reply_is_one(self) -> None:
        comment = Comment.from_payload(
            comment_payload(uuid_at(2), parent=uuid_at(1)),
        )
        assert comment.is_reply

    def test_the_timestamp_is_utc_aware(self) -> None:
        """`date_created` is epoch milliseconds as an int, not an ISO string."""
        comment = Comment.from_payload(comment_payload(uuid_at(1)))
        assert comment.created_at.tzinfo is not None
        assert comment.created_at.utcoffset().total_seconds() == 0

    def test_is_edited_is_surfaced(self) -> None:
        """The store assumes comment text never changes; an edit breaks that."""
        payload = comment_payload(uuid_at(1))
        assert not Comment.from_payload(payload).is_edited
        assert Comment.from_payload({**payload, "is_edited": True}).is_edited
