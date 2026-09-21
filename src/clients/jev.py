"""Async client for TypeSafe's Jev API, with the pacing the published limits require.

Jev publishes two limits, and a request over either returns 429:

    1,200 requests per minute   (20/s sustained)
    250,000 tokens per second

Neither is a cap on requests *in flight*, which is all a semaphore bounds. Ten in flight
is 40/s if Jev answers in 250 ms and 5/s if it answers in two seconds, so a semaphore
alone either breaches the limit or wastes most of the allowance depending on a latency
nobody has measured. This client therefore paces on *rate* and keeps the semaphore only
as a bound on open sockets.

Both limits are modelled as token buckets, which is what lets a short run finish fast
without being told how long it is. The request bucket holds a minute's allowance, so a
run of 500 comments finds it full, spends 500 at once and is bounded only by the token
bucket — about 68 requests a second. A run of 5,000 drains the bucket at that speed and
then settles to the 18/s refill. Dividing the per-minute allowance by the job size, the
obvious alternative, is right for one job size by luck and wrong either side of it.

The published numbers are a starting point, not the truth: TypeSafe's models page warns
they "can change without notice". Every 429 the transport sees halves the allowed rate
and a clean streak walks it back up, so the client converges on what the server actually
enforces — including a burst allowance smaller than the one assumed here.

Open it once and reuse it, like the other clients here:

    async with JevClient(settings.typesafe_api_key) as jev:
        response = await jev.ask(state, questions)

Nothing in this module knows what a comment is. It takes a state and a battery of
questions, returns the answers, and leaves every judgment to `processing`.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx2
import msgspec
from typesafe_sdk import (
    AsyncTypeSafeClient,
    JSONContent,
    Question,
    RetryPolicy,
    SystemOneResponse,
)

from processing.log_config import logger

# --------------------------------------------------------------------------- defaults

# Multiplied into both published limits. Leaves room for clock skew against the server's
# own window, and for the requests already in flight when a 429 lands.
SAFETY = 0.9
# A 429 halves the allowed rate; this is the floor, so the client never stops entirely.
MIN_SCALE = 0.05
# Successful requests needed before the rate steps back up.
RECOVERY_STREAK = 200
# How much of the lost rate each recovery step returns.
RECOVERY_STEP = 0.05
# Requests in flight. Bounds sockets, not rate. At the 18/s ceiling this keeps the rate
# limiter rather than the semaphore as the bottleneck for any mean latency up to ~3.5s.
DEFAULT_CONCURRENCY = 64
# Per-request HTTP timeout. The SDK default of 10s is tight for a large battery.
DEFAULT_TIMEOUT = 30.0
# Four characters per token, the usual English rule of thumb. The estimate only feeds the
# token bucket, and SAFETY absorbs the error.
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class JevLimits:
    """The published limits, so a change is a one-line edit.

    See https://docs.typesafe.ai/models. These are per API key, not per client.
    """

    requests_per_minute: int = 1200
    tokens_per_second: int = 250_000


# ----------------------------------------------------------------------- rate limiting


class _Bucket:
    """One token bucket: a level that refills at a fixed rate up to a capacity."""

    __slots__ = ("_capacity", "_level", "_per_second", "_updated")

    def __init__(self, *, capacity: float, per_second: float) -> None:
        self._capacity = capacity
        self._per_second = per_second
        # Starts full, which is what lets a short run go at the other limit's speed.
        self._level = capacity
        self._updated = time.monotonic()

    def wait_for(self, amount: float, now: float, scale: float) -> float:
        """Refill to ``now`` and return the seconds until ``amount`` is available."""
        self._level = min(
            self._capacity,
            self._level + (now - self._updated) * self._per_second * scale,
        )
        self._updated = now
        # A request larger than the whole bucket would otherwise wait forever.
        amount = min(amount, self._capacity)
        if self._level >= amount:
            return 0.0
        return (amount - self._level) / (self._per_second * scale)

    def take(self, amount: float) -> None:
        """Deduct ``amount``, which `wait_for` has already confirmed is available."""
        self._level -= min(amount, self._capacity)

    def drain(self) -> None:
        """Empty the bucket, so a 429 is followed by a real pause."""
        self._level = 0.0


class AdaptiveLimiter:
    """Paces requests against both published limits, adapting to observed 429s.

    One per API key. `JevClient` builds its own; share one explicitly only if several
    clients run against the same key at once.
    """

    def __init__(self, limits: JevLimits | None = None) -> None:
        self._limits = limits or JevLimits()
        per_second = self._limits.requests_per_minute / 60 * SAFETY
        tokens = self._limits.tokens_per_second * SAFETY
        self._requests = _Bucket(
            # A minute's allowance, so a run shorter than that is not throttled at all.
            capacity=self._limits.requests_per_minute * SAFETY,
            per_second=per_second,
        )
        # Capacity equals the per-second rate: this limit allows no multi-second burst.
        self._tokens = _Bucket(capacity=tokens, per_second=tokens)
        self._scale = 1.0
        self._successes = 0
        self._penalties = 0
        self._lock = asyncio.Lock()

    @property
    def scale(self) -> float:
        """The fraction of the published rate currently allowed."""
        return self._scale

    @property
    def penalties(self) -> int:
        """How many 429s have been observed."""
        return self._penalties

    async def acquire(self, estimated_tokens: int) -> None:
        """Block until this request fits within both limits."""
        while True:
            async with self._lock:
                now = time.monotonic()
                # Both are refilled before either is taken, so the two stay in step.
                wait = max(
                    self._requests.wait_for(1.0, now, self._scale),
                    self._tokens.wait_for(estimated_tokens, now, self._scale),
                )
                if wait <= 0.0:
                    self._requests.take(1.0)
                    self._tokens.take(estimated_tokens)
                    return
            # Capped so a penalty applied while waiting is picked up promptly.
            await asyncio.sleep(min(wait, 0.5))

    def penalise(self) -> None:
        """Halve the allowed rate after an observed 429."""
        self._penalties += 1
        self._successes = 0
        previous = self._scale
        self._scale = max(MIN_SCALE, self._scale * 0.5)
        self._requests.drain()
        self._tokens.drain()
        logger.warning(
            "Jev returned 429; rate scaled %.2f -> %.2f (%.1f req/s)",
            previous,
            self._scale,
            self._limits.requests_per_minute / 60 * SAFETY * self._scale,
        )

    def record_success(self) -> None:
        """Step the rate back up after a long enough clean streak."""
        if self._scale >= 1.0:
            return
        self._successes += 1
        if self._successes < RECOVERY_STREAK:
            return
        self._successes = 0
        self._scale = min(1.0, self._scale + RECOVERY_STEP)
        logger.info("Jev steady; rate scaled back up to %.2f", self._scale)


# --------------------------------------------------------------------------- the client


@dataclass(frozen=True, slots=True)
class Spend:
    """What a stretch of work cost, and which model answered it."""

    input_tokens: int
    output_tokens: int
    requests: int
    models: frozenset[str]

    @property
    def estimated_cost_usd(self) -> float:
        """Jev bills input tokens only, at $0.042 per million."""
        return self.input_tokens * 0.042 / 1_000_000

    @property
    def model(self) -> str:
        """The model that answered, or all of them if an alias moved mid-run."""
        return ", ".join(sorted(self.models)) or "no successful requests"


class JevClient:
    """Rate-limited, bounded-concurrency access to Jev's System One endpoint.

    Every call is paced against both published limits before it leaves, so callers fan
    out with a plain `asyncio.gather` and never think about rate limits.

    Usage and the answering model version are accumulated here rather than by the caller,
    because both are properties of the transport. `since()` turns the running totals into
    the cost of one stretch of work, which is what lets one client serve several articles.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        limits: JevLimits | None = None,
        limiter: AdaptiveLimiter | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout: float = DEFAULT_TIMEOUT,
        retry: RetryPolicy | None = None,
    ) -> None:
        """Build a client. Nothing is opened until it is entered.

        ``model`` of `None` leaves the version to the API (`jev-latest`); the version
        that actually answered is read back off each response and reported by `since`.
        """
        self._api_key = api_key
        self._model = model
        self._limiter = limiter or AdaptiveLimiter(limits)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._concurrency = concurrency
        self._timeout = timeout
        # More generous than the SDK's default of 2 retries inside a 30s budget, which a
        # long run meeting sustained 429s exhausts, turning throttling into lost comments.
        self._retry = retry or RetryPolicy(
            max_retries=4,
            backoff_initial=1.0,
            backoff_max=30.0,
            timeout=180.0,
        )
        self._usage: Counter[str] = Counter()
        self._models: set[str] = set()
        self._client: AsyncTypeSafeClient | None = None

    async def __aenter__(self) -> Self:
        self._client = AsyncTypeSafeClient(
            api_key=self._api_key,
            retry=self._retry,
            http_client=self._build_http_client(),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            # The SDK closes the http client it was handed.
            await self._client.__aexit__(exc_type, exc, tb)
            self._client = None

    def _build_http_client(self) -> httpx2.AsyncClient:
        """An HTTP client that reports 429s to the limiter.

        The SDK retries 429s internally and honours `Retry-After`, so by the time a call
        raises, the throttling has been absorbed and hidden. Hooking the response is the
        only way the limiter gets to see them and adapt.
        """

        async def on_response(response: httpx2.Response) -> None:
            if response.status_code == 429:
                self._limiter.penalise()

        return httpx2.AsyncClient(
            timeout=self._timeout,
            # httpx leaves connections unbounded by default; match the semaphore.
            limits=httpx2.Limits(max_connections=self._concurrency),
            event_hooks={"response": [on_response]},
        )

    async def ask(
        self,
        state: JSONContent,
        questions: dict[str, Question],
    ) -> SystemOneResponse:
        """Answer ``questions`` about ``state``, once the limits allow it.

        Raises whatever the SDK raises. A caller fanning out over many items should
        decide for itself whether one failure should end the run.
        """
        if self._client is None:
            msg = "JevClient must be used inside 'async with'."
            raise RuntimeError(msg)

        # Waiting for the limiter inside the semaphore would idle a slot, so the rate is
        # cleared first and the socket taken only when the request is ready to go.
        await self._limiter.acquire(self._estimate_tokens(state, questions))
        async with self._semaphore:
            response = await self._client.system_one(
                state=state,
                questions=questions,
                model=self._model,
            )

        self._limiter.record_success()
        self._models.add(response.model)
        self._usage["input_tokens"] += response.usage.input_tokens or 0
        self._usage["output_tokens"] += response.usage.output_tokens or 0
        self._usage["requests"] += 1
        return response

    @staticmethod
    def _estimate_tokens(state: JSONContent, questions: dict[str, Question]) -> int:
        """Roughly what this request will cost, measured rather than assumed.

        Measuring the real payload means a long article or an extra question is charged
        for what it is, so the token bucket stays accurate as the battery is tuned.
        """
        encoded = len(msgspec.json.encode(state)) + len(msgspec.json.encode(questions))
        return encoded // _CHARS_PER_TOKEN

    def spent(self) -> Spend:
        """Everything this client has spent since it was created."""
        return Spend(
            input_tokens=self._usage["input_tokens"],
            output_tokens=self._usage["output_tokens"],
            requests=self._usage["requests"],
            models=frozenset(self._models),
        )

    def since(self, mark: Spend) -> Spend:
        """What has been spent since ``mark``, for costing one article's run.

        Models are not subtracted: the set is small, and naming every version that
        answered during the stretch is the honest answer when an alias moves mid-run.
        """
        now = self.spent()
        return Spend(
            input_tokens=now.input_tokens - mark.input_tokens,
            output_tokens=now.output_tokens - mark.output_tokens,
            requests=now.requests - mark.requests,
            models=now.models,
        )

    @property
    def rate_scale(self) -> float:
        """The fraction of the published rate currently allowed, after any 429s."""
        return self._limiter.scale

    @property
    def penalties(self) -> int:
        """How many 429s this client has seen."""
        return self._limiter.penalties
