# Spec: comment classification with Jev — iteration 1

Status: **built**. The pipeline below runs end to end; the question wording, weights and
thresholds are what still need your review (§13).

## 1. Purpose

Take one Telegraph article, pull its top N comments, ask Jev a battery of typed questions
about each one, combine the answers in code, and write a Markdown report a Community
editor can read to judge whether the classifier agrees with them.

This iteration is a **measurement instrument, not a product**. Its job is to make Jev's
judgments visible next to the comment text so we can tune question wording and weights
before wiring anything into a real workflow. The Markdown output is deliberately
throwaway; a later iteration replaces it with JSON, a UI, or a Viafoura integration.

## 2. Scope

**In scope**

- Load settings once at startup and hold them in memory, ready to become a FastAPI
  service later (§3.1).
- Fetch the article (headline, standfirst, body) from CAPI.
- Fetch the top N comments from Viafoura and keep them **in memory** — no intermediate
  JSON file, unlike `src/main.py` today.
- Classify each comment with one Jev request carrying the whole question battery.
- Compute deterministic signals (word count, caps, URLs) in code.
- Apply exclusion rules and a weighted quality score, both defined in code.
- Render a Markdown report: ranked shortlist, excluded comments with reasons, thread-level
  stance summary.

**Out of scope for this iteration**

| Deferred | Why |
| --- | --- |
| Audience segments (Advocate / Traditionalist / Curator) | Luis's call: needs specialist segment definitions first; revisit once we have them. |
| Viafoura's moderation word list | Not obtained yet. |
| Commenter usernames | The MCP server returns `actor_uuid` only, no usernames. The brief needs them; sourcing them is a separate problem. |
| Auto-pinning, carousel generation, Viafoura writes | Explicitly out of scope in the brief. |
| Tuning against the pinned-vs-approved spreadsheet | Next iteration. This one produces the tool that will be measured. |
| Batching several comments per Jev request | Optimisation. One comment per request is the accurate baseline; batching gets tested against it later. |

## 3. Prerequisites

| Gap | Status |
| --- | --- |
| `src/processing/__init__.py` | **Done** — you added it. |
| `load_config.py` deleted, but `capi.py` still imports it | **Done.** `settings.py` written (§3.1) and `capi.py` fixed (§3.2). Verified with a live CAPI call. |
| Imports break for a script inside `processing/` | Blocking. Python puts the *script's* directory on `sys.path`, so `src/processing/workflow.py` gets `src/processing` and `from clients.capi import …` fails. Confirmed by test. Run with `PYTHONPATH=src uv run python src/processing/workflow.py`, documented in CLAUDE.md. The tidier long-term fix is an installable package with a build backend; not worth it yet. |

`log_config.py` is self-contained and works as-is. Everything written here uses its
`logger` rather than `print`, except the final report path.

`src/config.toml` is empty and now unused. Delete it unless you want TOML config back.

### 3.1 Settings: load once, reuse for every request — **built**

The design target is a FastAPI service, so settings are read **once at process start** and
held in memory; nothing re-reads `.env` per request.

```python
# src/processing/settings.py
@dataclass(frozen=True, slots=True)
class Settings:
    capi_url: str
    content_reader_apigee_key: str
    typesafe_api_key: str
    viafoura_api_key: str
    vf_section_uuid: str | None
    environment: str = "dev"


def load_settings() -> Settings: ...  # reads .env once, validates, caches
def get_settings() -> Settings: ...  # returns the cached instance
```

- `load_settings()` calls `load_dotenv()` once, reads the variables, **fails fast naming
  every missing one at once** rather than dying on the first `KeyError`, and stores the
  instance in a module-level singleton.
- `get_settings()` is what every other module calls. No I/O, no environment access, and it
  raises a clear error if startup never ran.
- Frozen dataclass, so nothing can mutate config mid-run.
- **Today (CLI):** `workflow.py` calls `load_settings()` once at the top of `main()`.
- **Later (FastAPI):** call it in the `lifespan` startup handler; request handlers call
  `get_settings()`. No other code changes.

It never logs or prints values, so the CLAUDE.md rule against reading `.env` still holds.

### 3.2 The edit `capi.py` needed — **done**

It imported the deleted module and read config by subscript. Four lines changed; the retry
logic and payload construction are untouched:

```diff
-from processing.load_config import AppConfig
+from processing.settings import Settings

-    app_config: AppConfig,
+    settings: Settings,

-    CONTENT_READER_APIGEE_KEY = app_config["CONTENT_READER_APIGEE_KEY"]
-    CAPI_URL = app_config["CAPI_URL"]
+    CONTENT_READER_APIGEE_KEY = settings.content_reader_apigee_key
+    CAPI_URL = settings.capi_url
```

**How config reaches `capi.py`, and why nothing more is needed.** It already takes both
the session and the config as arguments. That is dependency injection, and it is the
pattern that survives the move to FastAPI untouched: the caller owns the lifecycle, the
function stays pure, and a test can pass a hand-built `Settings` with no environment and
no patching. Adding a `CapiClient` class or having `capi.py` reach for `get_settings()`
itself would both be more code and worse — a hidden global makes the function depend on
startup ordering. So the answer to "how do we pass the variables in" is: exactly as it
does now.

```python
# CLI today
settings = load_settings()
async with aiohttp.ClientSession() as session:
    ucms = await get_ucms([url], session, settings)

# FastAPI later — built once in lifespan, reused per request
app.state.session = aiohttp.ClientSession()
app.state.settings = load_settings()
...
ucms = await get_ucms([url], request.app.state.session, request.app.state.settings)
```

### 3.3 Long-lived clients, same principle

Config is not the only thing that should outlive a request. In the FastAPI version these
are built once at startup too:

| Client | Why it must be shared |
| --- | --- |
| `aiohttp.ClientSession` | Connection pooling; creating one per request is a known anti-pattern. |
| `ViafouraMCPClient` | Holds an OAuth token and an open MCP session. Rebuilding it per request redoes the handshake. |
| `AsyncTypeSafeClient` | Connection pooling against the Jev endpoint. |

For iteration 1 the CLI opens all three for the duration of one run, which is the same
lifecycle, just shorter. They are constructed in `workflow.py` and passed down into
`fetch.py` and `classification.py`, never created inside them. That way the FastAPI
version changes only who owns the lifespan, not the pipeline code.

## 4. Inputs

### 4.1 Article, via CAPI

`clients.capi.get_ucms([url], session, app_config)` returns `{url: ucm}`. Verified
against a live article; the fields we need are:

| Field | Path in the UCM |
| --- | --- |
| Headline | `content.headline` |
| Standfirst | `content.standfirst` |
| Body | `content.body[]`, keeping blocks whose `type` is `text` or `heading`, joining their `data` |

The `data` values are **plain text already** — no HTML to strip; `html-data` is the
marked-up twin and we ignore it. Other block types (`image`, `call-to-action`) are
dropped. The test article gave 37 blocks, 24 text and 5 heading, reconstructing to 1,156
words of clean copy.

This is strictly better than scraping the page, which is why the earlier draft's
HTML-extraction plan is dropped.

Body text is **capped** before it enters `state` (default 600 words, §10). Jev's accuracy
falls as the state fills with material the question does not need, and the article is
re-sent with every comment, so it dominates cost.

**URL matching.** CAPI matches on the exact URL, so the input is normalised first: strip
any `#vf-…` fragment and query string. If the workflow is given a page id or container
UUID rather than a URL, it fetches one comment and takes `article_url` from its Viafoura
metadata, then calls CAPI with that.

### 4.2 Comments, via Viafoura

`ViafouraMCPClient.get_comments(article, limit=N)` — already built and tested. Per comment
we carry `content_uuid`, text, `created_at`, `is_reply`, `parent_uuid`, likes,
`total_replies`, `is_pinned`, `is_picked`.

Replies arrive nested under their parent in the same page, so code builds a
`uuid -> text` map and attaches `parent_text` to each reply. Jev needs it to judge whether
a reply stands on its own.

Everything stays in memory as `Comment` objects and flows straight into classification.

## 5. Pipeline

1. **`workflow.py`** calls `load_settings()`, opens the shared clients, and normalises the
   article input.
2. **`fetch.py`** gets the article from CAPI and the comments from Viafoura, concurrently,
   and returns an in-memory `ArticleThread(article, comments)`.
3. `fetch.py` builds the `uuid -> text` map and attaches `parent_text` to replies.
4. **`classification.py`** runs deterministic signals over every comment (no API calls).
5. It **skips Jev** for comments failing a hard code-side rule (§8.1) — no point paying to
   classify a 4-word comment. They are recorded as excluded, with the reason.
6. For every survivor it sends one Jev request (article + comment as `state`, the whole
   battery as `questions`), concurrently behind a semaphore, and returns a list of
   `Classification` records: the raw Jev answers plus the code-side signals.
7. It aggregates thread-level stance shares, then scores and ranks.
8. **`workflow.py`** renders the Markdown and writes it to `output/`.

No step writes an intermediate file. The only output is the report.

Steps 2 and 4–7 take the clients as arguments and never construct them, so the same
functions serve a FastAPI request handler unchanged (§3.3).

## 6. The Jev question battery

14 questions, one request per comment, all evaluated in parallel by the model. Question
ids are for our code only and are never sent, so each `instructions` is fully worded.

Every Noul is phrased so **high means the property is present**. Every Score runs
**worst to best**, so higher is always better. That consistency keeps the scoring code
free of special cases and matches the docs' advice against inverted criteria.

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
| `stance` | Choice, 4 options | `supportive` / `critical` / `mixed` / `no_position` on the article's subject. Feeds representativeness, not quality. |

### 6.2 Exclusion signals (what we sift out)

| id | type | what it asks |
| --- | --- | --- |
| `sarcasm` | Noul | Does it say the opposite of what it means? Criteria carry the brief's own examples: fake praise of the Telegraph, Guardian comparisons, "cultural enrichment", thanking moderators. |
| `on_topic` | Noul | Does it address the article, rather than the Telegraph, the comment section or other commenters? (Low value excludes.) |
| `unverified_claim` | Score, 3 levels | Level 0 no outside claims, level 1 general/common knowledge, level 2 specific checkable statistics, dates or quotations not in the article. Only level 2 excludes. Changed from a Noul after review: the pinned SCTV comment in the sample spreadsheet lists cast members and a binary question would have wrongly excluded it. |
| `personal_attack` | Noul | Insults or mocks a specific person or other commenters. |
| `group_hostility` | Noul | Contempt towards people by nationality, ethnicity, religion, sex, sexuality, disability or age. |
| `profanity_or_threat` | Noul | Swearing, slurs, threats. |

### 6.3 Full definitions

The exact `instructions` and `criteria` for all 13 questions are the ones worked out
earlier in the design conversation, including structured criteria with `what`, `not_for`
and `examples` fields for `sarcasm`, `proposes_solution` and `unverified_claim`. They live
in `questions.py` as the single place to edit during tuning.

**This is the part most worth your review.** The wording of the criteria, not the code,
determines whether the classifier agrees with the Community team.

## 7. Why these and not others

Three brief criteria are **not** Jev questions, because code does them exactly and Jev
does them badly (its own documentation says it cannot count or do arithmetic reliably):

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
review** in the middle band. Thresholds live in one dict, so changing policy is a number
change under review rather than a reworded question.

| signal | excludes at | flags at |
| --- | --- | --- |
| `sarcasm` | ≥ 0.70 | 0.40–0.70 |
| `personal_attack` | ≥ 0.70 | 0.40–0.70 |
| `group_hostility` | ≥ 0.60 | 0.35–0.60 |
| `profanity_or_threat` | ≥ 0.60 | 0.35–0.60 |
| `unverified_claim` (score) | ≥ 1.5 | 1.0–1.5 |
| `on_topic` | < 0.35 | 0.35–0.60 |

Flagged comments still appear in the report, marked, rather than disappearing. Editors
should see the borderline cases; that is how we learn where the thresholds belong.

### 8.3 Quality score

Each Score is normalised to 0–1 by dividing by its top level number, per the composite
scoring pattern in the TypeSafe docs. Starting weights, to be tuned:

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

### 8.4 Pin eligibility

Replies cannot be pinned. Every shortlisted comment is marked pin-eligible or
carousel-only based on `is_reply`. No separate scoring path; it is a label.

## 9. Output: the Markdown report

One file per run, `output/classification_<container_id>_<YYYYmmdd-HHMM>.md`:

1. **Header** — article headline, URL, container id, run timestamp, model id, counts
   (fetched / skipped pre-Jev / classified / excluded / flagged / shortlisted), token
   usage and estimated cost.
2. **Thread summary** — stance distribution as a small table; it drives representativeness,
   so it needs to be visible.
3. **Shortlist** — top N by quality score, each with rank, score, pin-eligible or
   carousel-only, Viafoura likes and reply count, the full comment text, and a compact
   line of the Jev numbers behind the score.
4. **Flagged for review** — the middle band, with which signal fired and its value.
5. **Excluded** — grouped by reason, comment truncated. The point is spotting false
   exclusions.

Numbers go in tables or on their own lines, never buried in prose, so a reader can scan
for disagreements with their own judgment.

## 10. Files and configuration

**Yes, `workflow.py` as the orchestrator is the right shape**, and per your call the
Markdown rendering lives inside it rather than in its own module — the report is a
temporary way to inspect the pipeline, so it does not deserve a permanent home. When the
output becomes JSON for an API, that rendering code is deleted rather than refactored.

Layout under `src/processing/`:

| File | Responsibility |
| --- | --- |
| `workflow.py` | Orchestrator and entry point. Loads settings, owns the clients, normalises the input, calls fetch → classify, renders the Markdown and writes it. Holds the run configuration constants. |
| `fetch.py` | Article from CAPI + comments from Viafoura, concurrently. Returns in-memory objects. No file writes. |
| `questions.py` | The Jev battery. Edited constantly during tuning, so kept alone for readable diffs. |
| `classification.py` | Deterministic signals, the Jev calls, thresholds, weights, ranking. Returns `Classification` records. |
| `settings.py` | **New.** Load-once config singleton (§3.1). |
| `log_config.py` | Unchanged, already present. |
| `__init__.py` | Already added. |

Four working modules plus settings and logging. If the Markdown rendering inside
`workflow.py` grows past roughly 100 lines it should move out, but it is a rendering
function over one list of records, so it should not.

Configuration is module constants at the top of `workflow.py`, matching the style of
`src/main.py` — no CLI flags beyond an optional article argument:

| knob | default | why |
| --- | --- | --- |
| `ARTICLE` | a URL | URL preferred; page id or container UUID also accepted. |
| `TOP_N_COMMENTS` | 50 | Keeps an iteration cheap and fast while tuning. |
| `RANKED_BY` | `most_liked` | How Viafoura picks the top N before we classify. |
| `ARTICLE_MAX_WORDS` | 600 | Caps the dominant token cost; also guards Jev's context-rot weakness. |
| `CONCURRENCY` | 10 | Well inside Jev's 1,200 requests/minute. |
| `SHORTLIST_N` | 15 | "More than enough for a carousel, not so many that sifting is a burden." |
| `MODEL` | `jev-1.13.0` | Pinned, not `jev-latest`: thresholds tuned on one version should not silently move. |

Run with:

```
PYTHONPATH=src uv run python src/processing/workflow.py [article]
```

## 11. Cost and performance

Per comment: article (~800 tokens capped) + comment (~70) + battery (~2,500) ≈ 3,400
input tokens.

| | estimate |
| --- | --- |
| Cost per comment | ~$0.00014 |
| Cost per 1,000 comments | ~$0.15 |
| 50-comment run (default) | under $0.01, a few seconds |
| Context headroom | ~3.4k of a 64k budget |

Jev charges input tokens only; output is free.

## 12. Risks and things to watch

- **Sarcasm is the hard one.** It is the failure mode that killed Viafoura's own Top
  Comments tool, and Jev's documentation admits the model can be steered by text arguing
  for its own classification. Expect this question to need the most iteration.
- **Literal reading.** Jev answers the question as written, not as intended. Criteria will
  need rewording against real Telegraph comments.
- **The article cap is a trade-off.** 600 words may cut context that `unverified_claim`
  needs to tell "not in the article" from "further down the article". Worth testing at a
  couple of cap sizes.
- **Ranking by likes biases the sample.** Taking the top N by likes means we tune on
  comments readers already promoted, which are not a random sample of the thread. Fine for
  iteration 1; worth a random sample later.
- **No ground truth in this iteration.** The report tells us what Jev thinks, not whether
  it is right. That judgment is yours and the Community team's until we run the
  spreadsheet.

## 13. Decisions

**Settled and built**

- `load_config.py` deleted; `settings.py` written (§3.1) and verified.
- `capi.py` fixed (§3.2) and verified against live CAPI.
- Markdown rendering lives in `workflow.py`, not a separate module.

**Still open, before I write the pipeline**

1. **Question wording** — the substance of §6. Anything that misreads what the Community
   team means? This is the part that decides whether the classifier is any good.
2. **Weights** (§8.3) and **thresholds** (§8.2) — plausible starting points?
3. **Article body in state** — worth the tokens, or start with headline + standfirst only
   and add the body if the article-relative questions look weak?
4. **`src/config.toml`** — empty and now unused. Delete it?
