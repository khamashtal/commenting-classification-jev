# Progress

_Last updated: 22 September 2026_

Spec: `.claude/comment-classification-spec.md` (one document, Parts I–IV).
Parts I and II are built. **Part III is the plan below.**

---

## Implementation plan — Part III, the HTTP API

Tick a box only when the step is **finished and validated**: `uv run pytest -q` green,
`uv run ruff check . && uv run ruff format --check .` clean, and the `pipeline-qa` agent
has reviewed the diff. Each step must leave the suite green on its own.

### Step 1 — `config.toml` and settings — **done 2026-09-22**
- [x] `config.toml` at the project root with the sections in spec §16
- [x] `load_settings()` reads `.env` **and** the TOML; no key appears in both
- [x] `HOST` / `PORT` env override documented and implemented (the one exception)
- [x] Startup validation: every question id under `[classification.*]` checked against
      `BATTERY`, failing loudly on a typo
- [x] Module constants in `workflow.py` / `classification.py` now read from settings
- [x] Tests: missing file, malformed TOML, unknown question id, env override precedence

What the step actually settled, beyond the list above:

- **`src/processing/config.py` is a new module** holding the frozen dataclasses and the
  TOML reader. It imports nothing from the project, which is what lets `settings.py` and
  `classification.py` both use it without importing each other; `BATTERY` is passed in as
  `known_question_ids` rather than imported there.
- **No policy default survives in `src/`.** `MIN_WORDS`, `MAX_CAPS_RATIO`, `SWEET_SPOT`,
  `EXCLUDE_AT`, `FLAG_AT`, `ON_TOPIC_*` and `WEIGHTS` are gone from `classification.py`;
  the policy arrives as a `ClassificationConfig` argument. A missing `config.toml` is a
  hard error, not a fall-back — a default that quietly replaces what somebody meant to
  write is the same failure as the code ignoring them. `TELEGRAPH_DOMAINS` stays in code
  on purpose: it is the boundary around content we can vouch for, not a number to tune.
- **The weight guard is stricter than §16 asked.** `[classification.weights]` must name
  exactly the six keys `quality_score` combines (`config.WEIGHT_KEYS`), because three of
  them — `experience`, `contribution`, `representativeness` — are composites rather than
  question ids, so the battery check cannot cover them. A missing weight would score every
  comment low in silence.
- **`max_words = 600` is unchanged**, so `battery_fingerprint()` is unchanged and no
  existing store was invalidated. Nothing was re-billed by this step.
- **Blank counts as unset.** `_first_env` treated `KEY=""` or `KEY="  "` as a value; a
  whitespace-only `jev_api_key` would have passed the required-variable check and gone to
  the API as a key made of spaces.
- **`api_key` renamed to `jev_api_key`** in `.env` by Luis mid-step; the alias tuple and
  the `CLAUDE.md` table follow it. `VF_SECTION_UUID` moved out of `.env` into
  `[viafoura] section_uuid` under the no-key-in-both rule — it was dead in the code, and
  Step 2 is the first thing that will read it.
- **Tests build their own policy** (`conftest.make_classification_config` / `make_settings`)
  rather than reading the repo's `config.toml`. Retuning a threshold must not break an
  unrelated test. `tests/test_settings.py` is the only place the shipped file's values are
  load-bearing, and it checks the things parsing cannot: weights summing to 1.0, every
  flag threshold below its exclusion, the committed host still loopback.
- `feedback/` added to `.gitignore` ahead of Step 6: it will hold reader comment text.

Validated: **196 tests, 3.2s**, ruff check and format clean, `pipeline-qa` reviewed the
diff and returned PASS with no defects. Its one substantive note — the two `on_topic`
thresholds were the only numbers read without a minimum bound — is fixed.

### Step 2 — Viafoura client rewrite — **done 2026-09-22**
- [x] `vf_mcp.py` → `viafoura.py`; `ViafouraMCPClient` → `ViafouraClient`
- [x] Public REST endpoints only (spec §15.1); shares the CAPI `aiohttp` session
- [x] Retries with backoff + jitter on 429/5xx, honouring `Retry-After`
- [x] Paginate **only** on the nested path — trap 1, spec §15.3
- [x] Keep the `seen` set and the "cursor did not advance" guard — trap 2
- [ ] ~~Early-stop paging against the store, plus `full_sweep_after_hours`~~ —
      **not done, deliberately. See "Why early-stop paging was dropped" below.**
- [x] Delete the MCP layer: OAuth, PKCE, dynamic registration, headless login, token cache
- [x] Remove `mcp` from dependencies; drop `VF_MCP_API_KEY`, `VF_MCP_URL`,
      `VF_MCP_TOKEN_STORE` from `.env` and from the `CLAUDE.md` table
- [x] Tests: cursor advance, page cap 100, the two traps as regression tests
- [x] Live check: one real article end to end — **the count does *not* match
      `total_visible_content`**, see below

#### Why early-stop paging was dropped

The plan said: page newest-first and stop at the first page whose uuids are all already
in the store. 14.6s cold, ~0.7s warm, 20x. It does not work, for a reason the measurement
hid.

Stopping early means the older comments are never fetched, so the run has no `Comment`
object for them — only their Jev answers, which is all the store holds. To rank the
thread it would have to rebuild those comments from the store, which means the store must
start holding the comment payload. And the moment it does, **the fields that change are
frozen at whatever they were when the comment was first seen** — `is_pinned`, `is_picked`,
`total_likes`, `total_replies`. `is_pinned` is the one that matters: it is the closest
thing to ground truth this project has, it is what the "Already pinned or picked" section
of the report is for, and it is the entire basis of the calibration loop. Caching it would
quietly break the thing being built.

Secondary but real: `stance_shares` — 15% of the score — is computed over the thread, so a
warm run that saw only the newest 100 comments would score every comment against a
different denominator than the cold run did. The same article would rank differently on
run 2 for no reason a reader could see.

What the optimisation was actually worth: **nothing that matters.** Fetching is free and
takes 15s; Jev costs money and the store already eliminates that completely. Trading
correctness of the pin signal for 14 seconds of wall clock is a bad trade.

Where early stopping *is* right is the harvester — many articles, asking which have new
activity — and `container_details` now gives a per-article count for exactly that, with no
thread download. That belongs with the harvester, not here. `full_sweep_after_hours` was
removed from `config.toml` rather than left as a setting nothing reads.

#### What the live check actually found

- `trending` returns its list under the key **`trending`**, not `contents` or
  `containers`. The first implementation guessed `containers`, and the endpoint returned
  HTTP 200 with an empty list and no error — the fake session in the tests had faithfully
  encoded the same wrong guess, so the suite was green and wrong. Now it raises if the key
  is absent, and `tests/test_viafoura.py` has that as a named regression test. **This is
  the argument for the live check being a checklist item rather than an optional extra.**
- **`total_visible_content` is approximate.** Across three live threads it ran 0.6–0.9%
  above a complete walk (4715 vs 4689 visible, 3628 vs 3603, 3622 vs 3591), in every case
  after the walk ended on the server's own `more_available=false`. Only one parent in
  ~3,200 declared more than 50 replies, so reply-limit truncation does not explain it. Use
  the counter to decide whether an article is worth downloading; never as proof that a
  walk was complete.
- Replies nest more than one level deep: 399 replies on one thread had a parent that was
  itself a reply. Pre-existing behaviour, unchanged here, but worth knowing.
- Pinned comments really do float above every sort: the top ten by likes came back 631,
  1079, 768, …, the 631 being the pinned one. A small `limit` can therefore never miss an
  editor's existing pick.
- `fetch_thread` end to end: URL → CAPI → 4,707 comments in 15.2s; page id → top 10 in
  0.5s.

#### Other decisions

- **The client is not a context manager.** It shares the CAPI `aiohttp.ClientSession` and
  owns no resource of its own, so `async with` would imply cleanup that does not exist.
- **`RankedBy` is gone.** It existed to map friendly names onto the MCP tool's vocabulary;
  `SortOrder` is the API's own and the mapping layer went with the server. `trending` as a
  comment sort had no REST equivalent and was dropped rather than guessed at.
- `[viafoura]` gained `max_concurrent_requests`, `max_retries`, `backoff_initial_seconds`
  and `backoff_max_seconds`. No rate limit is documented for this API anywhere, which is
  exactly why these are settings and not constants.
- **The Viafoura API key is no longer a secret this project handles at all.**
  `viafoura_api_key` is gone from `Settings`; 16 packages left the lockfile with `mcp`.

#### What QA found, and what was fixed

`pipeline-qa` returned PASS with five defects. Three were real and are fixed:

1. **Transport failures were never retried.** The retry loop caught `ViafouraHTTPError`
   only, so a reset connection, a DNS blip or a read timeout — none of which ever reach a
   status — escaped and aborted the whole walk. Paging a 4,000-comment thread holds a
   connection for fifteen seconds, which is precisely the window where one lands. Now
   caught alongside the status case; `FakeSession` can raise on entry so the tests can
   reproduce it, including a blip between page 1 and page 2.
2. **`[viafoura] page_size` and `reply_limit` were parsed, validated and documented but
   never read** — the client used the module constants directly. Exactly the "setting
   nothing reads" failure this step removed `full_sweep_after_hours` to avoid, committed
   two keys further down the same table. Wired through, still clamped to the server
   maxima, with tests that fail if it regresses.
3. **`src/main.py` resolved in the wrong order**, calling `container_details` before
   `resolve_container_uuid`. `container_details` takes a page id, so the documented
   `uv run python src/main.py <container UUID>` usage 404'd before it reached the
   resolver that would have passed the uuid straight through. Fixed and verified live.

Two were noted and not acted on:

4. `ViafouraClient` is built inside `run()` rather than beside `JevClient`. True, and it
   would matter under FastAPI, where a per-request construction would reset the uuid
   cache each time. Step 4 extracts `classify_article()` and Step 5 builds the clients in
   a lifespan handler, so the shape changes there anyway; fixing it now would be churn
   against a signature about to change.
5. `get_comments_in_range` with `until` and no `since` walks the whole thread. That is
   correct rather than a bug — everything older than `until` is inside the window, so
   there is nothing to stop at — but it is now said plainly in the docstring, because the
   cost on a liveblog thread is the full walk.

Validated: **243 tests, 3.2s**, ruff check and format clean, `pipeline-qa` PASS. QA
independently agreed with dropping early-stop paging, and named the `stance_shares`
argument a second and distinct reason.

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
| Viafoura client | `src/clients/viafoura.py` | Public REST, no auth. 4,707 comments over 33 pages in 15.2s, live 2026-09-22 |
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
