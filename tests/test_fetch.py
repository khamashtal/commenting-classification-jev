"""The fetch stage: CAPI parsing, and the no-scraping rule it now depends on."""

from __future__ import annotations

import pytest
from conftest import make_comment

from clients.vf_mcp import ViafouraMCPClient, ViafouraMCPError
from processing.fetch import (
    Article,
    ArticleThread,
    article_from_ucm,
    canonical_url,
)


class TestCanonicalUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://t.co.uk/a/b?x=1", "https://t.co.uk/a/b"),
            ("https://t.co.uk/a/b#vf-123", "https://t.co.uk/a/b"),
            ("https://t.co.uk/a/b?x=1#vf-9", "https://t.co.uk/a/b"),
            ("https://t.co.uk/a/b", "https://t.co.uk/a/b"),
        ],
    )
    def test_query_and_fragment_are_stripped(self, raw: str, expected: str) -> None:
        """Viafoura hands back #vf- anchors; CAPI matches on an exact string."""
        assert canonical_url(raw) == expected


class TestUcmParsing:
    def test_page_id_is_read_from_metadata(self) -> None:
        """This is what replaced scraping the vf:container_id meta tag."""
        ucm = {"metadata": {"page-id": "A65vpYj1jg7t"}, "content": {"headline": "H"}}
        assert article_from_ucm(ucm, "https://t.co.uk/a").page_id == "A65vpYj1jg7t"

    def test_missing_page_id_is_empty_not_an_error(self) -> None:
        article = article_from_ucm({"content": {}}, "https://t.co.uk/a")
        assert article.page_id == ""

    def test_only_text_blocks_become_body(self) -> None:
        ucm = {
            "metadata": {"page-id": "A1"},
            "content": {
                "headline": "H",
                "standfirst": "S",
                "body": [
                    {"type": "text", "data": "First para."},
                    {"type": "image", "data": "ignored.jpg"},
                    {"type": "heading", "data": "A heading"},
                    {"type": "text", "data": "Second para."},
                    {"type": "text"},  # no data at all
                ],
            },
        }
        article = article_from_ucm(ucm, "https://t.co.uk/a")
        assert "ignored.jpg" not in article.body
        assert article.body.split("\n\n") == [
            "First para.",
            "A heading",
            "Second para.",
        ]

    def test_empty_ucm_does_not_raise(self) -> None:
        article = article_from_ucm({}, "https://t.co.uk/a")
        assert article.headline == ""
        assert article.body == ""


class TestArticle:
    def test_body_is_capped_on_a_word_boundary(self) -> None:
        article = Article(
            url="u",
            headline="h",
            standfirst="s",
            body=" ".join(str(i) for i in range(1000)),
        )
        capped = article.capped_body(10)
        assert capped.endswith("[…]")
        assert len(capped.split()) == 11

    def test_short_body_is_returned_unchanged(self) -> None:
        article = Article(url="u", headline="h", standfirst="s", body="one two three")
        assert article.capped_body(100) == "one two three"

    def test_word_count(self) -> None:
        assert (
            Article(url="u", headline="h", standfirst="s", body="a b c").body_word_count
            == 3
        )


class TestParentText:
    def test_reply_is_linked_to_its_parent(self) -> None:
        parent = make_comment(
            "p-1",
            "The parent comment, long enough to be classified "
            "properly by the rules this pipeline applies.",
        )
        reply = make_comment(
            "r-1",
            "A reply to the above, also long enough to clear "
            "the minimum word count that applies here.",
            is_reply=True,
        )
        reply.parent_uuid = "p-1"
        thread = ArticleThread(
            article=Article(url="u", headline="h", standfirst="s", body="b"),
            comments=(parent, reply),
            container_uuid="c",
        )
        assert thread.parent_text == {"r-1": parent.text}

    def test_orphaned_reply_simply_has_no_parent(self) -> None:
        reply = make_comment(
            "r-1",
            "A reply whose parent was not in this page of "
            "results, which must not raise an error here.",
            is_reply=True,
        )
        reply.parent_uuid = "missing"
        thread = ArticleThread(
            article=Article(url="u", headline="h", standfirst="s", body="b"),
            comments=(reply,),
            container_uuid="c",
        )
        assert thread.parent_text == {}


class TestNoScraping:
    async def test_the_client_refuses_a_url(self) -> None:
        """Resolving a URL meant fetching the page; that route is gone."""
        client = ViafouraMCPClient("key")
        with pytest.raises(ViafouraMCPError, match="URL"):
            await client._to_container_id("https://www.telegraph.co.uk/news/x/")

    async def test_an_id_passes_through_untouched(self) -> None:
        client = ViafouraMCPClient("key")
        assert await client._to_container_id("A65vpYj1jg7t") == "A65vpYj1jg7t"

    def test_the_scraping_helpers_are_gone(self) -> None:
        import clients.vf_mcp as vf

        for name in ("container_id_from_url", "container_id_from_html"):
            assert not hasattr(vf, name), f"{name} was removed; it must not come back"
