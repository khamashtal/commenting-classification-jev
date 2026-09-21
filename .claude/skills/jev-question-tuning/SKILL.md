---
name: jev-question-tuning
description: Change a Jev question in the comment-quality battery and prove the change helped. Use when a question is over-firing or under-firing, when adding a question, when exclusion thresholds or score weights need adjusting, or when a classification report disagrees with what a Community editor would pick. Covers the Telegraph comment-pinning POC in src/processing.
compatibility: Requires uv, a TYPESAFE api_key in .env, and network access to api.typesafe.ai
metadata:
  project: jev-ai
  version: "1.0"
---

# Tuning the Jev question battery

The battery in `src/processing/questions.py` decides whether this classifier agrees with
the Community team. The code around it barely matters by comparison. This skill is the
loop for changing a question's wording and finding out whether the change was an
improvement or just a different kind of wrong.

## The loop

1. **Find a comment that is scored wrong.** Usually from a report in `output/`, or from
   the disagreements an audit turned up.
2. **Get a baseline.** Run that comment through the current battery with
   `scripts/try_question.py` and record the number.
3. **Write down what you expect** the corrected value to be, before you edit. This is the
   step people skip, and without it you will accept any change that moves the number.
4. **Edit one question** in `questions.py`.
5. **Re-run the same comment.** Did it move the way you predicted?
6. **Re-run comments that were already right.** A wording change that fixes one comment
   and breaks three is a regression. The script takes several comments at once for exactly
   this reason.
7. **Re-run the pipeline** on a whole article and compare the shortlist.

```bash
# one comment, current battery, all 14 answers
PYTHONPATH=src uv run python .claude/skills/jev-question-tuning/scripts/try_question.py \
  --text "They're so thick. Sadly, I didn't put 10 years of blood, sweat and tears into building my business only to sell it and hand it over."

# just the question you are working on, against several saved comments
PYTHONPATH=src uv run python .claude/skills/jev-question-tuning/scripts/try_question.py \
  --file cases.txt --only profanity_or_threat --only personal_attack

# the full pipeline afterwards
PYTHONPATH=src uv run python src/processing/workflow.py <article URL>
```

## How to write a question that works

Read `references/jev-gotchas.md` before your first edit. The essentials:

- **Jev reads literally.** It answers the question you wrote, not the one you meant. When
  you look at a wrong answer and find yourself explaining what you really meant, that
  explanation is the missing half of the instruction.
- **High always means the property is present.** Never invert a question so its number
  reads the other way; the docs warn that criteria contradicting the instruction score
  worse. Quality questions: high is good. Exclusion questions: high is bad.
  `classification.py` decides what each direction means.
- **One judgment per question.** "Is this angry *and* off topic?" produces a number that
  means nothing. Ask twice and combine in code.
- **Describe situations, not degrees.** For a Score, "Broken feature but a workaround
  exists" gives the model something to match. "Moderately severe" does not.
- **Each Score level is judged on its own.** The model never sees a level's number or its
  neighbours, so "worse than the previous level" means nothing to it.
- **Add `examples` when two options blur.** Examples steer the model hard, but only when
  they resemble real inputs. An unrelated example changes nothing.
- **Never ask it to count.** Word counts, capitals and URLs are already done in
  `classification.py`. Jev cannot count and the documentation says so.

## Structured criteria

When a plain string is not separating two cases, give the criterion an object. Use the
same field names on every option so the model compares like with like. The field names are
yours; `what`, `not_for` and `examples` are the convention in this battery.

```python
criteria=NoulCriteria(
    true={
        "what": "Asks the recipient to disclose a password, PIN or one-time code",
        "examples": ["Reply with your password"],
    },
    false={
        "what": "No sensitive credential is requested",
        "not_for": "A legitimate instruction to reset a credential",
        "examples": ["Reset your password from the settings page"],
    },
)
```

## Choosing the question type

| Type | Use when | Returns |
| --- | --- | --- |
| `Noul` | A clean yes/no where the probability is the signal | `noul`, 0–1 |
| `Score` | A position on a spectrum you can describe in steps | `score`, `probabilities`, `confidence` |
| `Choice` | One of a fixed set with no order between them | `choice`, `probabilities`, `confidence` |

A Noul near 0.5 means the model gives yes and no equal weight. It does **not** mean
"medium". If you want a degree, use a Score with described levels.

## Changing thresholds or weights instead

Often the question is fine and the number around it is wrong. Both live at the top of
`src/processing/classification.py`:

- `EXCLUDE_AT` and `FLAG_AT`: the bands that exclude a comment or send it for review.
- `ON_TOPIC_EXCLUDE_BELOW` / `ON_TOPIC_FLAG_BELOW`: `on_topic` runs the other way, so a
  **low** value is the problem.
- `WEIGHTS`: how the quality score is composed. Each part is normalised to 0–1 first, so
  the weights mean what they say.

Prefer moving a threshold to rewording a question when the model's ordering is right but
its cut-off is not. Thresholds are a number under review; wording changes are riskier.

## What counts as evidence

A wording change is an improvement only if it moves the comments you predicted **and**
leaves the rest alone. Higher confidence on its own proves nothing: the model can be
confidently wrong, and the docs say so explicitly.

The real test is the Community team's pinned-versus-approved spreadsheet. Treat it as a
**ranking** problem, not a classification one: the "approved" column contains good
comments that went unpinned purely for lack of capacity, so a good classifier puts pinned
comments near the top rather than scoring every approved comment low.

## Reference files

- `references/brief-criteria.md` — what the Community team actually asks for, with their
  own examples. Check any new question against this.
- `references/jev-gotchas.md` — how Jev fails, and the numbers (price, limits, context).
