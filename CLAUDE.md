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

## Layout

- `src/processing/`: the classification pipeline. `workflow.py` orchestrates and renders
  the Markdown report, `fetch.py` gets article + comments, `questions.py` holds the Jev
  battery (the tuning surface), `classification.py` holds signals, thresholds and weights.
  Run: `PYTHONPATH=src uv run python src/processing/workflow.py [article]`. The
  `PYTHONPATH` is required: a script inside `processing/` otherwise cannot import
  `clients`. Spec: `.claude/comment-classification-spec.md`.
- `src/main.py`: manual test harness for the Viafoura client (`uv run python src/main.py
  [container_id]`); constants at the top control what it fetches.
- `src/clients/vf_mcp.py`: Viafoura Comments MCP client (see below). Python API only,
  no CLI; async core plus sync one-shot helpers.
- `experiment_1.py` (root): TypeSafe experiment (Jev Choice question over persona data).
- `output/`: JSON dumps written by the harness; disposable.
- Add new modules under `src/`; import as `from clients.vf_mcp import …` with `src` on
  `sys.path` (or `uv run --directory src`).

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
  (e.g. `A65xRHy7KY6g`) is the Viafoura `container_id`; every article exposes it in a
  `<meta property="vf:container_id">` tag, which `container_id_from_url` reads. The
  Telegraph section_uuid is `00000000-0000-4000-8000-010fdf3f0a45`.
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

## Working style

- Analysis and design in chat first, code when asked. Keep changes scoped to the request.
- Test against the live services with small limits (e.g. `max_pages=2`); there are no
  unit tests yet. Scratch scripts go in the session scratchpad, not the repo.
- Report honestly: if a live call fails or a check was skipped, say so.
