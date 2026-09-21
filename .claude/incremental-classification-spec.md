# Incremental classification

Status: **proposed, not built.** Written 2026-09-21. Extends
`.claude/comment-classification-spec.md`; read that first.

## Why

The pipeline is stateless. Every run fetches the whole thread and sends every comment to
Jev. Run it twice on the same article and you pay twice for answers that provably cannot
have changed, because

```
quality_score = f(Jev answers, stance_shares)
```

and the Jev answers depend only on the article body, the comment text and the parent
text — all three immutable once posted. The only time-varying term is
`representativeness`, which reads `stance_shares` over the thread, and that is arithmetic
over answers already held. **No comment ever needs reclassifying.**

That single fact is what keeps this small. It also resolves the question that prompted
the work: comments gaining likes or replies after their first classification do not need
re-scoring, because engagement is not an input to the score — see *Engagement stays out
of scoring* below.

## What changes

One store, and a split in `classify_thread` so the paid half and the free half can run
independently.

```
run(article, since) ->
    1. fetch comments newer than the watermark, minus an overlap window
    2. drop any uuid already in the store          <- prevents double-paying
    3. Jev the remainder, write answers to the store
    4. recompute stance_shares over ALL stored answers for this container
    5. recompute quality_score for ALL comments, in code, no API calls
    6. rank, render, advance the watermark
```

Step 5 is what keeps every comment's score current as the thread's stance mix moves,
without a single extra request. Steps 1–3 are the only ones that cost money, and they
touch only comments never seen before.

## The store

### The uuid set is the cursor, not the timestamp

Comments do not arrive in creation order. `Comment.state` exists because Viafoura holds
comments in moderation: one created at 10:00 can be approved at 10:45, after a 10:30
watermark has moved past it. A timestamp cursor drops that comment permanently and
silently.

So the watermark is an **optimisation for fetching**, and the set of classified uuids is
what provides **correctness**. Fetch generously, deduplicate strictly:

- fetch `since = watermark - OVERLAP` (start at one hour; Viafoura calls are free and
  paginate fast, Jev calls are the ones that cost)
- skip any uuid already stored — this, not the timestamp, is what stops double-paying
- a crash between steps 3 and 6 re-fetches and re-skips; nothing is lost or paid twice

### Shape of a record

Whichever backing store is used, one record per classified comment:

| Field | Why |
| --- | --- |
| `container_uuid` | Which article's thread. |
| `comment_uuid` | The key. Also the handle an editor pastes into the Viafoura UI. |
| `answers` | The raw Jev answer dict. |
| `model` | The version that answered. |
| `created_at` | The comment's own timestamp, ISO 8601, UTC. |
| `classified_at` | When this row was written. |

Plus one watermark per container: `last_seen_at` (the newest comment's own `created_at`,
**not** wall-clock, so a slow run cannot skip comments posted while it was working) and
`last_run_at`.

Answers are stored raw rather than as a score, so re-weighting is a replay over stored
rows and costs nothing. That is the point: **thresholds and weights are tuned against
history without re-billing.** It is also why `model` is on every row — now that
`MODEL = None`, a row answered by a later Jev version is only comparable if you know
which version answered it.

### For the POC: one JSON file per container

`state/<container_uuid>.json`, holding the watermark and a `comment_uuid -> record` map.
Read it at the start of a run, write it once at the end.

```json
{
  "container_uuid": "00000000-0000-4000-8000-...",
  "last_seen_at": "2026-09-21T14:32:00+00:00",
  "last_run_at": "2026-09-21T14:35:12+00:00",
  "classified": {
    "c4f1a9e2-...": {
      "answers": {"personal_experience": 2.1, "...": "..."},
      "model": "jev-1.13.0",
      "created_at": "2026-09-21T14:31:02+00:00",
      "classified_at": "2026-09-21T14:35:09+00:00"
    }
  }
}
```

A file per container rather than one big file, so two articles never contend and a
corrupt file costs one thread rather than all of them. Write to a temporary file in the
same directory and `os.replace` it, which is atomic on POSIX — a crash mid-write then
leaves the previous run's state intact rather than a truncated file.

What this does **not** give you, and why it is fine for now: no concurrent writers (two
runs on the same article would clobber each other — don't do that yet), and the whole map
is loaded into memory (~1 KB per comment, so a 1,000-comment thread is ~1 MB — fine).

### For production: SQLite

Once the pipeline polls many articles in parallel, move to one SQLite file. It is barely
more code and brings concurrent writers, crash safety and partial reads that JSON files
cannot give.

```sql
CREATE TABLE classified (
    container_uuid TEXT NOT NULL,
    comment_uuid   TEXT NOT NULL,
    answers        TEXT NOT NULL,
    model          TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    classified_at  TEXT NOT NULL,
    PRIMARY KEY (container_uuid, comment_uuid)
) WITHOUT ROWID;

CREATE TABLE watermark (
    container_uuid TEXT PRIMARY KEY,
    last_seen_at   TEXT NOT NULL,
    last_run_at    TEXT NOT NULL
);
```

`INSERT OR IGNORE` on `classified` makes step 3 idempotent under concurrent runs on the
same article, which is the property the JSON version lacks.

The migration is a loop over the JSON files, so nothing is lost by starting with them —
which is the reason to start with them.

### Location

Not `output/`, which is disposable and gitignored. A `state/` directory, gitignored
separately, with the path in `Settings` so a deployment can point it elsewhere.

## Engagement stays out of scoring

The brief records that the 2024 Viafoura *Top Comments* trial "failed to identify the
most valuable comments and had a tendency to promote sarcastic comments". It ranked on
engagement, and sarcasm attracts likes — which is item 7 on the brief's sift-out list.
Every quality criterion in the brief is intrinsic to the comment text; the sole
thread-relative one, representativeness, is computed from stance labels rather than
likes.

Likes and reply counts may therefore inform **retrieval** — which comments to look at,
as `RANKED_BY` already does — and must not inform **scoring**. With
`TOP_N_COMMENTS = None` even the retrieval bias disappears.

The brief's one traction hypothesis ("I wonder if genuine suggestions tend to have more
replies?") is a proxy for `proposes_solution`, which Jev is already asked directly.

## What this is not

- Not a scheduler. Something else decides when to call `run`; this only makes a second
  call cheap.
- Not a cache with invalidation. Nothing invalidates, because nothing expires: an answer
  is a pure function of immutable inputs. A Jev version change is the one exception, and
  `model` on each row records it.
- Not multi-article orchestration. The store is keyed by container so it does not
  obstruct it, but parallel polling is a separate discussion.

## Open questions

1. **Deleted and re-moderated comments.** A comment removed after classification stays
   in the store and would still rank. Cheapest fix: honour `Comment.state` on re-fetch
   and mark rows withdrawn.
2. **Article edits.** A rewritten article invalidates every answer against it, since the
   body is in the state. Rare, probably ignorable; worth a note rather than a mechanism.
3. **Store growth.** ~1 KB per comment. A year of Telegraph volume is worth estimating
   before it is a surprise — and it is one of the triggers for the move to SQLite, since
   the JSON version reads the whole map into memory on every run.
4. **Overlap window.** One hour is a guess. The right value is the longest realistic
   moderation delay, which the Community Moderation team can answer.

## Cost

A 1,000-comment thread (~3,200 input tokens each) polled every five minutes for a day,
288 polls: **$0.13 in total**, against **$38.75** if every poll reclassified everything.
The saving is the whole point — it is what makes a five-minute cadence affordable.
