"""The Jev client: pacing, bounded concurrency, adaptation and accounting.

All offline. The limiter is driven by its own monotonic clock, so these assert real
timing behaviour without a single network call.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from clients.jev import (
    DEFAULT_CONCURRENCY,
    MIN_SCALE,
    SAFETY,
    AdaptiveLimiter,
    JevClient,
    JevLimits,
    Spend,
)

TOKENS = 3200  # a typical battery request


class TestBuckets:
    async def test_short_run_uses_the_burst_rather_than_the_sustained_rate(
        self,
    ) -> None:
        """A minute's allowance starts full, so a small job is not throttled to 18/s."""
        limiter = AdaptiveLimiter()
        start = time.monotonic()
        await asyncio.gather(*(limiter.acquire(TOKENS) for _ in range(200)))
        elapsed = time.monotonic() - start
        assert elapsed < 200 / 18, "a 200-request job should not be paced at 18/s"

    async def test_long_run_settles_to_the_sustained_rate(self) -> None:
        """Past the bucket, the refill rate is what governs."""
        # Deliberately small limits: the same arithmetic, in under a second, because
        # this suite runs on every change.
        limits = JevLimits(requests_per_minute=1200, tokens_per_second=10**9)
        limiter = AdaptiveLimiter(limits)
        capacity = int(1200 * SAFETY)
        over = 9
        start = time.monotonic()
        await asyncio.gather(*(limiter.acquire(1) for _ in range(capacity + over)))
        elapsed = time.monotonic() - start
        expected = over / (1200 / 60 * SAFETY)
        assert elapsed == pytest.approx(expected, rel=0.6)

    async def test_token_limit_binds_when_requests_are_large(self) -> None:
        limits = JevLimits(requests_per_minute=10**6, tokens_per_second=10_000)
        limiter = AdaptiveLimiter(limits)
        start = time.monotonic()
        # Each request is a third of the per-second allowance, so four of them cannot
        # all go at once however much request headroom there is.
        await asyncio.gather(*(limiter.acquire(3000) for _ in range(4)))
        assert time.monotonic() - start > 0.25

    async def test_a_request_larger_than_the_bucket_does_not_hang(self) -> None:
        limiter = AdaptiveLimiter(JevLimits(tokens_per_second=1000))
        await asyncio.wait_for(limiter.acquire(10**9), timeout=2.0)


class TestAdaptation:
    def test_a_429_halves_the_rate(self) -> None:
        limiter = AdaptiveLimiter()
        assert limiter.scale == 1.0
        limiter.penalise()
        assert limiter.scale == 0.5
        limiter.penalise()
        assert limiter.scale == 0.25
        assert limiter.penalties == 2

    def test_the_rate_never_reaches_zero(self) -> None:
        limiter = AdaptiveLimiter()
        for _ in range(100):
            limiter.penalise()
        assert limiter.scale == MIN_SCALE
        assert limiter.scale > 0

    def test_a_clean_streak_recovers_the_rate(self) -> None:
        limiter = AdaptiveLimiter()
        limiter.penalise()
        halved = limiter.scale
        for _ in range(400):
            limiter.record_success()
        assert limiter.scale > halved

    def test_success_at_full_rate_does_not_exceed_one(self) -> None:
        limiter = AdaptiveLimiter()
        for _ in range(1000):
            limiter.record_success()
        assert limiter.scale == 1.0

    async def test_a_penalty_drains_the_buckets(self) -> None:
        """A 429 must be followed by a real pause, not just a slower refill."""
        limiter = AdaptiveLimiter()
        await limiter.acquire(TOKENS)
        limiter.penalise()
        start = time.monotonic()
        await limiter.acquire(TOKENS)
        assert time.monotonic() - start > 0.05


class TestAccounting:
    def test_since_measures_one_stretch_not_the_whole_client(self) -> None:
        """One client serving several articles must not merge their costs."""
        client = JevClient("key")
        client._usage.update({"input_tokens": 1000, "requests": 3})
        client._models.add("jev-1.13.0")
        mark = client.spent()
        client._usage.update({"input_tokens": 500, "requests": 2})

        delta = client.since(mark)
        assert delta.input_tokens == 500
        assert delta.requests == 2
        assert client.spent().input_tokens == 1500

    def test_cost_is_input_tokens_only(self) -> None:
        spend = Spend(
            input_tokens=1_000_000,
            output_tokens=999,
            requests=1,
            models=frozenset({"jev-1.13.0"}),
        )
        assert spend.estimated_cost_usd == pytest.approx(0.042)

    def test_no_successful_requests_is_reported_honestly(self) -> None:
        spend = Spend(input_tokens=0, output_tokens=0, requests=0, models=frozenset())
        assert spend.model == "no successful requests"

    def test_several_models_are_all_named(self) -> None:
        """If an alias advances mid-run, the report must not claim a single version."""
        spend = Spend(
            input_tokens=0,
            output_tokens=0,
            requests=0,
            models=frozenset({"jev-1.13.0", "jev-1.14.0"}),
        )
        assert spend.model == "jev-1.13.0, jev-1.14.0"


class TestLifecycle:
    async def test_ask_outside_a_context_manager_fails_loudly(self) -> None:
        client = JevClient("key")
        with pytest.raises(RuntimeError, match="async with"):
            await client.ask({"a": 1}, {})

    def test_token_estimate_scales_with_the_payload(self) -> None:
        small = JevClient._estimate_tokens({"a": "x"}, {})
        large = JevClient._estimate_tokens({"a": "x" * 4000}, {})
        assert large > small
        assert large == pytest.approx(1000, rel=0.2)

    def test_token_estimate_accepts_the_real_battery(self) -> None:
        """The SDK's question types changed from msgspec to pydantic in 0.7, and every
        request then failed locally. FakeJev never reaches this method, so only the
        real battery here catches that."""
        from processing.questions import BATTERY

        assert JevClient._estimate_tokens({"a": "x"}, BATTERY) > 0

    def test_connection_pool_is_bounded_to_the_semaphore(self) -> None:
        """Unbounded httpx connections would defeat the concurrency bound."""
        client = JevClient("key", concurrency=8)
        http = client._build_http_client()
        assert http._transport._pool._max_connections == 8

    def test_default_concurrency_is_sane(self) -> None:
        assert 1 <= DEFAULT_CONCURRENCY <= 256


class TestAdaptationWiring:
    """The limiter only adapts if the transport actually reports 429s to it."""

    async def test_a_429_response_reaches_the_limiter(self) -> None:
        client = JevClient("key")
        http = client._build_http_client()
        hooks = http.event_hooks["response"]
        assert hooks, "no response hook: 429s would never reach the limiter"

        class Response:
            status_code = 429

        before = client._limiter.scale
        for hook in hooks:
            await hook(Response())
        assert client._limiter.scale < before, (
            "a 429 passed through the hook without scaling the rate down"
        )
        await http.aclose()

    async def test_a_200_response_does_not_penalise(self) -> None:
        client = JevClient("key")
        http = client._build_http_client()

        class Response:
            status_code = 200

        for hook in http.event_hooks["response"]:
            await hook(Response())
        assert client._limiter.scale == 1.0
        assert client._limiter.penalties == 0
        await http.aclose()

    async def test_the_rate_is_cleared_before_a_socket_is_taken(self) -> None:
        """Waiting for the limiter while holding a semaphore slot would idle it."""
        import inspect

        source = inspect.getsource(JevClient.ask)
        acquire_at = source.index("_limiter.acquire")
        semaphore_at = source.index("self._semaphore")
        assert acquire_at < semaphore_at, (
            "the limiter must be acquired before the semaphore, or slots sit idle"
        )
