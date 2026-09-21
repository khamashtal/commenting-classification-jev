#!/usr/bin/env python
"""Run one or more comments through the current Jev battery and print every answer.

The fast feedback loop for tuning question wording: edit `src/processing/questions.py`,
re-run this, and see whether the number moved the way you predicted. A single comment
takes about a second, against several seconds for a full pipeline run.

Usage (from the project root, PYTHONPATH must include src):

    PYTHONPATH=src uv run python .claude/skills/jev-question-tuning/scripts/try_question.py \\
        --text "the comment to test"

    # several comments from a file, one per line, blank lines and # comments ignored
    PYTHONPATH=src uv run python .claude/skills/jev-question-tuning/scripts/try_question.py \\
        --file cases.txt --only sarcasm --only personal_attack

    # with real article context, which five of the questions need
    PYTHONPATH=src uv run python .claude/skills/jev-question-tuning/scripts/try_question.py \\
        --text "..." --article https://www.telegraph.co.uk/...

Without --article the article fields are placeholders, which is fine for questions that
only read the comment (sarcasm, tone, readability, personal_attack, profanity_or_threat)
and misleading for those that compare it with the article (on_topic, stance,
experience_relevant, unverified_claim, proposes_solution).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import aiohttp
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

from processing.fetch import Article, fetch_article
from processing.questions import BATTERY, build_state
from processing.settings import Settings, load_settings

PLACEHOLDER = Article(
    url="",
    headline="(no article supplied)",
    standfirst="",
    body="(no article supplied — questions comparing the comment with the article "
    "cannot be judged)",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="A single comment to test")
    source.add_argument(
        "--file",
        type=Path,
        help="A file of comments, one per line; blank lines and # lines are ignored",
    )
    parser.add_argument(
        "--article",
        help="Article URL for real context. Without it the article fields are placeholders.",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="QUESTION_ID",
        help="Only show these question ids. Repeatable. Default: all of them.",
    )
    parser.add_argument(
        "--parent",
        help="Parent comment text, when testing a reply (affects `standalone`)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Pin a Jev version; omitted, this matches the pipeline and uses the default",
    )
    parser.add_argument(
        "--article-words",
        type=int,
        default=600,
        help="Words of article body to send (default 600, matching the pipeline)",
    )
    return parser.parse_args()


def load_comments(args: argparse.Namespace) -> list[str]:
    if args.text:
        return [args.text]
    lines = args.file.read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]


async def get_article(url: str | None, settings: Settings) -> Article:
    if not url:
        return PLACEHOLDER
    async with aiohttp.ClientSession() as session:
        return await fetch_article(url, session, settings)


def format_answer(question_id: str, response: object) -> str:
    """One line per question, shaped to the question type."""
    answer = response.answers[question_id]  # type: ignore[attr-defined]
    question = BATTERY[question_id]
    if isinstance(question, Noul):
        value = answer.noul
        return f"  {question_id:<22} {value:.2f}  {_bar(value)}"
    if isinstance(question, Score):
        top = len(question.criteria) - 1
        spread = " ".join(
            f"{level}:{probability:.2f}"
            for level, probability in sorted(answer.probabilities.items())
        )
        return (
            f"  {question_id:<22} {answer.score:.2f}/{top}  "
            f"{_bar(answer.score / top)}  conf {answer.confidence:.2f}  [{spread}]"
        )
    if isinstance(question, Choice):
        spread = " ".join(
            f"{option}:{probability:.2f}"
            for option, probability in sorted(
                answer.probabilities.items(),
                key=lambda kv: kv[1],
                reverse=True,
            )
        )
        return (
            f"  {question_id:<22} {answer.choice}  conf {answer.confidence:.2f}  "
            f"[{spread}]"
        )
    return f"  {question_id:<22} (unknown question type)"


def _bar(fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "#" * filled + "." * (width - filled)


async def main() -> int:
    args = parse_args()
    settings = load_settings()
    comments = load_comments(args)
    if not comments:
        print("No comments to test.", file=sys.stderr)
        return 1

    question_ids = args.only or list(BATTERY)
    unknown = [qid for qid in question_ids if qid not in BATTERY]
    if unknown:
        print(
            f"Unknown question id(s): {', '.join(unknown)}.\n"
            f"Available: {', '.join(BATTERY)}",
            file=sys.stderr,
        )
        return 1

    article = await get_article(args.article, settings)
    body = article.capped_body(args.article_words)
    if article is PLACEHOLDER:
        print("No --article given: article-relative answers are not meaningful.\n")
    else:
        print(f"Article: {article.headline}")
        print(f"Body sent: {len(body.split())} of {article.body_word_count} words\n")

    total_tokens = 0
    async with AsyncTypeSafeClient(api_key=settings.typesafe_api_key) as client:
        for index, text in enumerate(comments, start=1):
            state = build_state(
                headline=article.headline,
                standfirst=article.standfirst,
                body=body,
                comment_text=text,
                parent_text=args.parent,
            )
            response = await client.system_one(
                state=state,
                questions=BATTERY,
                model=args.model,
            )
            total_tokens += response.usage.input_tokens or 0
            preview = " ".join(text.split())
            print(f"[{index}] {preview[:100]}{'…' if len(preview) > 100 else ''}")
            print(f"    ({len(text.split())} words)")
            for question_id in question_ids:
                print(format_answer(question_id, response))
            print()

    cost = total_tokens * 0.042 / 1_000_000
    print(
        f"{len(comments)} comment(s), {total_tokens:,} input tokens, about ${cost:.5f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
