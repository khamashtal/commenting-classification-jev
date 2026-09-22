# Spec: comment classification with Jev

The whole project in one document. Three parts, in the order they were built:

| Part | What | Status |
| --- | --- | --- |
| **I** | The classification pipeline — fetch, ask Jev, score, report | **Built** |
| **II** | Incremental classification — pay once per comment, ever | **Built 2026-09-22** |
| **III** | The HTTP API — FastAPI service plus editor feedback capture | **Designed, not built** |
| **IV** | Cost, risks, open questions | — |

The implementation plan and its checkboxes live in `.claude/memory-bank/progress.md`.
Question wording, weights and thresholds are what still need review (§23).

---

# Part I — the classification pipeline

## 1. Purpose

Take one Telegraph article, pull its comments, ask Jev a battery of typed questions about
each one, combine the answers in code, and produce a ranked list of comments worth pinning
or putting in a carousel.

Part I was built as a **measurement instrument**: a Markdown report that puts Jev's
judgments next to the comment text so we can tune wording and weights. Part III turns the
same pipeline into a service a UI can call, and adds the feedback loop that finally gives
us ground truth.

## 2. Scope

**In scope**

- Load settings once at startup and hold them in memory (§3).
- Fetch the article (headline, standfirst, body) from CAPI.
- Fetch comments from Viafoura and keep them **in memory** — no intermediate JSON file.
- Classify each comment with one Jev request carrying the whole question battery.
- Compute deterministic signals (word count, caps, URLs) in code.
- Apply exclusion rules and a weighted quality score, both defined in code.
- Remember answers between runs so a comment is paid for once (Part II).
- Serve the result over HTTP and record editor verdicts (Part III).

**Out of scope**

| Deferred | Why |
| --- | --- |
| Audience segments (Advocate / Traditionalist / Curator) | Needs specialist segment definitions first. |
| Viafoura's moderation word list | Not obtained yet. |
| Commenter usernames | Viafoura returns `actor_uuid` only. The brief needs them; sourcing them is a separate problem. |
| Auto-pinning, carousel generation, Viafoura writes | Out of scope in the brief, and needs `mod` credentials — see §15.6. |
| Batching several comments per Jev request | Investigated 2026-09-21 and **declined**: ~25% token saving against a documented accuracy risk. Revisit above ~10,000 comments per article. |

## 3. Architecture: build once, pass down

The design target was always a service, so **settings and clients are created once at
process start** and passed as arguments. Nothing re-reads configuration per request and
nothing constructs a client inside a function.

```python
@dataclass(frozen=True, slots=True)
class Settings: ...


# Once, at startup. Fails fast, naming every missing variable at once.
def load_settings() -> Settings: ...


# Everywhere else. No I/O.
def get_settings() -> Settings: ...
```

- **CLI:** `workflow.py` calls `load_settings()` at the top of `main()`.
- **FastAPI:** the `lifespan` handler calls it; routes get it via `Depends`.

Functions take `Settings` as an argument rather than calling `get_settings()` themselves,
which keeps them testable and request-agnostic. `capi.py` already worked this way, and
that is why it needed no changes for the API phase.

The same rule governs long-lived clients:

| Client | Why it must be shared |
| --- | --- |
| `aiohttp.ClientSession` | Connection pooling; one per request is a known anti-pattern. Shared by CAPI **and** Viafoura (§15.5). |
| `ViafouraClient` | Holds the session and its retry policy. |
| `JevClient` | **The rate limiter is per-account.** Two clients would each believe they had the full 1,200 req/min budget and start collecting 429s. |

Settings are a frozen dataclass, never logged or printed, so the `CLAUDE.md` rule against
reading `.env` holds. Configuration now comes from two sources — see §16.

## 4. Inputs

### 4.1 Article, via CAPI

`clients.capi.get_ucms([url], session, settings)` returns `{url: ucm}`.

| Field | Path in the UCM |
| --- | --- |
| Headline | `content.headline` |
| Standfirst | `content.standfirst` |
| Body | `content.body[]`, keeping blocks whose `type` is `text` or `heading`, joining their `data` |
| Page id | `metadata.page-id` — **this is the Viafoura `container_id`** |

The `data` values are plain text already; `html-data` is the marked-up twin and we ignore
it. Other block types (`image`, `call-to-action`) are dropped.

`metadata.page-id` is the reason nothing in this project ever fetches an article page. An
anonymous page fetch returns **HTTP 402** on most premium articles, so scraping was never
a working route — only one that happened to survive on free articles. The scraping code is
deleted, not disabled.

Body text is **capped** before it enters `state` (default 600 words). Jev's accuracy falls
as the state fills with material the question does not need, and the article is re-sent
with every comment, so it dominates cost.

**URL matching.** CAPI matches on the exact URL, so the input is normalised first: strip
any `#vf-…` fragment and query string.

### 4.2 Comments, via Viafoura

The public Live Comments REST API — no MCP server, no OAuth, no API key. See §15 for the
endpoints, the measured limits and the two silent-failure traps.

Per comment we carry `content_uuid`, text, `created_at`, `is_reply`, `parent_uuid`, likes,
`total_replies`, `is_pinned`, `is_picked`, `state`.

Replies arrive nested under their parent in the same page, so code builds a
`uuid -> text` map and attaches `parent_text` to each reply. Jev needs it to judge whether
a reply stands on its own.

## 5. Pipeline

1. **`workflow.py`** calls `load_settings()`, opens the shared clients, normalises input.
2. **`fetch.py`** gets the article from CAPI, then the comments from Viafoura. Sequential,
   because CAPI hands back the `page-id` that Viafoura needs.
3. `fetch.py` builds the `uuid -> text` map and attaches `parent_text` to replies.
4. **`classification.py`** runs deterministic signals over every comment (no API calls).
5. It **skips Jev** for comments failing a hard code-side rule (§8.1) — no point paying to
   classify a 4-word comment. They are recorded as excluded, with the reason.
6. It **skips Jev for comments already in the store** (Part II).
7. For every survivor it sends one Jev request, concurrently behind the client's limiter,
   and returns `Classification` records: raw Jev answers plus code-side signals.
8. It aggregates thread-level stance shares, then scores and ranks **every** comment,
   restored ones included.
9. **`workflow.py`** renders Markdown; **the API** serialises JSON (Part III).

No step writes an intermediate file. Steps 2 and 4–8 take the clients as arguments and
never construct them, so the same functions serve a request handler unchanged.

## 6. The Jev question battery

14 questions, one request per comment, all evaluated in parallel by the model. Question
ids are for our code only and are never sent, so each `instructions` is fully worded.

Every Noul is phrased so **high means the property is present**. Every Score runs
**worst to best**, so higher is always better. That consistency keeps the scoring code
free of special cases.

```python
state = {
    "article": {"headline": ..., "standfirst": ..., "body": ...},
    "comment": {"text": ..., "parent_text": ... or None},
}
```

### 6.1 Quality signals (what we want)

| id | type | what it asks |
| --- | --- | --- |
| `personal_experience` | Score, 4 levels | How much of the comment is a first-hand account. Levels separate "uses I/my but is pure opinion" from "describes a lived event with concrete detail". |
| `experience_relevant` | Noul | Does that experience bear on the article's subject? Multiplies `personal_experience`, so an irrelevant story does not win. |
| `proposes_solution` | Noul | Does it seriously propose a specific course of action? Criteria exclude sarcastic proposals. |
| `reasoned_argument` | Noul | Does it give reasons or evidence rather than only asserting? |
| `tone` | Score, 3 levels | Angry/ranting → emphatic but civil → calm and reflective. |
| `readability` | Score, 3 levels | Hard to read → readable with effort → clean sentences and punctuation. |
| `standalone` | Noul | Can a reader understand it without the parent comment? Matters most for replies. |
| `stance` | Choice, 4 options | `supportive` / `critical` / `mixed` / `no_position`. Feeds representativeness, not quality. |

### 6.2 Exclusion signals (what we sift out)

| id | type | what it asks |
| --- | --- | --- |
| `sarcasm` | Noul | Does it say the opposite of what it means? Criteria carry the brief's own examples: fake praise of the Telegraph, Guardian comparisons, "cultural enrichment", thanking moderators. |
| `on_topic` | Noul | Does it address the article, rather than the Telegraph, the comment section or other commenters? (Low value excludes.) |
| `unverified_claim` | Score, 3 levels | 0 no outside claims, 1 general knowledge, 2 specific checkable statistics, dates or quotations not in the article. Only level 2 excludes. Changed from a Noul after review: the pinned SCTV comment in the sample spreadsheet lists cast members and a binary question would have wrongly excluded it. |
| `personal_attack` | Noul | Insults or mocks a specific person or other commenters. |
| `group_hostility` | Noul | Contempt towards people by nationality, ethnicity, religion, sex, sexuality, disability or age. |
| `profanity_or_threat` | Noul | Swearing, slurs, threats. |

### 6.3 Full definitions

The exact `instructions` and `criteria` live in `questions.py` as the single place to edit
during tuning, including structured criteria with `what`, `not_for` and `examples` fields
for `sarcasm`, `proposes_solution` and `unverified_claim`.

**This is the part most worth review.** The wording of the criteria, not the code,
determines whether the classifier agrees with the Community team. Use the
`jev-question-tuning` skill to change one.

## 7. Why these and not others

Three brief criteria are **not** Jev questions, because code does them exactly and Jev
does them badly — its own documentation says it cannot count or do arithmetic reliably:

- Word count and the 20–100 word sweet spot.
- ALL CAPS ratio.
- URLs, and whether they are on telegraph.co.uk.

Representativeness is also not a question. It cannot be judged from one comment; it is the
thread-wide share of that comment's stance, computed in code once all comments are
classified (§8.3).

## 8. Decision logic (all in code)

### 8.1 Hard exclusions, before Jev

Cheap, deterministic, and they save API calls:

- Fewer than 15 words (the brief's "lack of substance" threshold).
- Contains a URL outside telegraph.co.uk.
- ALL CAPS ratio above a threshold (default 0.5 of alphabetic characters).
- Comment state is not `visible`.

### 8.2 Jev-based exclusions, after classification

A comment is **excluded** when any signal crosses its action threshold, and **flagged for
review** in the middle band. Thresholds live in one place, so changing policy is a number
change rather than a reworded question — and from Part III they live in `config.toml`.

| signal | excludes at | flags at |
| --- | --- | --- |
| `sarcasm` | ≥ 0.70 | 0.40–0.70 |
| `personal_attack` | ≥ 0.70 | 0.40–0.70 |
| `group_hostility` | ≥ 0.60 | 0.35–0.60 |
| `profanity_or_threat` | ≥ 0.60 | 0.35–0.60 |
| `unverified_claim` (score) | ≥ 1.5 | 1.0–1.5 |
| `on_topic` | < 0.35 | 0.35–0.60 |

Flagged comments still appear, marked, rather than disappearing. Editors should see the
borderline cases; that is how we learn where the thresholds belong.

### 8.3 Quality score

Each Score is normalised to 0–1 by dividing by its top level number. Starting weights:

| component | weight |
| --- | --- |
| `personal_experience` × `experience_relevant` | 0.35 |
| `tone` | 0.15 |
| `readability` | 0.10 |
| max(`proposes_solution`, `reasoned_argument`) | 0.15 |
| `standalone` | 0.10 |
| representativeness | 0.15 |

Representativeness is the thread-wide share of the comment's winning stance. A comment in
the majority camp scores high; a dissenting comment scores low but is not excluded, which
matches the brief's rule that dissent can be pinned alongside common views.

### 8.4 Pin eligibility and prior actions

Replies cannot be pinned, only carouselled. Every comment is labelled pin-eligible or
carousel-only from `is_reply`. No separate scoring path.

A comment already pinned or picked by an editor is **classified but kept out of the
proposals** — proposing it again wastes their time. It is still scored and still shown,
because a comment an editor chose by hand is the closest thing to ground truth this
pipeline has, and its score is the cheapest available check on whether the questions agree
with the people they imitate.

`is_top_comment` is deliberately not treated as a prior action: that is Viafoura's own
algorithmic pick — the tool the brief reports as having promoted sarcasm — not a human
decision.

### 8.5 What gets proposed

Not a fixed shortlist size. Comments clearing `min_score` are proposed, capped at
`max_results`; if fewer than `min_results` clear the bar, the best `min_results` are shown
anyway so the output is never empty. Defaults 0.50 / 25 / 5.

Evidence for a threshold over a fixed N: on the capital gains article the comment the
Community team had actually pinned scored **0.946** against **0.454** for the next. A
fixed count would have padded the list with comments nothing distinguishes.

## 9. Output: the Markdown report

One file per article, `output/classification_<container_uuid>.md`, **rewritten in place**
rather than a new timestamped file per run. The run counter is in the header.

1. **Header** — headline, URL, container uuid, run count, model, counts (fetched / skipped
   pre-Jev / classified / new / reused / excluded / flagged), token usage, estimated cost.
2. **Thread summary** — stance distribution; it drives representativeness, so it must be visible.
3. **Proposed** — best first, each with rank, score, pin-eligible or carousel-only, likes,
   reply count, the **comment uuid** (editors paste it into the Viafoura UI), the full
   text, and **all 14 Jev answers** so the score can be checked rather than trusted.
4. **Already actioned** — comments the editor pinned or picked, with our score beside them.
5. **Flagged for review** — the middle band, with which signal fired and its value.
6. **Excluded** — grouped by reason. The point is spotting false exclusions.

Numbers go in tables or on their own lines, never buried in prose, so a reader can scan for
disagreements with their own judgment.

The Markdown renderer lives inside `workflow.py` and is a CLI concern. Part III extracts
the orchestration it wraps into `classify_article()` so the API can share it.

---

# Part II — incremental classification

Built 2026-09-22 in `processing/store.py`, wired into `classify_thread` and `workflow.run`.

## 10. Why: no comment ever needs reclassifying

The pipeline was stateless. Run it twice on the same article and you pay twice for answers
that provably cannot have changed, because

```
quality_score = f(Jev answers, stance_shares)
```

and the Jev answers depend only on the article body, the comment text and the parent text
— all three immutable once posted. The only time-varying term is `representativeness`,
which reads `stance_shares` over the thread, and that is arithmetic over answers already
held.

**That single fact is what keeps this small.** It also resolves the question that prompted
the work: comments gaining likes or replies after their first classification do not need
re-scoring, because engagement is not an input to the score (§13).

## 11. What changes

```
run(article) ->
    1. fetch the current thread (free)
    2. drop any uuid already in the store          <- prevents double-paying
    3. Jev the remainder, write answers to the store as each arrives
    4. re-apply thresholds to every comment, restored ones included
    5. recompute stance_shares and quality_score for ALL comments, in code, no API calls
    6. rank, render into the article's one report file
```

Step 5 keeps every comment's score current as the thread's stance mix moves, without a
single extra request. Steps 1–3 are the only ones that cost money, and they touch only
comments never seen before.

Answers are written to the store **the moment each one arrives**, not batched after the
gather: an answer is paid for when it arrives, so it must be recorded when it arrives.
Batching meant a Ctrl-C partway through discarded everything bought so far.

## 12. The store

### 12.1 The uuid set is the cursor, not a timestamp

The original design had a timestamp watermark. Building it showed the watermark was
unnecessary and harmful.

Comments do not arrive in creation order: `Comment.state` exists because Viafoura holds
comments in moderation, so one created at 10:00 can be approved at 10:45, after a 10:30
watermark has moved past it. A timestamp cursor drops that comment permanently and
silently.

Since Viafoura is free and Jev is what bills, there is nothing to save by fetching less.
Fetching the current thread every run and skipping known uuids needs **no cursor at all**,
removes that bug class entirely, and means a run always reports on the whole thread rather
than a slice. `last_run_at` is recorded for the report's run counter and is never read as
a cursor.

(Part III adds an *early-stop* optimisation on top — see §15.4. That bounds how many
pages are fetched; it does not reintroduce a correctness cursor.)

### 12.2 Shape of a record

| Field | Why |
| --- | --- |
| `container_uuid` | Which article's thread. |
| `comment_uuid` | The key. Also the handle an editor pastes into the Viafoura UI. |
| `answers` | The raw Jev answer dict. |
| `model` | The version that answered *this* comment, not the run's cumulative set. |
| `classified_at` | When this row was written, UTC, to the second. |

Plus per container: `article_url` and `article_headline` (a uuid in a filename identifies
nothing to a person opening `state/`), `fingerprint`, `first_run_at`, `last_run_at`, `runs`.

Answers are stored **raw rather than as a score**, so re-weighting is a replay over stored
rows and costs nothing. That is the point: **thresholds and weights are tuned against
history without re-billing.** It is also why `model` is on every row — with the model
unpinned, a row answered by a later Jev version is only comparable if you know which
version answered it.

Timestamps are UTC with an explicit offset and no microseconds. Local time is meaningless
on another machine; six decimal places hid the `+00:00` that explains an apparent hour gap
under BST.

### 12.3 For the POC: one JSON file per container

`state/<container_uuid>.json`. A file per container rather than one big file, so two
articles never contend and a corrupt file costs one thread rather than all of them.

Written with `mkstemp` + `fsync` + `os.replace` + directory fsync, which is atomic on
POSIX: a crash mid-write leaves the previous run's state intact rather than a truncated
file. A corrupt or unknown-schema file degrades to an empty store rather than raising —
losing the cache costs money, losing the run costs the user, so prefer the money.

The whole map is read into memory (~0.7 KB per comment, so a 1,000-comment thread is under
a megabyte) and rewritten in full every run.

**Two gaps were closed during the build, both found by the `pipeline-qa` agent:**

- **Concurrent runs are locked.** `os.replace` makes the write atomic and does nothing for
  the read-modify-write around it: two runs on one article both loaded the same snapshot,
  both paid Jev for the same comments, and the second save erased the first's answers.
  `store.locked()` takes an `O_EXCL` lock per container and refuses rather than queues.
- **The cache key is the uuid *and* a fingerprint** of the battery plus
  `ARTICLE_MAX_WORDS`. Comment text is immutable, which is what makes caching sound, but
  the questions and the article extract are not. Reusing an answer across a tuning change
  is worse than wrong: `noul()` and `score()` return 0.0 for a missing key, so a renamed
  question would quietly drag every cached comment down the ranking with nothing in the
  report to show it. A fingerprint mismatch discards the file and re-bills — the correct
  price.

A third was found in the fix itself: a stale lock broken after 30 minutes could be deleted
by its original holder on exit, taking the *successor's* live lock with it. Each holder now
writes a pid + uuid4 token and only unlinks a lock whose token still matches. Read-then-
unlink narrows the window rather than closing it; closing it properly needs `flock` or the
SQLite move below.

### 12.4 For production: SQLite, then Postgres

Once the pipeline polls many articles in parallel, move to one SQLite file — barely more
code, and it brings concurrent writers, crash safety and partial reads that JSON files
cannot give.

```sql
CREATE TABLE classified (
    container_uuid TEXT NOT NULL,
    comment_uuid   TEXT NOT NULL,
    answers        TEXT NOT NULL,
    model          TEXT NOT NULL,
    classified_at  TEXT NOT NULL,
    PRIMARY KEY (container_uuid, comment_uuid)
) WITHOUT ROWID;
```

`INSERT OR IGNORE` makes step 3 idempotent under concurrent runs on the same article,
which is the property the JSON version lacks. The migration is a loop over the JSON files,
so nothing is lost by starting with them — which is the reason to start with them.

Beyond local use the target is **Postgres on Cloud SQL**, for the reason in §19.

### 12.5 Location

Not `output/`, which is disposable. A `state/` directory, gitignored separately, with the
path in configuration so a deployment can point it elsewhere. **Deleting `state/` means
paying Jev again for every comment** — it is machine-local, not source, but it is not
disposable.

## 13. Engagement stays out of scoring

The brief records that the 2024 Viafoura *Top Comments* trial "failed to identify the most
valuable comments and had a tendency to promote sarcastic comments". It ranked on
engagement, and sarcasm attracts likes — which is item 7 on the brief's sift-out list.

Every quality criterion in the brief is intrinsic to the comment text; the sole
thread-relative one, representativeness, is computed from stance labels rather than likes.

So likes and reply counts may inform **retrieval** — which comments to look at, as
`max_comments` + `from` do — and must **not** inform **scoring**. With no retrieval cap,
even that bias disappears.

The brief's one traction hypothesis ("I wonder if genuine suggestions tend to have more
replies?") is a proxy for `proposes_solution`, which Jev is already asked directly.

## 14. What Part II is not

- **Not a scheduler.** Something else decides when to call `run`; this only makes a second
  call cheap.
- **Not a cache with invalidation.** Nothing expires, because an answer is a pure function
  of immutable inputs. A Jev version change is the one exception, and `model` records it.
- **Not multi-article orchestration.** The store is keyed by container so it does not
  obstruct it, but parallel polling is a separate discussion.

---

# Part III — the HTTP API

**Designed 2026-09-22, not yet built.** Turns the pipeline into a local FastAPI service a
UI can call: one endpoint to classify an article's comments, one to record a Community
manager's verdicts on what came back. Those verdicts are the calibration data this project
does not otherwise have (§18).

Scope: **local only.** Not deployed at this stage, so JSON files on disk are adequate. The
one place that decision is load-bearing is §19.

## 15. Viafoura: the MCP server goes away

`src/clients/vf_mcp.py` becomes `src/clients/viafoura.py`; `ViafouraMCPClient` becomes
`ViafouraClient`, talking directly to the public Live Comments REST API.

### 15.1 Why, and what it deletes

Everything the project needs is available anonymously over plain HTTP. Verified live
2026-09-22 against the Calais article (`container_id=A65vpYj1jg7t`,
`content_container_uuid=01a0c441-…`):

| Need | Endpoint |
| --- | --- |
| Resolve id → uuid, container counts | `GET /v4/livecomments/{section_uuid}?container_id={page_id}&limit=0` |
| The thread | `GET /v4/livecomments/{section_uuid}/{content_container_uuid}/comments` |
| Article discovery | `GET /v4/livecomments/{section_uuid}/trending` |

`Comment.from_payload` parses the public payload **unchanged** — same field names,
`is_pinned`, `is_picked`, `state`, `total_likes`, `parent_uuid`, and
`metadata.origin_url` / `origin_title` all present.

**No API key is required to read.** The OpenAPI definition marks both read endpoints
`{"TokenInCookie": ["optional"]}, {"SignedRequest": ["optional"]}`. Access levels are
`optional | user | mod | admin | client`; reads are `optional`, and a token only
personalises the response. Around forty anonymous calls during verification, all 200.

Deleted: the `mcp` SDK dependency, OAuth authorization-code + PKCE, dynamic client
registration, the headless HTML form login, the `~/.jev_ai/` token cache, and the env vars
`VF_MCP_API_KEY`, `VF_MCP_URL`, `VF_MCP_TOKEN_STORE`. **The Viafoura API key stops being a
secret this project handles at all.**

### 15.2 Discovery improves

`GET /v4/livecomments/{section_uuid}/trending` is public and better than the MCP tool it
replaces. Required: `limit` (max **1000**), `sorted_by=total_visible_contents`,
`content_window_hours` (max **48**). Optional: `content_container_window_days` (max **30**),
`date_time`.

Verified: 200 containers returned anonymously, each carrying `container_id`,
**`content_container_uuid`** (so the resolution hop disappears), `origin_url`,
`origin_title`, `date_published`, `total_visible_contents`.

The two windows are different things, which the MCP layer conflated: `content_window_hours`
bounds **comment activity**; `content_container_window_days` bounds **article age**. The
"48h lookback" recorded elsewhere is accurate *for MCP*; going direct gives a month of
articles, which is what makes the calibration harvest viable.

### 15.3 Two silent-failure traps

Both cost real debugging time. Neither raises an error, because the API accepts unknown
query parameters without complaint.

1. **`starting_from` is ignored on the flat `?container_id=…` form.** It returns page 1
   forever. Paginate only on the nested `/{content_container_uuid}/comments` path.
2. **A synthesised cursor is silently ignored.** Comment uuids are genuinely UUIDv7 — the
   embedded 48-bit millisecond timestamp matches `date_created` exactly — so it looks
   possible to fabricate cursors at chosen times and page slices concurrently. The server
   resolves `starting_from` by uuid lookup; an unknown uuid falls back to page 1 and
   returns **HTTP 200**. A parallel fetcher built this way returns the same page N times
   and looks like it worked.

Keep the two guards already in `iter_comments` — the `seen` set and the "cursor did not
advance" check. Trap 2 is exactly what they catch.

### 15.4 Measured limits and timings

Against the 2,680-comment Calais thread:

- **`limit` caps at 100.** Undocumented, hard: `limit=101` returns HTTP 400. The docs state
  no maximum; the server disagrees.
- `limit` counts **top-level only**, so `reply_limit` does not change the page count.
- `reply_limit` caps at 50. A comment with more than 50 replies loses the tail; the
  dedicated `getreplies` / `getdirectreplies` endpoints exist if that ever matters.
- Full walk, `reply_limit=50`: **21 pages, 2,680 comments, 14.6s** (~697 ms/page).
- Same walk, `reply_limit=0`: **21 pages, 2,012 top-level, 13.7s**, and 402 of those
  parents have direct replies.

**Paging one article cannot be parallelised.** `offset`, `page`, `after` and `cursor` are
ignored, and cursors cannot be synthesised. Fetching replies concurrently from the separate
endpoint — suggested by Viafoura's docs assistant — is measurably **worse**: it saves 0.9s
on the top-level walk and then owes 402 extra HTTP calls to recover the 668 replies that
arrive free inline. Paging across *different* articles has no shared cursor and
parallelises freely; that is the harvest, not this API.

**The real saving is to stop early.** Comments are append-only, the store holds every uuid
already classified, and `newest` order puts new comments first. So page newest-first and
stop at the first page whose uuids are all already in the store:

- Cold run: 14.6s. Every run after: one page, ~0.7s. **20× on the common path**, with none
  of the correctness risk of the cursor hack.

Two accepted gaps: a comment restored by a moderator after the fact carries an old uuid,
sits mid-thread and is skipped — mitigated by `full_refresh` and an automatic full sweep
after `[viafoura] full_sweep_after_hours`. And the payload carries `is_edited`: an edited
comment's cached answers are stale, because the store assumes comment text is immutable.
Not addressed; recorded so the assumption is visible.

### 15.5 Client design

One `aiohttp.ClientSession` shared with CAPI — same lifespan object, one connection pool,
and it lets `httpx` go as a direct dependency. Explicit connect/read/total timeouts. Retry
with exponential backoff and jitter on 429 and 5xx, honouring `Retry-After`; **no rate
limits are documented anywhere**, so the default is conservative. Bounded concurrency
semaphore for multi-article use. Async context manager, built once at startup.

### 15.6 Writes stay out of scope

Pinning requires `{"TokenInCookie": ["mod"]}` — a JWT belonging to a Viafoura account
holding the moderator role, not a static bearer key. Obtaining one means logging that
account in through the configured provider and refreshing the token; the alternative
`SignedRequest` (`X-REQUEST_SIGNATURE`) needs a shared secret issued by Viafoura.

A day's code at most, but it needs Viafoura to issue a service identity, someone to own
that credential, and a decision about a bot holding moderator powers — the same role can
delete comments and ban users. The brief lists auto-pinning as out of scope. The read path
stays clean enough that a write path bolts on later if the Community team asks for it.

## 16. Configuration

Two sources, loaded together at startup by `load_settings()`, with **one rule: no key
appears in both**, so there are no precedence puzzles.

- **`.env`** keeps exactly what it has today: secrets, `CAPI_URL`, `ENVIRONMENT`. Nothing
  moves out of it, so nothing secret can leak into a committed file.
- **`config.toml`** takes what is currently hardcoded as module constants. `tomllib` is
  stdlib in 3.12, so no new dependency.

```toml
[viafoura]  section_uuid, base_url, page_size, reply_limit, timeout_seconds,
            full_sweep_after_hours
[jev]       model, concurrency, timeout_seconds, requests_per_minute, tokens_per_second
[article]   max_words
[classification]             min_words
[classification.weights]     per question id
[classification.exclude_at]  per question id
[classification.flag_at]     per question id
[api]       host, port, cors_origins, default_min_score, max_results, min_results,
            lock_timeout_seconds
[paths]     state_dir, feedback_dir, output_dir
```

**The one documented exception:** `HOST` and `PORT` environment variables override
`[api] host` and `[api] port`. Cloud Run injects `PORT` and will not route to a container
bound to loopback, so the default is `127.0.0.1` locally and a deployment sets `0.0.0.0`
without a code change.

**Two guards, both mandatory.**

1. Validate every question id under `[classification.*]` against `BATTERY` at startup and
   fail loudly. A typo'd id otherwise scores 0.0 in silence — precisely the failure mode
   the fingerprint exists to prevent.
2. `[article] max_words` feeds the fingerprint. Changing it **discards the store and
   re-bills the whole thread**. Weights and thresholds do not: they are applied at scoring
   time, so recalibration stays free. This asymmetry must be stated wherever the file is
   documented, because the two sit next to each other and behave completely differently.

## 17. `POST /v1/classifications`

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | str | required | Must match the Telegraph allowlist |
| `min_score` | float | 0.50 | Proposal threshold |
| `max_results` | int | 25 | Caps the sift burden the brief warns about |
| `min_results` | int | 5 | If too few clear the bar, still show the best N |
| `include_replies` | bool | true | Replies can go in carousels, cannot be pinned |
| `include_already_pinned` | bool | false | A manager may want to see their own pins |
| `max_comments` | int \| null | null | null = the whole thread |
| `from` | `newest` \| `oldest` | `newest` | With `max_comments`: last N or first N |
| `full_refresh` | bool | false | Skip the early-stop optimisation |

`max_comments` selects **before** classification, so it decides what Jev is paid for and
what can ever be proposed: `newest 200` of a 2,680-comment thread means an excellent
comment from hour one cannot surface. It is a speed dial, not a ranking preference. Pinned
comments float to the top of every sort, so they are always inside the window.

POST rather than GET because the call spends money and writes state.

### 17.1 Response

Envelope: `request_id`, `article` (url, headline, container_uuid, page_id), `model`,
`battery_fingerprint`, `scoring_fingerprint`, `params_applied`, a `summary` block (totals,
newly classified vs reused, estimated cost, duration), `comments` **sorted by score
descending**, and a compact `excluded` list of `{uuid, reason}` so nothing vanishes
silently.

Per comment: `uuid`, `parent_uuid`, `is_reply`, `pin_eligible`, `is_pinned`, `is_picked`,
`is_top_comment`, `rank`, `score`, `signals`, `flags`, `text`, `actor_uuid`, `created_at`,
`likes`, `dislikes`, `total_replies`, and `answers`.

**`answers` is restructured at the boundary.** The store's flat `tone__confidence` key is
an internal encoding and must not leak:

```json
"tone": { "type": "score", "value": 1.8, "confidence": 0.91,
          "probabilities": {…}, "legend": {…} }
```

### 17.2 Answer fields available

Established by inspecting the SDK types:

| Primitive | Count | Fields |
| --- | --- | --- |
| Noul | 9 | `noul` only — **no confidence exists**; the value *is* the calibrated probability |
| Score | 4 | `score`, `confidence`, `probabilities`, `legend` |
| Choice | 1 | `choice`, `confidence`, `probabilities` |

`_read_answers` currently drops `probabilities` and `legend` on Score questions. It starts
capturing them. This does **not** invalidate the store — the fingerprint hashes the
questions, not the parsing — so no re-billing. But answers banked before the change cannot
be backfilled without paying again, so they stay thinner on those four questions. The API
omits an absent field rather than emitting an empty one.

(`tests/conftest.py` gives fake Noul answers a `.confidence` the real type does not have.
Fix while here: a fixture asserting an impossible field.)

## 18. `POST /v1/feedback`

### 18.1 One review event, not one tick

The payload covers **every comment that was on screen**, each with its two booleans — not
just the ones ticked. If only the ticks arrive, an unticked comment is ambiguous between
"rejected" and "never looked at", and the dataset collapses to positives-only: the exact
problem that makes mining pinned comments unusable for calibration. With the full set,
**absence of a tick is data**, and the rejects become as valuable as the accepts.

This is the single design decision that determines whether the feedback is worth
collecting.

### 18.2 The record snapshots what was shown

Denormalise the score, the answers, the model, the fingerprints and the comment text into
the record. Do **not** point into the store. Scores change when weights are retuned,
answers change when questions are reworded, and the comment itself can be edited or deleted
in Viafoura. Calibration needs to know what the manager actually saw.

```
review_id, submitted_at (UTC, seconds), reviewer (optional, nullable),
request_id, article {url, container_uuid, headline},
model, battery_fingerprint, scoring_fingerprint, params,
items: [ { comment_uuid, rank, score, pin, carousel,
           was_already_pinned, is_reply, text, answers, signals, note } ]
```

- **`reviewer`** is optional and nullable by decision — few people will use this. Included
  anyway because it costs one line, and without it inter-rater agreement cannot be
  measured, which is what sets the ceiling on achievable accuracy.
- **`note`** per item: free text on rejects. Five rejects all saying "just opinion, no
  experience" identifies a question to sharpen; five booleans never will.
- **`scoring_fingerprint`** hashes the resolved `[classification]` section. Since weights
  are now editable without a code change, a bare score is otherwise uninterpretable later.

Revisions are append-only: a second review is a new event with a later timestamp, and
analysis takes the latest. No update logic, and it maps to an INSERT-only table.

## 19. Storage

One file per review event, `feedback/<review_id>.json`, written with the atomic write
already proven in `store.py`.

**Not JSONL.** A review record is 20–60KB, far above the 4KB limit below which an
`O_APPEND` write is atomic, so concurrent appends could interleave and corrupt the file.
One file per event needs no locking at all.

`src/processing/feedback.py`, module functions mirroring `store.py` — a thin seam, not an
abstract repository. Postgres later means two tables (`review`, `review_item`); the JSON
keys above are chosen to map straight across.

**Local only.** On Cloud Run the filesystem is ephemeral and per-instance: `state/` and
`feedback/` would be lost on restart and not shared between instances. Losing `state/`
means re-billing Jev for every comment. Postgres on Cloud SQL is a precondition for
deploying, not a later improvement.

## 20. Concurrency, security, layout

**Lock contention.** The store takes a per-article exclusive lock, so two managers opening
the same article means the second request blocks for the length of a cold run. Bound the
wait at `[api] lock_timeout_seconds` and return **409** with a retry hint rather than
hanging the connection.

**One `JevClient` per process** (§3), built in the lifespan alongside settings and the
shared `aiohttp` session, injected via `Depends` — which is also the seam that lets tests
substitute `FakeJev`.

**Security.** No auth by decision: this runs on a private network. What still applies:

- **Telegraph URL allowlist**, so the endpoint cannot forward an arbitrary URL to CAPI.
- Bind `127.0.0.1` by default; `HOST=0.0.0.0` only where the platform requires it.
- CORS pinned to the UI's origin, never `*`.
- **XSS moves to the UI.** JSON encoding makes our side safe, but comment text is untrusted
  reader input: the UI must not put it through `innerHTML`. Part of the contract.
- Never log comment text, settings, or `reviewer`.

**Layout.**

```
src/api/app.py       lifespan, middleware, app factory
src/api/routes.py    the two endpoints
src/api/schemas.py   pydantic models
src/processing/feedback.py
src/clients/viafoura.py   (was vf_mcp.py)
config.toml
```

`src/api` must be added to `[tool.hatch.build.targets.wheel] packages`, then `uv sync`.
New dependencies: `fastapi`, `uvicorn[standard]`. Removed: `mcp`.

**Refactor.** `workflow.run()` currently orchestrates, renders Markdown and prints. Extract
the orchestration into `classify_article()` that both the CLI and the API call, leaving the
Markdown renderer a CLI concern.

**Tests.** Starlette's `TestClient` uses an in-process ASGI transport and opens no sockets,
so the autouse `_no_network` fixture stays satisfied — verify rather than assume. Cover:
URL allowlist rejection, param validation and clamping, descending order, already-pinned
excluded unless requested, `max_comments` + `from` selection, early-stop paging,
`full_refresh`, 409 on lock contention, feedback round-trip, and that no route reaches Jev.

---

# Part IV — cost, risks, open questions

## 21. Cost and performance

Per comment: article (~800 tokens capped) + comment (~70) + battery (~2,500) ≈ 3,400 input
tokens. Jev charges input tokens only; output is free.

| | figure | source |
| --- | --- | --- |
| Cost per comment | ~$0.000146 | measured |
| Cost per 1,000 comments | ~$0.15 | measured |
| 1,454-comment article, first run | **$0.2120 in 21.2s** | measured; predicted 20.8s |
| Same article, second run | **$0.0000, 0 requests** | measured |
| 1,000-comment thread polled every 5 min for a day | **$0.13** vs $38.75 without the store | calculated |
| Viafoura full thread fetch (2,680 comments) | 14.6s cold, ~0.7s warm | measured |

The rate limiter was validated live: 1,454 requests in 21.2s against a predicted 20.8s.

## 22. Risks and things to watch

- **Sarcasm is the hard one.** It is the failure mode that killed Viafoura's own Top
  Comments tool, and Jev's documentation admits the model can be steered by text arguing
  for its own classification. Expect this question to need the most iteration.
- **Literal reading.** Jev answers the question as written, not as intended. Criteria will
  need rewording against real Telegraph comments.
- **The article cap is a trade-off.** 600 words may cut context that `unverified_claim`
  needs to tell "not in the article" from "further down the article". Worth testing at a
  couple of cap sizes — but note it re-bills (§16).
- **No ground truth yet.** Everything above is unvalidated against what editors actually
  pick. Part III's feedback endpoint is the fix; until it has data, the weights are
  informed guesswork.
- **Pinned ≠ negative labels.** When calibration data does arrive: a manager pinning one
  comment out of 600 does not mean the other 599 were rejected. Recall is measurable,
  precision is not, and tuning to penalise unticked comments would make the tool worse.
  This is why §18.1 insists the feedback payload covers every comment shown.
- **Deleted and re-moderated comments.** A comment removed after classification stays in
  the store and would still rank. Cheapest fix: honour `Comment.state` on re-fetch and mark
  rows withdrawn.
- **Article edits.** A rewritten article invalidates every answer against it, since the body
  is in the state. Rare; a note rather than a mechanism.
- **Store growth.** ~1 KB per comment, whole map read into memory each run. One of the
  triggers for the move to SQLite.

## 23. Open questions

1. **Question wording** (§6) — the part that decides whether the classifier is any good.
2. **Weights** (§8.3) and **thresholds** (§8.2) — plausible starting points, unvalidated.
3. **Article body in state** — worth the tokens, or headline + standfirst only?
4. **Calibration method.** Mining pinned comments gives positives only; the agreed approach
   is to put a stratified, blind, shuffled sample in front of two or three Community
   managers and measure inter-rater agreement to establish the ceiling. Part III's feedback
   endpoint is the mechanism.

## 24. Where the documentation lives

| Document | Purpose |
| --- | --- |
| `CLAUDE.md` | Project baseline: tooling, the `.env` rule, layout, conventions |
| **This file** | The full spec — pipeline, store, API, questions, thresholds, weights |
| `.claude/memory-bank/progress.md` | What is built, and the implementation plan with checkboxes |
| `.claude/memory-bank/` | Also: active context, decision log, lessons learnt |
| `.claude/skills/jev-question-tuning/` | How to change a question and prove the change helped |
| `.claude/agents/pipeline-qa.md` | Runs the suite and reviews a diff before work is called done |
| `.claude/agents/comment-quality-auditor.md` | Audits a report against the brief |
| `Identifying good comments POC _ Brief.md` | The original Community team brief |
