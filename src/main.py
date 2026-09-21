"""Manual test harness for the Viafoura MCP client.

Run from the project root:

    uv run python src/main.py                  # uses ARTICLE below
    uv run python src/main.py <article URL>    # any Telegraph article URL
    uv run python src/main.py <page id>        # or its page id, e.g. A65xRHy7KY6g
    uv run python src/main.py <container UUID> # or the Viafoura container UUID

It resolves the article, fetches the top comments, the comments posted in the last few
hours, and a capped sample of the whole thread, prints a summary and writes the sample
to ``output/``. Tweak the constants below rather than adding CLI flags.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from clients.vf_mcp import (
    Comment,
    ViafouraMCPClient,
    ViafouraMCPError,
    comments_to_dicts,
)

# Article to test with: a URL, a Telegraph page id, or a Viafoura container UUID.
ARTICLE = "https://www.telegraph.co.uk/health-fitness/conditions/ageing/longevity-lessons-britain-can-learn-from-singapore/"

# Top comments by likes.
TOP_N = 5
# Window, in hours, for the "posted since" fetch.
RECENT_HOURS = 3
# Pages of 100 top-level comments for the full fetch; None fetches the whole thread.
MAX_PAGES: int | None = 3
# Where the JSON dump goes; None skips writing.
OUTPUT_DIR: Path | None = Path("output")

# Fields worth showing from the container record; the rest is moderation settings.
_CONTAINER_FIELDS = (
    "container_id",
    "content_container_uuid",
    "total_visible_content",
    "total_visible_pinned_content",
    "total_visible_picked_content",
    "total_visible_top_content",
)


def preview(comment: Comment, width: int = 90) -> str:
    kind = "reply" if comment.is_reply else "TOP  "
    flags = "".join(
        f for f, on in (("P", comment.is_pinned), ("K", comment.is_picked)) if on
    )
    text = " ".join(comment.text.split())[:width]
    return (
        f"  {kind} {comment.created_at:%d %b %H:%M} likes={comment.likes:<5}"
        f" replies={comment.total_replies:<3} {flags:<2} {text}"
    )


def show(title: str, comments: list[Comment], limit: int = 5) -> None:
    top_level = sum(not c.is_reply for c in comments)
    print(f"\n{title}: {len(comments)} comments ({top_level} top-level)")
    for comment in comments[:limit]:
        print(preview(comment))
    if len(comments) > limit:
        print(f"  ... {len(comments) - limit} more")


async def describe(vf: ViafouraMCPClient, article: str) -> str | None:
    """Resolve the article to a container UUID and print what Viafoura knows about it.

    Returns None (after explaining) when the article cannot be resolved.
    """
    try:
        uuid = await vf.resolve_container_uuid(article)
        record = await vf.get_container(article)
    except ViafouraMCPError as exc:  # covers an unreachable page and an unknown id
        print(f"\nCould not resolve {article!r}:\n  {exc}")
        print(
            "\nCheck the URL is a live Telegraph article with comments enabled. "
            "Articles that are currently active, for reference:",
        )
        trending = await vf.call_tool("get_trending_containers", {"limit": 5})
        items = trending.get("trending", []) if isinstance(trending, dict) else []
        for item in items:
            if isinstance(item, dict):
                print(f"  {item.get('container_id')!s:<16} {item.get('origin_url')}")
        return None

    print(f"\nResolved {article}\n      -> {uuid}")
    if isinstance(record, dict):
        summary = {k: record[k] for k in _CONTAINER_FIELDS if k in record}
        print(f"Container: {json.dumps(summary)}")
    return uuid


async def main(article: str) -> None:
    load_dotenv(override=True)
    started = datetime.now(UTC)

    async with ViafouraMCPClient() as vf:
        print(f"Connected; server tools: {', '.join(await vf.list_tools())}")

        uuid = await describe(vf, article)
        if uuid is None:
            return

        top = await vf.get_comments(uuid, limit=TOP_N)
        show(f"Top {TOP_N} by likes", top, TOP_N)

        since = started - timedelta(hours=RECENT_HOURS)
        recent = await vf.get_comments_in_range(uuid, since=since)
        show(f"Posted in the last {RECENT_HOURS}h (since {since:%H:%M} UTC)", recent)

        sample = await vf.get_all_comments(uuid, max_pages=MAX_PAGES)
        label = "All comments" if MAX_PAGES is None else f"First {MAX_PAGES} page(s)"
        show(label, sample)

    if top:
        first = top[0]
        print(f"\nArticle: {first.article_title}\n         {first.article_url}")

    if OUTPUT_DIR is not None and sample:
        path = save_json(sample, OUTPUT_DIR / f"comments_{uuid}.json")
        print(f"\nWrote {len(sample)} comments to {path}")

    print(f"\nDone in {(datetime.now(UTC) - started).total_seconds():.1f}s")


def save_json(comments: list[Comment], path: Path) -> Path:
    """Write the comments as a JSON list; a small sync write, done after the fetches."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(comments_to_dicts(comments), indent=1, ensure_ascii=False),
    )
    return path


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ARTICLE))
