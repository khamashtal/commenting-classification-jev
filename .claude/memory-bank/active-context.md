# Active context

_Last updated: 21 September 2026_

## What this project is

A proof of concept for The Telegraph's Community team: automatically find reader comments
worth **pinning** to the top of a thread or putting in a **carousel**. The team currently
pins around 500 comments a week and fills up to 100 carousel slots a day by hand, and is
capped by how fast people can read long threads. The brief is `Identifying good comments
POC _ Brief.md` (Philippa Law, updated Feb 2026).

The approach: score each comment with **TypeSafe's Jev**, a decision model that returns
calibrated probabilities rather than text, then combine those numbers in ordinary Python.
Jev is not an LLM and does not generate prose.

## Where things stand

The end-to-end pipeline **works**. `PYTHONPATH=src uv run python src/processing/workflow.py`
fetches an article and its comments, classifies each comment against 14 Jev questions, and
writes a Markdown report to `output/`.

The strongest evidence it does something real: on the capital gains tax article, the
comment the Community team had **actually pinned** came out ranked first with a score of
0.946, against 0.454 for second place. The classifier reached the same conclusion a
Community editor did, without being told which comment was pinned.

## What to work on next

**1. Fix two over-firing questions.** This is the most valuable next change.

| Question | Problem | Evidence |
| --- | --- | --- |
| `profanity_or_threat` | Catching insults rather than profanity. Scored 0.91 on a comment containing no swearing, excluding a personal-experience comment that may have been pinnable. | "They're so thick. Sadly, I didn't put 10 years of blood, sweat and tears into building my business..." |
| `unverified_claim` | Fires very often. 4 exclusions in a 17-comment run, 2 of 5 in another. Some are right per the brief; others are ordinary assertions. | Comments making general claims about Singapore's policies |

Use the `jev-question-tuning` skill. It has the workflow and a script for testing a
wording change against one comment in about a second.

**2. Decide whether tone should be a floor, not a weight.** A comment scoring 0.00 on
tone (angry, contemptuous) still reached the shortlist at rank four, because tone carries
only 15% of the weight and cannot sink a comment alone. If the Community team thinks an
angry comment should never be shortlisted, that needs a floor rather than a weight.

**3. Validate against the spreadsheet.** The Community team has a sample of pinned
comments versus merely-approved ones. Running the battery over it is how weights and
thresholds stop being guesses. Two caveats recorded at the time: the "approved" column is
noisy, because good comments went unpinned purely for lack of capacity, so treat it as a
**ranking** problem rather than a classification one; and the sheet has no article URLs,
which five of the questions need, so ask for those to be added.

## Open questions for Luis

These are listed in §13 of `.claude/comment-classification-spec.md` and are still open:

1. **Question wording** (§6 of the spec). This decides whether the classifier is any good.
2. Weights (§8.3) and thresholds (§8.2).
3. Whether to send the article body at all, or start with headline plus standfirst.
4. Whether to delete the now-unused empty `src/config.toml`.

## Deliberately not built

| Deferred | Why |
| --- | --- |
| Audience segments (Advocate / Traditionalist / Curator) | Luis's call. Needs written segment definitions with example comments before Jev can classify against them. |
| Commenter usernames | The Viafoura MCP server returns an anonymous `actor_uuid` only. The brief needs usernames, so this needs another source. |
| Viafoura's moderation word list | Not obtained. It would become a code-side hard filter, not a Jev question. |
| Batching several comments per Jev request | One comment per request is the accurate baseline. Batch only after measuring against it. |
| FastAPI service | The code is shaped for it (see `decision-log.md`), but nothing is built. |
| Auto-pinning or publishing | Explicitly out of scope in the brief. |
