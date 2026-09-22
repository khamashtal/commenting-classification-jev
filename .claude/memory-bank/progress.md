# Progress

_Last updated: 21 September 2026_

## Built and verified against live services

| Component | File | Evidence it works |
| --- | --- | --- |
| Viafoura MCP client | `src/clients/vf_mcp.py` | Headless OAuth completes; fetched 409 comments across 2 pages in ~2s; accepts URL, page id or UUID |
| CAPI article client | `src/clients/capi.py` | Live call returned headline, standfirst and 1,182 words of body |
| Settings | `src/processing/settings.py` | Loads once, caches, redacts secrets in `__repr__`, names all missing vars at once |
| Question battery | `src/processing/questions.py` | 14 questions: 9 Noul, 4 Score, 1 Choice |
| Fetch stage | `src/processing/fetch.py` | Article and comments fetched concurrently when given a URL |
| Classification stage | `src/processing/classification.py` | Code-side signals, Jev calls behind a semaphore, thresholds, weighted score |
| Orchestrator and report | `src/processing/workflow.py` | Two full runs, reports in `output/` |
| Viafoura test harness | `src/main.py` | Separate from the pipeline; useful for poking at comment data |
| TypeSafe experiment | `experiment_1.py` | Luis's original Jev example, unrelated to the pipeline |

Ruff passes across the project.

## Runs so far

| Article | Comments | Shortlisted | Excluded | Cost | Time |
| --- | --- | --- | --- | --- | --- |
| Singapore longevity | 5 | 3 | 2 | $0.0007 | 5.0s |
| Burnham capital gains tax | 20 | 4 | 16 | $0.0025 | 4.7s |

**The result that matters:** on the capital gains article the comment the Community team
had actually pinned ranked **first at 0.946**, against 0.454 for second. Its signals were
personal experience 2.97/3, relevance 0.96, readability 2.00/2, sarcasm 0.03.

Reports are in `output/`, named `classification_<container_uuid>_<timestamp>.md`.

## Known problems

1. **`profanity_or_threat` over-fires on insults**, not profanity. Excluded a
   personal-experience comment containing no swearing.
2. **`unverified_claim` fires often.** 4 of 17 exclusions in one run.
3. **Tone cannot sink a comment.** An angry comment scoring 0.00 on tone still reached
   rank four, because tone is 15% of the weight. May want a floor instead.
4. **No ground truth yet.** The reports say what Jev thinks, not whether it is right.

## Not started

- Tuning against the Community team's pinned-vs-approved spreadsheet. This is the real
  measurement and everything above is currently unvalidated guesswork by comparison.
- Audience segments.
- Usernames (needs a source other than the MCP server).
- Viafoura's moderation word list as a hard filter.
- Batching comments per Jev request. Investigated 2026-09-21 and **declined**: the
  saving is ~25% of tokens, not the docs' 12x, because the battery is 2.5x the article
  and repeats per comment inside a batch. Against that, Jev's own jaggedness page warns
  accuracy falls as the state fills with irrelevant detail — which the other comments in
  a batch are. Revisit above ~10,000 comments per article, where the 20 req/s cap starts
  to bind.
- FastAPI service. The code is shaped for it but nothing is written.

## Built 2026-09-22

- **Incremental classification.** `processing/store.py` keeps each comment's Jev answers
  in `state/<container>.json`, keyed by comment uuid. A second run pays only for comments
  it has never seen: a 1,000-comment thread polled every five minutes costs $0.13 a day
  rather than $38. There is no timestamp cursor, deliberately — see the module docstring.
- **Reports are rewritten in place**, one per article, rather than a new timestamped file
  per run. The run counter is in the report header.
- **`clients/jev.py`.** The Jev calls moved behind a client alongside `vf_mcp` and `capi`,
  taking the rate limiting, concurrency bound, retry policy and usage accounting with
  them. `classify_thread` lost six parameters in the process.
- **Rate limiting.** Dual token buckets against both published limits, halving on an
  observed 429 and recovering on a clean streak. A semaphore alone could not do this: it
  bounds requests in flight, not rate, and those only coincide at one latency.
- **No page is ever fetched.** The Viafoura container id comes from CAPI's
  `metadata.page-id`; scraping returned HTTP 402 on every premium article. The scraping
  code is deleted, not disabled.
- **A test suite**, `tests/`, ~115 tests in under four seconds, none of which call Jev.
  `tests/test_guards.py` enforces the standing decisions above as executable rules.
- **A `pipeline-qa` agent** that runs the suite and reviews a diff for correctness, async
  hygiene, security, performance and test quality.

## Where the documentation lives

| Document | Purpose |
| --- | --- |
| `CLAUDE.md` | Project baseline: tooling, the `.env` rule, layout, conventions |
| `.claude/comment-classification-spec.md` | The full spec: pipeline, questions, thresholds, weights, open decisions |
| `.claude/memory-bank/` | This folder: state, decisions, lessons |
| `.claude/skills/jev-question-tuning/` | How to change a question and prove the change helped |
| `.claude/agents/comment-quality-auditor.md` | Subagent for auditing a report against the brief |
| `Identifying good comments POC _ Brief.md` | The original Community team brief |
