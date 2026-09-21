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
- Batching comments per Jev request.
- FastAPI service. The code is shaped for it but nothing is written.
- Any test suite. There are no unit tests; verification so far is live runs.

## Where the documentation lives

| Document | Purpose |
| --- | --- |
| `CLAUDE.md` | Project baseline: tooling, the `.env` rule, layout, conventions |
| `.claude/comment-classification-spec.md` | The full spec: pipeline, questions, thresholds, weights, open decisions |
| `.claude/memory-bank/` | This folder: state, decisions, lessons |
| `.claude/skills/jev-question-tuning/` | How to change a question and prove the change helped |
| `.claude/agents/comment-quality-auditor.md` | Subagent for auditing a report against the brief |
| `Identifying good comments POC _ Brief.md` | The original Community team brief |
