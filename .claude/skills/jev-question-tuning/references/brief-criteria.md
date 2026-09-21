# What the Community team actually wants

Condensed from `Identifying good comments POC _ Brief.md` (Philippa Law, updated Feb
2026). Check any question wording against this. The examples are the team's own.

## The one difference between pinning and carousels

A pinned comment **must be a top-level post**; replies cannot be pinned. Carousel comments
may be replies, though top-level is preferred. Otherwise treat them as one category.

## What they skim for, in order

### 1. Personal experience and anecdote — preferred above all others

> I/me/my/we/us/our are **necessary but not sufficient**. Most such comments are opinion
> ("I can't stand…" / "We should get rid of…"). Relations are a good indicator: wife,
> husband, son, daughter.

This is the single most important signal, and it is why `personal_experience` carries the
largest weight and is multiplied by `experience_relevant`.

### 2. Representative of the thread's opinion

Comments should reflect the overriding opinion, or the spread of opinions, in that
comment section. **Dissenting views can be pinned if they are likely to generate
constructive discussion, but only alongside more commonly expressed views.** Hence
representativeness lowers a dissenting comment's rank without excluding it.

### 3. Suggested solutions

> "It's not fair that house buyers can pull out all the way to exchange. Why doesn't
> England adopt the same system as Scotland? It's much better because…"

Beware sarcasm here. The team wondered whether genuine suggestions attract more replies.

### 4. Measured tone

All caps or multiple typos count against. Some pinned comments contain a few words in
caps; a comment that is *entirely* caps would not be pinned.

AI analysis of their sample found pinned comments use more sentences and more quotations
(narrative, reflective), while non-pinned use more ALL CAPS and punctuation (emphatic,
reactive).

### 5. Word count

| | |
| --- | --- |
| Under ~15 words | Lacks substance |
| 20–100 words | Good |
| Over 100 words | Acceptable with a paragraph break before or around the 100-word mark |
| Longest pinned in their sample | 282 words |
| Median pinned vs approved | 45 words vs 25 words |

Personal-experience comments run longer than opinion comments. Handled in code, not by
Jev.

### 6. Readability

Short sentences with punctuation, capitals at the start of sentences. One or two typos are
fine; multiple typos usually mean a poorer comment. Their sample showed pinned comments
contain fewer typos, measured as uncapitalised sentences, missing spaces and repeated
characters.

## What they actively sift out

### 7. Sarcasm

The team's own examples:

- Apparent praise of articles or the Telegraph: "top journalism, worth every penny!"
- Comparisons to the Guardian
- References to "cultural enrichment" or "joys of multiculturalism"
- Thanking the moderators for their work

**This is the hard one.** Viafoura's own Top Comments tool failed precisely because it
promoted sarcastic comments, and it is why that product was rejected.

### 8. Anything against the community guidelines

Even if not yet flagged or removed. Viafoura hold a word list used for automated
detection; it has not been obtained, and when it is it becomes a code-side hard filter,
not a Jev question.

### 9. Claims not in the article, presented as fact

> "When the Chernobyl kids came over for post radiation help to the uk they were giving
> them tinned Tomatoes to help remove their Toxic levels"

> Avoid any stats or historical points that we would have to look up to check; these are
> common in war threads.

Note the nuance that forced `unverified_claim` to become a 3-level Score: a pinned comment
in their sample lists SCTV cast members, which is a factual claim but common knowledge. A
binary question would have wrongly excluded it.

### 10. URLs

Excluded unless on the telegraph.co.uk domain, because the team cannot check linked pages
or guarantee they stay the same. Handled in code.

## Their objectives, for context on why this matters

- Publish comment carousels faster on fast-moving stories.
- Publish more carousels within articles.
- Better quality on Your Say, the homepage and the app.
- Pin comments systematically across the site, so good reader comments appear on almost
  any article.

The constraint is human capacity, not desire: they already pin ~500 a week and fill up to
100 carousel slots a day by hand, and long threads of 1,000+ comments on politics stories
and liveblogs are where it breaks down.

## Also wanted, not yet built

**Diverse perspectives.** Their commenters skew to the "Advocate" audience segment, and
they want to surface "Traditionalist" and "Curator" voices. Deferred pending written
segment definitions with example comments.

**Output fields.** The brief asks for the poster's **username**, the comment text, a link
to the article, a link to the comment, and whether it is an original post or a reply. The
Viafoura MCP server returns an anonymous `actor_uuid` only, so usernames need another
source.
