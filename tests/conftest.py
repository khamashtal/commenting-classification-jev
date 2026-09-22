"""Shared fixtures. Nothing here touches a network or costs money.

Every Jev interaction in the suite goes through `FakeJev`, which satisfies the same
contract as `JevClient` — `ask`, `spent`, `since` — and returns canned answers. If a test
ever reaches the real API it is a defect, not a slow test: see `test_no_live_calls.py`,
which fails the suite if the real client is constructed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from clients.jev import Spend
from clients.vf_mcp import Comment
from processing.classification import Classification, compute_signals
from processing.fetch import Article, ArticleThread
from processing.questions import NOUL_IDS

# A comment Jev likes: first-hand experience, measured, clean on every gate.
GOOD_ANSWERS: dict[str, Any] = {
    "personal_experience": 2.6,
    "experience_relevant": 0.92,
    "tone": 1.8,
    "readability": 1.9,
    "proposes_solution": 0.15,
    "reasoned_argument": 0.85,
    "standalone": 0.95,
    "stance": {"choice": "supportive", "confidence": 0.9, "probabilities": {}},
    "on_topic": 0.97,
    "sarcasm": 0.02,
    "unverified_claim": 0.3,
    "personal_attack": 0.01,
    "group_hostility": 0.01,
    "profanity_or_threat": 0.0,
}

# The same comment as far as the score goes, but sarcastic enough to be excluded.
SARCASTIC_ANSWERS: dict[str, Any] = {**GOOD_ANSWERS, "sarcasm": 0.95}

# Low on everything that earns points, clean on every gate: kept, but ranked last.
WEAK_ANSWERS: dict[str, Any] = {
    **GOOD_ANSWERS,
    "personal_experience": 0.2,
    "reasoned_argument": 0.1,
    "proposes_solution": 0.05,
    "tone": 1.0,
}


def make_comment(
    uuid: str = "c-1",
    text: str = (
        "My wife waited fourteen months for a scan she was told would take eighteen "
        "weeks, and nobody ever rang to explain the delay to us."
    ),
    *,
    is_reply: bool = False,
    is_pinned: bool = False,
    is_picked: bool = False,
    likes: int = 10,
    state: str | None = "visible",
    created_at: datetime | None = None,
) -> Comment:
    """One Viafoura comment, with only the fields this project reads set meaningfully."""
    return Comment(
        uuid=uuid,
        container_uuid="container-1",
        parent_uuid="parent-1" if is_reply else "container-1",
        thread_uuid="thread-1",
        is_reply=is_reply,
        text=text,
        created_at=created_at or datetime(2026, 9, 21, 14, 32, tzinfo=UTC),
        actor_uuid="actor-1",
        likes=likes,
        dislikes=0,
        total_replies=2,
        is_pinned=is_pinned,
        is_picked=is_picked,
        is_top_comment=False,
        state=state,
        article_title="Test article",
        article_url="https://www.telegraph.co.uk/news/2026/09/21/test/",
        raw={},
    )


def make_article(body_words: int = 600) -> Article:
    return Article(
        url="https://www.telegraph.co.uk/news/2026/09/21/test/",
        headline="A test headline",
        standfirst="A test standfirst",
        body=" ".join(["word"] * body_words),
        page_id="A65vpYj1jg7t",
    )


def make_thread(comments: list[Comment] | None = None) -> ArticleThread:
    return ArticleThread(
        article=make_article(),
        comments=tuple(make_comment() if comments is None else comments),
        container_uuid="container-1",
    )


def make_record(
    comment: Comment,
    answers: dict[str, Any] | None = None,
) -> Classification:
    record = Classification(comment=comment, signals=compute_signals(comment.text))
    if answers is not None:
        record.answers = dict(answers)
    return record


class FakeResponse:
    """The shape of `SystemOneResponse` that `_read_answers` actually reads."""

    def __init__(self, answers: dict[str, Any], model: str = "jev-1.13.0") -> None:
        self.model = model
        self.usage = type("U", (), {"input_tokens": 3200, "output_tokens": 0})()
        self.answers = {qid: _FakeAnswer(qid, value) for qid, value in answers.items()}


class _FakeAnswer:
    """Carries only the attribute its answer type really has.

    A Noul answer has `.noul` and a Score answer has `.score`; the real SDK types do not
    carry both. Setting both here meant a question moved between `NOUL_IDS` and
    `SCORE_IDS` in questions.py could never fail a test.
    """

    def __init__(self, question_id: str, value: Any) -> None:
        if isinstance(value, dict):
            self.choice = value["choice"]
            self.confidence = value.get("confidence", 0.9)
            self.probabilities = value.get("probabilities", {})
        elif question_id in NOUL_IDS:
            self.noul = value
            self.confidence = 0.9
        else:
            self.score = value
            self.confidence = 0.9


class FakeJev:
    """A `JevClient` that answers from a script instead of the API.

    `answers_for` maps a comment's text to its answers; anything unmatched gets
    `default`. Every call is counted, so a test can assert that the store prevented one.
    """

    def __init__(
        self,
        default: dict[str, Any] | None = None,
        answers_for: dict[str, dict[str, Any]] | None = None,
        *,
        fail_on: set[str] | None = None,
        model: str = "jev-1.13.0",
    ) -> None:
        self.default = default if default is not None else GOOD_ANSWERS
        self.answers_for = answers_for or {}
        self.fail_on = fail_on or set()
        self.model = model
        self.calls: list[str] = []
        self.successes = 0
        self._input_tokens = 0

    async def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, Any],
    ) -> FakeResponse:
        text = state["comment"]["text"]
        self.calls.append(text)
        if text in self.fail_on:
            msg = "simulated Jev failure"
            raise RuntimeError(msg)
        # A real call yields to the loop; doing the same here keeps concurrency honest.
        await asyncio.sleep(0)
        self._input_tokens += 3200
        self.successes += 1
        return FakeResponse(self.answers_for.get(text, self.default), self.model)

    def spent(self) -> Spend:
        return Spend(
            input_tokens=self._input_tokens,
            output_tokens=0,
            requests=len(self.calls),
            # Empty until a call succeeds, exactly like the real client. Hardcoding the
            # model here made a fully-reused run look like it had one when it did not.
            models=frozenset({self.model}) if self.successes else frozenset(),
        )

    def since(self, mark: Spend) -> Spend:
        now = self.spent()
        return Spend(
            input_tokens=now.input_tokens - mark.input_tokens,
            output_tokens=now.output_tokens - mark.output_tokens,
            requests=now.requests - mark.requests,
            models=now.models,
        )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a real network call impossible, rather than merely discouraged.

    `tests/test_guards.py` greps for live clients, which catches the obvious cases and
    nothing else. This closes the socket itself: any test that tries to reach the
    network fails loudly instead of quietly spending money.
    """
    import socket

    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = (
            "A test tried to open a socket. The suite must never reach a live service: "
            "use FakeJev, or mark the test @pytest.mark.live."
        )
        raise RuntimeError(msg)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def fake_jev() -> FakeJev:
    return FakeJev()


@pytest.fixture
def state_dir(tmp_path):  # noqa: ANN001, ANN201 - pytest's tmp_path fixture
    """An empty store directory, thrown away after each test."""
    directory = tmp_path / "state"
    directory.mkdir()
    return directory
