# How Jev fails, and the numbers

Jev is a System One model: it returns typed decisions and calibrated probabilities, never
text. Source: TypeSafe's own jaggedness page for `jev-1.13`, plus what we have observed on
Telegraph comments.

## Failure modes that matter for this battery

### Literal reading

It answers the question as written, not the one you meant. Scoping words, negations and
implied conditions are taken at face value. State the exact condition in `instructions` and
put boundary cases in `criteria`. Where interpretation is unavoidable, split it into two
literal questions and combine them in code.

### It cannot count or calculate

Characters in a word, occurrences of a term, items in a list. The error grows with the size
of the thing counted. **Everything countable in this project is already done in Python**:
word count, caps ratio, URL detection. Do not add a question that counts.

### Adversarial content

Jev does not treat the state as hostile. Text written to steer its own classification can
move the answer. This bears directly on `sarcasm`, which is the question most likely to be
gamed, and the reason Viafoura's own tool failed.

### Context rot

Accuracy falls as the state grows with material unrelated to the question. This is why
`ARTICLE_MAX_WORDS` caps the article body at 600 words by default. If a question seems to
need more article context, test at two cap sizes rather than assuming bigger is better.

### Contradictory instructions and criteria

A Noul whose `true` maps to "no" performs worse. Treat criteria as an extension of the
instruction and keep both pointing the same way.

### Structural invariants do not hold

- A Noul and an equivalent yes/no Choice return different numbers on the same input.
- A question and its negation do not sum to 1.
- A Choice is *relative* (which option wins), a Noul is *absolute* (can be low for all).

So never carry a threshold tuned on one question type across to another.

### It does not generate text

If you ever need a written explanation of a pick, that is an LLM's job, not Jev's.

## Reading the answers

| Type | Field | Meaning |
| --- | --- | --- |
| Noul | `noul` | Probability the answer is yes. Near 0.5 means genuinely split, **not** "medium". |
| Score | `score` | Probability-weighted position across levels; can land between two. |
| Score | `probabilities` | Per level. Two different distributions can give the same score, so read both. |
| Choice | `choice` | Highest-probability option. |
| Choice / Score | `confidence` | How peaked the distribution is, 0–1. |

Confidence describes the *shape of the answer*, not whether it is correct. A model can be
confidently wrong. Higher confidence after a wording change does not prove the change was
an improvement.

Low confidence on a Score usually means one of three things: the levels overlap for this
input, the question is measuring more than one thing, or the comment does not say enough.

## Numbers

| | |
| --- | --- |
| Price | $0.042 per million input tokens; output tokens free |
| Context | 64k per request; 32k for state plus the longest question |
| Rate limits | 1,200 requests/minute, 250k tokens/second (TypeSafe warn these change without notice) |
| Latency | ~100ms per request |
| Choice options | Up to 255 |
| Score levels | 2 to 10 |
| Observed here | ~3,400 input tokens per comment with a 600-word article; $0.0025 for 20 comments |

Adding a question to an existing request is nearly free: questions are evaluated in
parallel, so latency barely moves and you pay only for the extra question's tokens. Asking
a question you might not need is cheaper than a second round trip.

## Customisation

Jev is not fine-tuned and the same weights serve every account. The only levers are:

1. What goes in `state`.
2. The `instructions` and `criteria` of each question.
3. How you combine the answers in code.

That is why `questions.py` is the file that matters.

## Pinning the version

`MODEL = "jev-1.13.0"` in `workflow.py`, deliberately not `jev-latest`. An alias moves when
TypeSafe ship, and thresholds tuned against one version should not shift underneath you.
When you do upgrade, re-check the thresholds against known comments.
