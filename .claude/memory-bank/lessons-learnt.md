# Lessons learnt

Things that cost time to discover. All verified on 21 September 2026 unless noted.

---

## Viafoura

### The MCP server's id-lookup tools are broken

`get_content_container`, `get_content_containers`, `get_top_comments`, `get_comment_count`
and `search_comments` return **"Container not found" for every identifier**, including the
`container_id` values the server's own `get_trending_containers` tool hands back. Also
tried: the container UUID, the full article URL, and the URL path. All fail.

`get_comments` with `content_container_uuid` works perfectly. So does everything else that
takes a UUID.

**Workaround, already implemented in `clients/vf_mcp.py`:** resolve ids through Viafoura's
public Live Comments API, which needs no authentication.

```
GET https://livecomments.viafoura.co/v4/livecomments/{section_uuid}?container_id=<id>&limit=0
```

It returns `content_container_uuid` plus a `container` record with comment totals, and
404s on an unknown id. Worth raising with Viafoura as a server bug.

### The Telegraph page id *is* the Viafoura container id

Every article exposes it in `<meta property="vf:container_id" content="A65xRHy7KY6g">`.
`container_id_from_url()` reads it. So a URL, a page id and a UUID are all accepted by the
client.

### The Telegraph section UUID

`00000000-0000-4000-8000-010fdf3f0a45`. Set as `VF_SECTION_UUID` in `.env` to skip a
discovery call. It identifies the site your API key is scoped to, and is not a secret.

### The API key is not a bearer token

The server uses OAuth authorization code with PKCE and dynamic client registration.
Passing the key as `Authorization: Bearer` returns `invalid_token`. Its `/authorize` page
is a plain HTML form with an `api_key` field that 302s back with the code, which is why
the client can complete the whole flow **headlessly**, with no browser and no callback
server. Tokens are cached in `~/.jev_ai/vf_mcp_tokens.json`.

### `get_comments` shape

- Response is `{"more_available": bool, "contents": [...]}`, a **flat** list with replies
  inline after their parent. A comment is top-level when `parent_uuid ==
  content_container_uuid`.
- `limit` (max 100) counts **top-level comments only**; replies come on top of it.
- Paginate with `starting_from` set to the last **top-level** comment's uuid.
- **Pinned comments float to the top of every sort order**, including "oldest". Code that
  reasons about recency must skip them.
- There is no server-side time filter. The client pages newest-first and stops.
- No usernames. Only an anonymous `actor_uuid`.

---

## CAPI

`get_ucms([url], session, settings)` returns `{url: ucm}`. The fields that matter:

| Field | Path |
| --- | --- |
| Headline | `content.headline` |
| Standfirst | `content.standfirst` |
| Body | `content.body[]` where `type` is `text` or `heading`, joining `data` |

`data` is already plain text; `html-data` is the marked-up twin and is ignored. A test
article gave 37 blocks reconstructing to 1,156 words, complete, with no paywall
truncation.

**CAPI matches on the exact URL**, so strip the fragment and query first. Viafoura hands
back links carrying a `#vf-…` comment anchor, which will not match.

---

## Python and tooling

### `PYTHONPATH=src` is required for anything under `processing/`

Python puts the **script's own directory** on `sys.path`, so running
`src/processing/workflow.py` directly gives it `src/processing` and `from clients.capi
import …` fails. Confirmed by test. Either prefix `PYTHONPATH=src`, or run as a module
from inside `src`.

### The MCP Python SDK 2.x uses snake_case

`input_schema`, `is_error`, `structured_content`, not the camelCase of the 1.x docs. It
also imports `httpx2`, not `httpx`.

### Ruff here is strict

The project config is a copy of Luis's user-level one and enables ANN (full type hints, no
bare `Any` in signatures), COM (trailing commas), S (bandit), DTZ (timezone-aware
datetimes), ASYNC, FBT and ARG. Run `uv run ruff check --fix . && uv run ruff format .`
before saying anything is done. Recurring hits: missing `-> None` on `__init__`, `Any` in
a signature, and `ASYNC240` for `pathlib` calls inside an async function (wrap the write in
`asyncio.to_thread`).

### A formatter rewrites files on save

Files change on disk between your edits. Re-read before editing, and do not be surprised
when a file you just wrote comes back reformatted.

### Never read `.env`

A standing rule in `CLAUDE.md`, at Luis's request. The variable names are documented there
so the file never needs opening. The same goes for the OAuth token cache.

---

## Jev itself

### Known weak spots, from TypeSafe's own jaggedness page

Literal reading (it answers the question as written, not as intended), counting,
arithmetic, date comparison, indirection, and large states full of irrelevant detail. It
can also be steered by text arguing for its own classification, which matters directly for
the sarcasm question.

### Our own observations

- **`profanity_or_threat` over-fires on insults.** Scored 0.91 on a comment with no
  swearing at all. Needs its criteria narrowed.
- **`unverified_claim` fires often.** Some correctly per the brief, some on ordinary
  assertions. Either tighten the level-2 criteria or raise the 1.5 threshold.
- **`personal_experience` is accurate.** Scored 2.97/3 on a genuine 190-word first-hand
  business story, and 0.00 on opinion comments. This question works.
- Structural invariants do not hold. A Noul and an equivalent yes/no Choice give different
  numbers, and a question and its negation do not sum to 1. Do not carry a threshold from
  one question type to another.

### Numbers worth remembering

| | |
| --- | --- |
| Price | $0.042 per million input tokens; output free |
| Context | 64k per request, 32k for state plus the longest question |
| Rate limits | 1,200 requests/minute, 250k tokens/second (changing without notice) |
| Latency | About 100ms per request |
| Observed cost | $0.0025 for 20 comments including a 600-word article each time |

The article is re-sent with every comment and **dominates token cost**, which is why
`ARTICLE_MAX_WORDS` exists.
