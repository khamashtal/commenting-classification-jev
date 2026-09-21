# Comment classification — The longevity lessons Britain can learn from Singapore

<https://www.telegraph.co.uk/health-fitness/conditions/ageing/longevity-lessons-britain-can-learn-from-singapore/>

| | |
| --- | --- |
| Container | `01a0c2c4-bfd3-7e04-ae83-d9461ca3abaf` |
| Run | 2026-09-21 13:45 UTC |
| Model | `jev-1.13.0` |
| Article body sent | 600 of 1182 words |
| Comments fetched | 5 (top 5 by most_liked) |
| Skipped before Jev | 0 |
| Classified | 5 |
| Excluded by Jev | 2 |
| Flagged for review | 3 |
| Shortlisted | 3 |
| Input tokens | 17,207 |
| Estimated cost | $0.0007 |

## Thread summary

| Stance | Share of classified comments |
| --- | --- |
| supportive | 67% |
| mixed | 33% |

Representativeness in the score below is a comment's stance share, so a comment in the majority camp scores higher. Dissenting comments are ranked lower but never excluded.

Score weights: experience 35%, tone 15%, readability 10%, contribution 15%, standalone 10%, representativeness 15%.

## Shortlist (top 3 by score)

### 1. Score 0.500 — pin or carousel

*21 Sep 10:05 · 110 likes · 9 replies · 23 words · stance: mixed*

> I love Singapore and all this is true. But being significantly richer than Britons and having world class healthcare also helps a lot.

`experience 0.01/3 · relevant 0.53 · tone 1.99/2 · readability 1.68/2 · solution 0.03 · reasoned 0.90 · standalone 0.81 · on-topic 0.96 · sarcasm 0.08 · claims 1.49/2`

**Flagged:** unverified_claim 1.49

### 2. Score 0.431 — pin or carousel

*21 Sep 10:10 · 119 likes · 1 replies · 41 words · stance: supportive*

> Singapore has so much to teach us in every sphere of our lives, but first and foremost, the welfare dependency that is embedded in so much of UK culture must go, we will be a richer and healthier country for it.

`experience 0.00/3 · relevant 0.09 · tone 1.13/2 · readability 1.48/2 · solution 0.21 · reasoned 0.52 · standalone 0.94 · on-topic 0.37 · sarcasm 0.11 · claims 0.74/2`

**Flagged:** on_topic 0.37

### 3. Score 0.401 — pin or carousel

*21 Sep 07:37 · 125 likes · 2 replies · 70 words · stance: supportive*

> Singapore rewards aspiration.
> 
> It doesn’t reward lazy layabouts.
> 
> It protects its borders in a meaningful way.
> 
> Politicians act in the interests of Singapore not for themselves or their political parties.
> 
> They don’t spend beyond their means whilst delivering incredible capital projects on time (to the day) free from the planning red tape and nonsense that chokes off UK growth.
> 
> Work, rest and play rather than laze about, thieve and claim.

`experience 0.00/3 · relevant 0.16 · tone 0.19/2 · readability 1.67/2 · solution 0.08 · reasoned 0.75 · standalone 0.91 · on-topic 0.38 · sarcasm 0.10 · claims 1.45/2`

**Flagged:** unverified_claim 1.45, on_topic 0.38

## Flagged for review

Kept in the shortlist, but a signal is close to its exclusion threshold. These are the cases most worth your judgment.

| Score | Flags | Comment |
| --- | --- | --- |
| 0.500 | unverified_claim 1.49 | I love Singapore and all this is true. But being significantly richer than Britons and having world class healthcare al… |
| 0.431 | on_topic 0.37 | Singapore has so much to teach us in every sphere of our lives, but first and foremost, the welfare dependency that is … |
| 0.401 | unverified_claim 1.45, on_topic 0.38 | Singapore rewards aspiration. It doesn’t reward lazy layabouts. It protects its borders in a meaningful way. Politician… |

## Excluded

Grouped by reason. Scan for anything you would have kept.

| Reason | Comment |
| --- | --- |
| unverified_claim 1.55 (excludes at 1.5) | Singapore -They don't have the worry of 1. Random street violence 2. Terrible Health service 3. Expensive transport and… |
| unverified_claim 1.99 (excludes at 1.5) | 1/ Singapore doesn’t accept illegal migrants and has a zero tolerance policy in this respect, 2/ Singapore doesn’t indu… |

---

Exclusion thresholds: sarcasm ≥ 0.7, personal_attack ≥ 0.7, group_hostility ≥ 0.6, profanity_or_threat ≥ 0.6, unverified_claim ≥ 1.5, on_topic < 0.35. Edit them in `processing/classification.py`; edit the question wording in `processing/questions.py`.
