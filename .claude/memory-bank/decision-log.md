# Decision log

Decisions that shaped the code, and why. If you are about to change one of these, read
the reasoning first: most were made against a constraint that still holds.

---

## 1. Jev alone for the classification, no LLM in the loop

**Date:** Sept 2026, during the feasibility review.

Every criterion in the brief is a judgment a knowledgeable person makes in a second about
one comment, which is exactly the shape Jev is built for. An LLM would be slower and far
more expensive at the same job, and its output would need parsing.

An LLM would add value in only three optional places, none of them core: proposing
article-specific stance options instead of the generic four, writing an editor-facing
sentence explaining a pick, and re-reading comments in the uncertain middle band.

---

## 2. Anything countable stays in code

Word count, ALL CAPS ratio and URL detection are done in Python, not asked of Jev. The
model's own documentation states it does not count reliably and is not a calculator. Code
does these exactly and for free.

This also saves money: a comment failing a code-side rule, such as being under 15 words,
is excluded **before** any API call.

---

## 3. Representativeness is computed, not asked

It cannot be judged from a single comment. It is the thread-wide share of that comment's
stance, so `classification.py` classifies everything first, then aggregates stance shares,
then scores. No extra API calls.

---

## 4. `unverified_claim` is a 3-level Score, not a yes/no

**Changed after review of the sample spreadsheet.** A pinned comment listing SCTV cast
members would have been wrongly excluded by a binary question. The three levels separate
"no outside claims" from "general or widely known facts" from "specific checkable
statistics, dates or quotations", and only the top level excludes.

---

## 5. Every question is phrased so high means the property is present

Never invert a question to make its number read the other way. The TypeSafe docs warn
that criteria contradicting the instruction score worse. For quality questions high is
good; for exclusion questions high is bad. `classification.py` decides what each direction
means. `on_topic` is the one where a **low** value excludes.

---

## 6. The model version is not pinned — reversed 2026-09-21

Originally `MODEL = "jev-1.13.0"`, on the grounds that thresholds tuned against one
version should not silently move when an alias advances. **Reversed on Luis's call:**
`MODEL = None` in `workflow.py`, so requests take the SDK default (`jev-latest`) and the
POC picks up model improvements without a code change.

The reproducibility the pin was bought for is recovered a different way: `classify_thread`
reads the resolved version off each response and `ClassificationResult.model` reports it,
so the report names the model that actually answered rather than the one requested. That
labels a run after the fact; it does not stop a threshold shifting mid-tuning. If the
spreadsheet tuning turns into real calibration, pin `MODEL` for the duration of it.

---

## 7. Article text comes from CAPI, not from scraping the page

An earlier draft planned to scrape `article-body-text` blocks out of the HTML. CAPI is
strictly better: `content.body[]` gives typed blocks whose `data` field is **already plain
text**, so there is no markup to strip. Keep the `text` and `heading` blocks, drop
`image` and `call-to-action`.

---

## 8. Comments are fetched by container UUID, and ids are resolved through Viafoura's public API

The MCP server's own id-lookup tools are broken (see `lessons-learnt.md`). The client
resolves a page id or URL through `livecomments.viafoura.co` instead, which needs no
authentication. This is a workaround for a server-side bug and should be revisited if
Viafoura fix it.

---

## 9. Settings are loaded once and passed down, never read from a global

`processing/settings.py` reads `.env` exactly once at startup and caches a frozen
`Settings`. Functions **take a `Settings` argument** rather than calling `get_settings()`
themselves.

The reason is the stated goal of a FastAPI service. This shape means the move costs
nothing: `load_settings()` shifts into the lifespan handler, and the pipeline functions do
not change at all. It also keeps them testable with a hand-built `Settings`, with no
environment and no patching.

The same applies to the three long-lived clients (`aiohttp.ClientSession`,
`ViafouraMCPClient`, `AsyncTypeSafeClient`). They are built in `workflow.py` and passed
down. The Viafoura one especially: it holds an OAuth token and an open session, so
rebuilding it per request would redo the handshake every time.

**Rejected alternative:** giving `capi.py` a `CapiClient` class, or having it call
`get_settings()` itself. Both are more code, and a hidden global makes a function depend
on startup ordering.

---

## 10. The Markdown report is deliberately throwaway

Its job is to make Jev's judgments visible next to the comment text so a human can say
where the classifier is wrong. When the output becomes JSON for an API, the rendering half
of `workflow.py` is **deleted, not refactored**. That is why it lives inside the
orchestrator rather than in its own module.

---

## 11. Nothing is written to disk between pipeline stages

Comments stay in memory from fetch through classification to rendering. The only file
written is the report. An earlier version of the Viafoura harness dumped comments to JSON;
that was dropped on request.

---

## 12. Audience segments are deferred

Luis's call. Jev could do it, but only with written definitions of the Advocate,
Traditionalist and Curator segments including example comments, and even then a single
comment is thin evidence about a person. Expect many "unclear" answers and read the
probability distribution rather than just the winning label.
