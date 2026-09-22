# Progress

_Last updated: 22 September 2026_

Spec: `.claude/comment-classification-spec.md` (one document, Parts I–IV).
Parts I and II are built. **Part III is the plan below.**

---

## Implementation plan — Part III, the HTTP API

Tick a box only when the step is **finished and validated**: `uv run pytest -q` green,
`uv run ruff check . && uv run ruff format --check .` clean, and the `pipeline-qa` agent
has reviewed the diff. Each step must leave the suite green on its own.

### Step 1 — `config.toml` and settings
- [ ] `config.toml` at the project root with the sections in spec §16
- [ ] `load_settings()` reads `.env` **and** the TOML; no key appears in both
- [ ] `HOST` / `PORT` env override documented and implemented (the one exception)
- [ ] Startup validation: every question id under `[classification.*]` checked against
      `BATTERY`, failing loudly on a typo
- [ ] Module constants in `workflow.py` / `classification.py` now read from settings
- [ ] Tests: missing file, malformed TOML, unknown question id, env override precedence

### Step 2 — Viafoura client rewrite
- [ ] `vf_mcp.py` → `viafoura.py`; `ViafouraMCPClient` → `ViafouraClient`
- [ ] Public REST endpoints only (spec §15.1); shares the CAPI `aiohttp` session
- [ ] Retries with backoff + jitter on 429/5xx, honouring `Retry-After`
- [ ] Paginate **only** on the nested path — trap 1, spec §15.3
- [ ] Keep the `seen` set and the "cursor did not advance" guard — trap 2
- [ ] Early-stop paging against the store, plus `full_sweep_after_hours`
- [ ] Delete the MCP layer: OAuth, PKCE, dynamic registration, headless login, token cache
- [ ] Remove `mcp` from dependencies; drop `VF_MCP_API_KEY`, `VF_MCP_URL`,
      `VF_MCP_TOKEN_STORE` from `.env` and from the `CLAUDE.md` table
- [ ] Tests: cursor advance, early-stop, page cap 100, the two traps as regression tests
- [ ] Live check: one real article end to end, comment count matches
      `total_visible_content`

### Step 3 — richer Jev answers
- [ ] `_read_answers` captures `probabilities` and `legend` on the four Score questions
- [ ] Confirm the fingerprint is unchanged, so nothing is re-billed
- [ ] Absent fields omitted, never emitted empty (old rows stay thinner)
- [ ] Fix `tests/conftest.py`: fake Noul answers carry a `.confidence` the real type lacks

### Step 4 — extract `classify_article()`
- [ ] Orchestration out of `workflow.run()`, callable by both the CLI and the API
- [ ] Markdown renderer stays a CLI concern
- [ ] CLI output byte-identical before and after — this step changes no behaviour

### Step 5 — `POST /v1/classifications`
- [ ] `src/api/` package; added to `[tool.hatch.build.targets.wheel] packages`; `uv sync`
- [ ] Lifespan builds settings, the shared session, `ViafouraClient` and **one** `JevClient`
- [ ] Request/response schemas per spec §17, `answers` restructured at the boundary
- [ ] Telegraph URL allowlist; CORS from config; bind `127.0.0.1` by default
- [ ] Bounded lock wait → **409** rather than a hung connection
- [ ] Results sorted by score descending; `max_comments` + `from`; `full_refresh`
- [ ] Tests: allowlist rejection, param clamping, ordering, already-pinned handling,
      409 contention, and that no route reaches Jev (via `dependency_overrides`)
- [ ] Verify `TestClient` opens no sockets, so the `_no_network` fixture still holds

### Step 6 — `POST /v1/feedback`
- [ ] `src/processing/feedback.py`, module functions mirroring `store.py`
- [ ] One file per review event, atomic write, **not** JSONL (spec §19)
- [ ] Payload covers every comment shown, not only the ticks (spec §18.1)
- [ ] Record snapshots score, answers, model, both fingerprints and comment text
- [ ] `scoring_fingerprint` over the resolved `[classification]` config
- [ ] Tests: round trip, revision ordering, unticked-is-data, path traversal on `review_id`

### Step 7 — documentation
- [ ] `CLAUDE.md`: layout, the Viafoura section, the env table, `config.toml`
- [ ] `decision-log.md`: MCP dropped, no auth, no reviewer, JSON over DB for now
- [ ] `lessons-learnt.md`: the two silent-failure traps, the 100 cap, the paging numbers
- [ ] This file: move Part III into "Built"

---

## Built and verified against live services

| Component | File | Evidence |
| --- | --- | --- |
| Viafoura client | `src/clients/vf_mcp.py` | Fetched 409 comments across 2 pages in ~2s; **to be replaced in Step 2** |
| CAPI article client | `src/clients/capi.py` | Live call returned headline, standfirst, 1,182 words of body |
| Settings | `src/processing/settings.py` | Loads once, caches, redacts secrets, names all missing vars at once |
| Question battery | `src/processing/questions.py` | 14 questions: 9 Noul, 4 Score, 1 Choice |
| Fetch stage | `src/processing/fetch.py` | Article then comments; `page-id` from CAPI |
| Classification stage | `src/processing/classification.py` | Code signals, Jev behind the client, thresholds, weighted score |
| Jev client | `src/clients/jev.py` | 1,454 requests in 21.2s against a predicted 20.8s |
| Incremental store | `src/processing/store.py` | Run 1: 1,454 calls / $0.2120. Run 2: 0 calls / $0.0000 |
| Orchestrator and report | `src/processing/workflow.py` | Reports in `output/`, rewritten in place |
| Test suite | `tests/` | **148 tests, ~3s, no Jev calls** |
| QA agent | `.claude/agents/pipeline-qa.md` | Found 3 criticals in round 1, 1 in round 2 |

Ruff passes across the project.

## The result that matters

On the capital gains article the comment the Community team had actually pinned ranked
**first at 0.946**, against 0.454 for second: personal experience 2.97/3, relevance 0.96,
readability 2.00/2, sarcasm 0.03. One article, so it is an encouraging anecdote rather
than evidence — which is what Part III's feedback loop is for.

## Known problems

1. **`profanity_or_threat` over-fires on insults**, not profanity. Excluded a
   personal-experience comment containing no swearing.
2. **`unverified_claim` fires often.** 4 of 17 exclusions in one run.
3. **Tone cannot sink a comment.** An angry comment scoring 0.00 on tone still reached rank
   four, because tone is 15% of the weight. May want a floor instead.
4. **No ground truth yet.** The reports say what Jev thinks, not whether it is right.

## Not started

- **Calibration with the Community team.** The agreed method: a stratified, blind, shuffled
  sample in front of two or three managers, measuring inter-rater agreement to establish
  the ceiling. Part III Step 6 is the mechanism.
- **The pinned-comment harvester.** Now viable on better terms than thought — the trending
  endpoint reaches 30 days of articles, not 48 hours (spec §15.2), and per-container counts
  give a pinned-count probe without downloading comments.
- Audience segments.
- Usernames (needs a source other than Viafoura).
- Viafoura's moderation word list as a hard filter.
- Batching comments per Jev request. Investigated 2026-09-21 and **declined**: ~25% token
  saving, not the docs' 12x, because the battery is 2.5x the article and repeats per comment
  inside a batch. Against that, Jev's jaggedness page warns accuracy falls as the state
  fills with irrelevant detail — which the other comments in a batch are. Revisit above
  ~10,000 comments per article.
- Auto-pinning. Needs `mod` credentials from Viafoura and is out of scope in the brief
  (spec §15.6).
