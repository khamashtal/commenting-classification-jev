# jev_ai

**New here? Read `.claude/memory-bank/` first** — `active-context.md` for where things
stand and what to do next, then `progress.md`, `decision-log.md` and `lessons-learnt.md`.
The full spec is `.claude/comment-classification-spec.md`. To change a Jev question, use
the `jev-question-tuning` skill. To audit a classification report, use the
`comment-quality-auditor` agent.

Proof of concept for The Telegraph's Community team: automatically surface reader
comments worth pinning or putting in carousels. Comments come from Viafoura (via its
Comments MCP server); the quality judgments come from TypeSafe's Jev model (a decision
model that returns calibrated probabilities, not text). Deterministic work (word counts,
URL checks, all-caps ratio, thresholds, weights) stays in code.

## Tooling

- Python 3.12, managed with **uv**. Never call `pip` or the venv's python directly:
  `uv sync`, `uv run python …`, `uv add <pkg>` (`uv add --dev` for tooling).
- **ruff** for linting and formatting. Config is `ruff.toml` at the project root (a copy
  of Luis's user-level config, so the project is self-contained). Notable rules: ANN
  (full type hints, no bare `Any` in signatures), COM (trailing commas), S (bandit), DTZ
  (timezone-aware datetimes), ASYNC, FBT, ARG. E501 is ignored; the formatter wraps at 88.
- Before finishing any change run both and fix everything they flag:
  `uv run ruff check --fix . && uv run ruff format .`
## Secrets: never read `.env`

**Do not open, read, cat, grep, copy, edit or print `.env` (or `.env.*`), for any reason,
even redacted, even when the user seems to ask for it.** If a value is needed, read it at
runtime through `os.environ` inside code that the user runs; if a variable seems missing
or wrong, say so and ask the user to check it themselves. The same applies to the OAuth
token cache (`~/.jev_ai/vf_mcp_tokens.json`), which holds live bearer tokens.

Read settings through `processing.settings`, never `os.environ` scattered through the
code: `load_settings()` once at startup (in `main()`, or a FastAPI lifespan handler),
`get_settings()` everywhere else. Functions take a `Settings` argument rather than calling
`get_settings()` themselves, which keeps them testable and request-agnostic. The same
applies to long-lived clients (`aiohttp.ClientSession`, `ViafouraMCPClient`,
`AsyncTypeSafeClient`): build once at startup, pass down, never construct per call.

The variables the code expects, documented here so the file never has to be opened:

| Variable | Used for |
| --- | --- |
| `api_key` (or `TYPESAFE_API_KEY`) | TypeSafe / Jev API key |
| `vf_key` (or `VF_MCP_API_KEY`) | Viafoura Comments MCP API key |
| `VF_SECTION_UUID` | Viafoura section (site) UUID; skips a discovery call |
| `CAPI_URL`, `CONTENT_READER_APIGEE_KEY` | Telegraph CAPI, for article text |
| `ENVIRONMENT` | `dev` / `prod`; sets the log level |
| `VF_MCP_URL`, `VF_MCP_TOKEN_STORE`, `VF_LIVECOMMENTS_URL` | Optional overrides |

Never print, log or commit secret values, and keep `.env` and `~/.jev_ai/` out of git.

## Git: read-only, never write

**Luis is the only one who commits, merges and pushes.** Leave every change in the working
tree and say what was changed, for Luis to review and commit. Do not offer to commit, and
do not treat "that's done" or a clean lint run as permission to.

Reading history is fine and encouraged: `git log`, `git show`, `git diff`, `git status`,
`git blame`, `git ls-files`, `git check-ignore`.

**The enforcement lives in `.claude/settings.json`**, which denies every writing git and
`gh` command outright, so the full list is not repeated here. This section states the
intent, because a rule that exists only as a permission denial is invisible to anyone
reading the code, and because the denial only binds this harness — the intent binds
wherever this file is read.

One case the deny list cannot express: discarding work. To undo an edit, edit the file
back. If that is not practical, say so and let Luis decide rather than reaching for
`git checkout --` or `git reset`.

## Never scrape a page

**Do not fetch an article page, or any other HTML, to extract data from it.** Everything
this project needs about an article comes from CAPI: the body text, and the Telegraph
`page-id` that doubles as the Viafoura `container_id` (`metadata.page-id`, surfaced as
`Article.page_id`). CAPI is the supported source, needs no HTML parsing, and works on
premium articles — an anonymous page fetch returns **HTTP 402** on most of them, so
scraping was never a working route, only one that happened to survive on free articles.

`ViafouraMCPClient` therefore refuses a URL outright and takes a page id or container
UUID. If something seems to need a page, the answer is another CAPI field or another API,
not a request for the HTML.

## Layout

- `src/processing/`: the classification pipeline. `workflow.py` orchestrates and renders
  the Markdown report, `fetch.py` gets article + comments, `questions.py` holds the Jev
  battery (the tuning surface), `classification.py` holds signals, thresholds and weights.
  Run: `uv run python src/processing/workflow.py [article]`, from any directory in the
  project. Spec: `.claude/comment-classification-spec.md`.
- `src/main.py`: manual test harness for the Viafoura client (`uv run python src/main.py
  [container_id]`); constants at the top control what it fetches.
- `src/clients/vf_mcp.py`: Viafoura Comments MCP client (see below). Python API only,
  no CLI; async core plus sync one-shot helpers.
- `src/clients/jev.py`: TypeSafe/Jev client. Wraps the SDK with the rate limiting the
  published limits need (token buckets for both 1,200 req/min and 250k tok/s, halving on
  an observed 429 and recovering on a clean streak), a concurrency bound, a retry policy
  sized for long runs, and usage/model accounting. `async with JevClient(key) as jev:`
  then `await jev.ask(state, questions)`. It knows nothing about comments: the battery,
  the thresholds and the weights stay in `processing/`.
- `experiment_1.py` (root): TypeSafe experiment (Jev Choice question over persona data).
- `src/processing/store.py`: what survives between runs — one JSON file per article
  holding each comment's Jev answers, keyed by comment uuid, so a second run pays only
  for comments it has never seen. There is deliberately no timestamp cursor. The file
  also carries a fingerprint of the battery and `ARTICLE_MAX_WORDS`: change a question
  and the cache is discarded rather than silently misranking the thread. A run holds an
  exclusive lock on the article, because two concurrent runs would double-spend and one
  would lose its answers. See the module docstring and
  `.claude/incremental-classification-spec.md`.
- `output/`: reports, one per article, rewritten in place; disposable.
- `state/`: the store. **Not** disposable — deleting it means paying Jev again for every
  comment. Gitignored.
- `tests/`: the suite. No Jev calls, ever.
- Add new modules under `src/`; import as `from clients.vf_mcp import …`. `pyproject.toml`
  installs `src/clients` and `src/processing` into the venv as editable packages, so imports
  resolve regardless of the working directory. A new top-level package under `src/` must be
  added to `[tool.hatch.build.targets.wheel] packages` and then `uv sync` re-run.

## Viafoura Comments MCP (verified 2026-09-21)

- Server: `https://comments-mcp.viafoura.co/mcp`, official `mcp` Python SDK (2.x, uses
  `httpx2`; SDK model fields are snake_case, e.g. `input_schema`, `is_error`).
- Auth is OAuth authorization-code + PKCE with dynamic registration. The API key is NOT a
  bearer token; the `/authorize` page is an HTML form with field `api_key` that 302s back
  with the code. `vf_mcp.py` submits that form headlessly through the SDK's
  `OAuthClientProvider` and caches tokens in `VF_MCP_TOKEN_STORE`.
- The MCP server's id-based tools (`get_content_container(s)`, `get_top_comments`,
  `get_comment_count`, `search_comments`) return "Container not found" for every
  identifier, including the `container_id` values the server's own trending tool
  returns. `get_comments` by `content_container_uuid` works. The client therefore
  resolves ids through Viafoura's public Live Comments API instead:
  `GET https://livecomments.viafoura.co/v4/livecomments/{section_uuid}?container_id=<id>&limit=0`
  (no auth; 404 for unknown ids) returns `content_container_uuid`. The Telegraph page id
  (e.g. `A65xRHy7KY6g`) is the Viafoura `container_id`, and **CAPI returns it** as
  `metadata.page-id` — `processing.fetch` reads it there and puts it on `Article.page_id`.
  The Telegraph section_uuid is `00000000-0000-4000-8000-010fdf3f0a45`.
- `get_comments` returns `{"more_available", "contents": [...]}` as a flat list, replies
  inline after their parent (top-level when `parent_uuid == content_container_uuid`).
  `limit` (max 100) counts top-level only; paginate with `starting_from` = last top-level
  uuid; pinned comments float first in every sort; there is no server-side time filter
  (the client pages newest-first and stops). Payload has `actor_uuid` only, no usernames.
- Default lookback of the trending/site tools is 48 h; article-level tools return the
  full current thread.

## TypeSafe / Jev conventions

- One request per comment: `state = {"article": {...}, "comment": {...}}`, many atomic
  questions per request (Noul for yes/no, Score for ordered levels, Choice for
  categories). Reference state fields in instructions with backticked paths.
- Jev cannot count, do arithmetic or generate text; keep those in code. Pin the model
  version (`jev-1.13.0`) rather than `jev-latest` once thresholds are tuned.
- Combine answers in code with explicit weights; exclude on high-probability negatives
  (sarcasm, guideline breaches, off-site URLs); route the uncertain band to editors.

## Tests

`tests/`, run with `uv run pytest`. Around 115 tests, under four seconds, and **no test
ever calls Jev** — every interaction goes through `FakeJev` in `tests/conftest.py`. A test
that would reach the API is a defect, not a slow test; `tests/test_guards.py` enforces
that, along with the project's other standing decisions (no scraping, no `os.environ`
outside `settings.py`, no blocking calls in async functions, no unbounded fan-out).

Run the suite after every change. The `pipeline-qa` agent runs it and reviews the diff
for correctness, async hygiene, security, performance and test quality; use it before
calling any change done.

Live services are still exercised by hand for the fetch stage; those checks are marked
`@pytest.mark.live` and deselected by default.

## Working style

- Analysis and design in chat first, code when asked. Keep changes scoped to the request.
- Run `uv run pytest` and both ruff commands before finishing. Scratch scripts go in the
  session scratchpad, not the repo.
- Report honestly: if a live call fails or a check was skipped, say so.
