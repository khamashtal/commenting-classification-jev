"""Fetch stage: one article from CAPI, its comments from Viafoura, both in memory.

Nothing here writes a file. The clients are passed in rather than constructed, so the
same functions serve a CLI run and a FastAPI request handler (see the spec, §3.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from clients.capi import get_ucms
from clients.viafoura import Comment, SortOrder, ViafouraClient
from processing.log_config import logger
from processing.settings import Settings

# UCM body blocks that carry article prose. Images and call-to-action blocks are dropped.
_TEXT_BLOCK_TYPES = frozenset({"text", "heading"})


class ArticleNotFoundError(RuntimeError):
    """CAPI returned no content for the requested URL."""


@dataclass(frozen=True, slots=True)
class Article:
    """The parts of an article Jev needs, extracted from a CAPI UCM document."""

    url: str
    headline: str
    standfirst: str
    body: str
    page_id: str = ""
    """Telegraph page id, which is also the Viafoura ``container_id``.

    Taken from CAPI. The alternative is scraping the article's ``vf:container_id`` meta
    tag, which returns HTTP 402 on any premium article — so this is both cheaper and the
    only route that works behind the paywall.
    """

    @property
    def body_word_count(self) -> int:
        return len(self.body.split())

    def capped_body(self, max_words: int) -> str:
        """The body truncated to ``max_words``, on a word boundary.

        The article is re-sent with every comment, so it dominates token cost, and Jev's
        accuracy falls as the state fills with material a question does not need.
        """
        words = self.body.split()
        if len(words) <= max_words:
            return self.body
        return " ".join(words[:max_words]) + " […]"


@dataclass(frozen=True, slots=True)
class ArticleThread:
    """An article and the comments to classify against it."""

    article: Article
    comments: tuple[Comment, ...]
    container_uuid: str

    @property
    def parent_text(self) -> dict[str, str]:
        """Map each reply's uuid to its parent's text, for the ``standalone`` question.

        Replies arrive nested under their parent in the same page, so the parent is
        almost always in this batch. When it is not, the reply simply gets no parent.
        """
        by_uuid = {c.uuid: c.text for c in self.comments}
        return {
            c.uuid: by_uuid[c.parent_uuid]
            for c in self.comments
            if c.is_reply and c.parent_uuid in by_uuid
        }


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def canonical_url(url: str) -> str:
    """Strip the fragment and query so the URL matches what CAPI indexed.

    Viafoura hands back links carrying a ``#vf-…`` comment anchor; CAPI matches on an
    exact string, so those must go.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def article_from_ucm(ucm: dict[str, Any], url: str) -> Article:
    """Pull headline, standfirst and body text out of a CAPI UCM document."""
    content = ucm.get("content", {})
    blocks = content.get("body", []) or []
    paragraphs = [
        block["data"]
        for block in blocks
        if block.get("type") in _TEXT_BLOCK_TYPES and isinstance(block.get("data"), str)
    ]
    return Article(
        url=url,
        headline=content.get("headline", "") or "",
        standfirst=content.get("standfirst", "") or "",
        # `data` is already plain text; `html-data` is the marked-up twin and is ignored.
        body="\n\n".join(paragraphs),
        page_id=str(ucm.get("metadata", {}).get("page-id", "") or ""),
    )


async def fetch_article(
    url: str,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> Article:
    """Fetch one article from CAPI and extract the fields Jev needs."""
    target = canonical_url(url)
    ucms = await get_ucms([target], session, settings)
    ucm = ucms.get(target)
    if ucm is None:
        raise ArticleNotFoundError(f"CAPI returned no content for {target}")
    article = article_from_ucm(ucm, target)
    logger.info(
        "Fetched article %r (%d words of body)",
        article.headline,
        article.body_word_count,
    )
    return article


async def fetch_comments(
    vf: ViafouraClient,
    article_ref: str,
    limit: int | None,
    sorted_by: SortOrder = "num_likes_desc",
) -> list[Comment]:
    """Fetch the top ``limit`` comments for an article, or every comment when ``None``.

    ``article_ref`` is a URL, a Telegraph page id, or a Viafoura container UUID.

    The two modes return different populations, not just different counts: ``limit=N``
    gives the first N *top-level* comments in ``sorted_by`` order, while ``None`` pages
    the whole thread and includes replies. Replies are classified like any other comment
    — `standalone` and the parent text in the state exist for exactly that case.
    """
    comments = await vf.get_comments(article_ref, limit=limit, sorted_by=sorted_by)
    logger.info("Fetched %d comments for %s", len(comments), article_ref)
    return comments


async def fetch_thread(
    *,
    vf: ViafouraClient,
    session: aiohttp.ClientSession,
    settings: Settings,
    article_ref: str,
    limit: int | None,
    sorted_by: SortOrder = "num_likes_desc",
) -> ArticleThread:
    """Fetch an article and its comments.

    Given a URL, the article comes first: CAPI hands back the Telegraph ``page-id``,
    which is the Viafoura ``container_id``. **No article page is ever fetched.** The
    earlier route read the ``vf:container_id`` meta tag out of the page HTML, which is
    scraping and returns HTTP 402 on every premium article; CAPI is the supported source
    for the same value and works regardless of the paywall.

    The two calls therefore run in sequence rather than concurrently. One CAPI round trip
    is a small price for a path that works on paywalled articles; the old concurrent
    version only ever worked on free ones.

    Given a page id or container UUID, the comments come first because their metadata is
    what tells us the article URL.
    """
    if _is_url(article_ref):
        article = await fetch_article(article_ref, session, settings)
        if not article.page_id:
            raise ArticleNotFoundError(
                f"CAPI returned no `metadata.page-id` for {article_ref}, so the "
                "Viafoura container cannot be resolved. Nothing here falls back to "
                "reading the article page: that is scraping, and it fails on every "
                "paywalled article anyway. Pass the page id or container UUID directly.",
            )
        comments = await fetch_comments(vf, article.page_id, limit, sorted_by)
        container_ref = article.page_id
    else:
        container_ref = article_ref
        comments = await fetch_comments(vf, article_ref, limit, sorted_by)
        if not comments:
            raise ArticleNotFoundError(
                f"No comments found for {article_ref!r}, so the article URL is unknown. "
                "Pass the article URL instead.",
            )
        url = comments[0].article_url
        if not url:
            raise ArticleNotFoundError(
                f"Viafoura returned no article URL for {article_ref!r}. "
                "Pass the article URL instead.",
            )
        article = await fetch_article(url, session, settings)

    container_uuid = await vf.resolve_container_uuid(container_ref)
    return ArticleThread(
        article=article,
        comments=tuple(comments),
        container_uuid=container_uuid,
    )
