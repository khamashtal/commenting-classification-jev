"""The Jev question battery for comment quality.

One request per comment carries every question below; Jev evaluates them in parallel and
returns a typed answer for each. Question ids are ours alone and are never sent to the
model, so each ``instructions`` is worded to stand on its own.

**Phrasing rule.** Every question is written so a *high* value means the named property is
**present**. For quality questions that makes high good; for exclusion questions it makes
high bad. Never invert a question to make the number read the other way — the docs warn
that criteria contradicting the instruction score worse — the scoring code in
``classification.py`` decides what each direction means.

This module is the tuning surface. Expect to edit the wording here far more often than any
other file, which is why it holds nothing but the battery and the state it expects.
"""

from __future__ import annotations

from typing import Any

from typesafe_sdk import Choice, Noul, NoulCriteria, Score

# Article fields are referenced from instructions by backticked path, e.g. `article.body`.
STANCE_OPTIONS: tuple[str, ...] = ("supportive", "critical", "mixed", "no_position")


def build_state(
    *,
    headline: str,
    standfirst: str,
    body: str,
    comment_text: str,
    parent_text: str | None,
) -> dict[str, Any]:
    """Assemble the ``state`` for one comment.

    The keys here are the paths the instructions point at, so this function and the
    battery below must change together.
    """
    return {
        "article": {
            "headline": headline,
            "standfirst": standfirst,
            "body": body,
        },
        "comment": {
            "text": comment_text,
            # Present only for replies. Named explicitly so questions can point at it.
            "parent_text": parent_text or "",
        },
    }


# --------------------------------------------------------------------- quality signals

PERSONAL_EXPERIENCE = Score(
    instructions={
        "question": (
            "How much of `comment.text` is a first-hand account of something the writer, "
            "or someone close to them, actually lived through?"
        ),
        "focus": (
            "Using the words I, me, my, we or our is not enough on its own. Judge whether "
            "a real situation or event is described. Mentions of a wife, husband, son, "
            "daughter or other relation often signal a real account."
        ),
    },
    criteria=[
        {
            "what": (
                "Opinion, reaction or argument only. Nothing about the writer's own life, "
                "even if it uses I, me, we or our."
            ),
            "examples": [
                "I can't stand this government.",
                "We should scrap the whole scheme.",
            ],
        },
        {
            "what": (
                "Mentions the writer's own job, family, age or location in passing, but "
                "describes no event or situation they lived through."
            ),
            "examples": [
                "As a retired GP I think this is nonsense.",
                "My wife would agree with every word of this.",
            ],
        },
        {
            "what": (
                "Describes a specific situation or event the writer or a relation lived "
                "through, with at least one concrete detail such as when, where, or what "
                "happened."
            ),
            "examples": [
                "My son's school shut for three days last winter because the boiler "
                "failed and nobody could get the part.",
            ],
        },
        {
            "what": (
                "An extended first-hand account with several concrete details and some "
                "reflection on what it meant."
            ),
            "examples": [
                "We sold our house in Edinburgh in 2019. Offers were binding within a "
                "week, the survey was done before listing, and the whole thing completed "
                "in six weeks. Moving to England two years later, we lost a buyer the day "
                "before exchange. The Scottish system is not perfect, but it removes that "
                "gamble.",
            ],
        },
    ],
)

EXPERIENCE_RELEVANT = Noul(
    instructions=(
        "Does the personal experience described in `comment.text`, if there is one, "
        "relate to the subject of `article`?"
    ),
    criteria=NoulCriteria(
        true="The experience bears on the article's subject or a point it raises",
        false=(
            "No personal experience is described, or the experience is about something "
            "unrelated to the article"
        ),
    ),
)

PROPOSES_SOLUTION = Noul(
    instructions={
        "question": (
            "Does `comment.text` seriously propose a specific course of action, policy or "
            "fix for a problem discussed in `article`?"
        ),
        "focus": "A genuine suggestion, not a sarcastic or throwaway one.",
    },
    criteria=NoulCriteria(
        true={
            "what": (
                "Names a concrete action and means it: adopting another country's system, "
                "changing a specific rule, or a practical workaround"
            ),
            "examples": [
                "Why doesn't England adopt the Scottish system where offers are binding? "
                "It would stop buyers pulling out the day before exchange.",
            ],
        },
        false={
            "what": (
                "Only complains, asks a question, or proposes something plainly sarcastic "
                "or unserious"
            ),
            "examples": [
                "Something must be done.",
                "Just ban everything, that'll fix it.",
            ],
        },
    ),
)

REASONED_ARGUMENT = Noul(
    instructions=(
        "Does `comment.text` give reasons or evidence for its view, rather than only "
        "asserting it?"
    ),
    criteria=NoulCriteria(
        true="Explains why, with at least one reason, example or consequence",
        false="States a conclusion or a reaction with no supporting reason",
    ),
)

TONE = Score(
    instructions={
        "question": "How measured is the tone of `comment.text`?",
        "focus": (
            "Judge the emotional register, not whether the view is right and not how "
            "strongly it is held."
        ),
    },
    criteria=[
        {
            "what": (
                "Angry, contemptuous or ranting. Insults, sneering, or sustained outrage."
            ),
            "examples": [
                "Absolute clowns, every last one of them, and anyone who voted for them "
                "is an idiot.",
            ],
        },
        {
            "what": (
                "Emphatic or irritated but civil. Strong words or an exclamation, no "
                "insults."
            ),
            "examples": ["This is a disgrace and the minister should resign!"],
        },
        {
            "what": (
                "Calm and reflective. States a view or an experience with reasons and no "
                "hostility."
            ),
            "examples": [
                "I disagree with the minister here. The scheme worked in our area "
                "precisely because it was voluntary.",
            ],
        },
    ],
)

READABILITY = Score(
    instructions={
        "question": "How easy is `comment.text` to read as written?",
        "focus": (
            "Judge spelling, punctuation, capitalisation and sentence length. Ignore "
            "whether you agree with it."
        ),
    },
    criteria=[
        {
            "what": (
                "Hard to read. Run-on text, many spelling or grammar errors, missing "
                "punctuation or sentence capitals throughout."
            ),
        },
        {
            "what": (
                "Readable with effort. Several errors, or long unbroken sentences, but "
                "the meaning is clear."
            ),
        },
        {
            "what": (
                "Clean. Proper sentences, punctuation and capitals, with at most one or "
                "two slips."
            ),
        },
    ],
)

STANDALONE = Noul(
    instructions=(
        "Can a reader understand `comment.text` without seeing `comment.parent_text` or "
        "any other comment?"
    ),
    criteria=NoulCriteria(
        true={
            "what": (
                "Makes sense on its own. Any reference to other people is "
                "self-explanatory."
            ),
        },
        false={
            "what": "Depends on the parent comment or another comment to make sense",
            "examples": ["Exactly this.", "You're wrong about the second point."],
        },
    ),
)

STANCE = Choice(
    instructions={
        "question": (
            "What position does `comment.text` take on the main subject of `article`?"
        ),
        "focus": (
            "Judge the comment's view of the article's subject, not its view of the "
            "Telegraph or of other commenters."
        ),
    },
    criteria={
        "supportive": (
            "Supports the article's argument, or is favourable to the person, policy or "
            "development it reports"
        ),
        "critical": (
            "Opposes the article's argument, or is hostile to the person, policy or "
            "development it reports"
        ),
        "mixed": "Agrees with part and disagrees with part, or weighs both sides",
        "no_position": (
            "Takes no position: a question, a joke, or a comment about something else"
        ),
    },
)

# ------------------------------------------------------------------- exclusion signals

SARCASM = Noul(
    instructions={
        "question": (
            "Is `comment.text` sarcastic, meaning it says the opposite of what the writer "
            "actually means?"
        ),
        "focus": (
            "On this site, apparent praise of the article, the Telegraph, its journalists "
            "or the moderators is usually sarcastic. So are comparisons to the Guardian "
            "and phrases such as 'cultural enrichment' or 'the joys of multiculturalism'."
        ),
    },
    criteria=NoulCriteria(
        true={
            "what": (
                "Says something positive or agreeable that the writer clearly does not "
                "mean, or mocks through fake praise"
            ),
            "examples": [
                "Top journalism, worth every penny!",
                "Ah, the joys of multiculturalism.",
                "Thanks to the moderators for their tireless work.",
                "I'm sure the Guardian will cover this with perfect balance.",
            ],
        },
        false={
            "what": (
                "Means what it says, whether the view expressed is positive, negative or "
                "neutral"
            ),
            "examples": [
                "This is a well-argued piece and I agree with most of it.",
                "The policy has failed and should be scrapped.",
            ],
        },
    ),
)

ON_TOPIC = Noul(
    instructions="Does `comment.text` address the subject of `article`?",
    criteria=NoulCriteria(
        true="Engages with the article's subject, or with a point the article raises",
        false=(
            "About something else: another news story, the Telegraph itself, the comment "
            "section, the moderators, or other commenters"
        ),
    ),
)

UNVERIFIED_CLAIM = Score(
    instructions={
        "question": (
            "How much does `comment.text` rely on specific factual claims that do not "
            "appear in `article` and are presented as fact?"
        ),
        "focus": (
            "Judge whether the claim is present in the article, not whether the claim is "
            "true. Opinions, predictions and the writer's own experience are not factual "
            "claims."
        ),
    },
    criteria=[
        {
            "what": (
                "No outside factual claims. Opinion, prediction, the writer's own "
                "experience, or facts that are in the article."
            ),
            "examples": [
                "This will end badly for the Chancellor.",
                "My council took six months to fix the same problem.",
            ],
        },
        {
            "what": (
                "General or widely known facts that a reader would not need to look up: "
                "common knowledge, well-known cultural or historical references."
            ),
            "examples": [
                "Petrol is far more expensive than it was a few years ago.",
                "John Candy and Eugene Levy were both on SCTV.",
            ],
        },
        {
            "what": (
                "Specific checkable claims not found in the article: a statistic, a sum "
                "of money, a date, a named study, or a quotation attributed to someone."
            ),
            "examples": [
                "Crime in Sweden rose 40 per cent after 2015.",
                "When the Chernobyl children came to the UK they were given tinned "
                "tomatoes to remove their toxic levels.",
            ],
        },
    ],
)

PERSONAL_ATTACK = Noul(
    instructions=(
        "Does `comment.text` insult, demean or mock a specific person, including the "
        "article's author, a named public figure, or another commenter?"
    ),
    criteria=NoulCriteria(
        true=(
            "Name-calling, ridicule of appearance, intelligence or character, or abuse "
            "aimed at an individual or at other readers"
        ),
        false=(
            "Criticises actions, decisions or arguments without abusing the person who "
            "made them"
        ),
    ),
)

GROUP_HOSTILITY = Noul(
    instructions=(
        "Does `comment.text` express hostility or contempt towards a group of people "
        "because of their nationality, ethnicity, religion, sex, sexuality, disability or "
        "age?"
    ),
    criteria=NoulCriteria(
        true=(
            "Generalises negatively about such a group, or wishes them harm or exclusion"
        ),
        false=(
            "Discusses policy on immigration, religion or similar without contempt for "
            "the people themselves"
        ),
    ),
)

PROFANITY_OR_THREAT = Noul(
    instructions=(
        "Does `comment.text` contain swearing, slurs, threats, or wishes of harm?"
    ),
    criteria=NoulCriteria(
        true="Contains a swear word, a slur, a threat, or a wish that harm comes to someone",
        false="Contains none of those, however strongly worded it is",
    ),
)


# --------------------------------------------------------------------------- the battery

BATTERY: dict[str, Noul | Choice | Score] = {
    # quality
    "personal_experience": PERSONAL_EXPERIENCE,
    "experience_relevant": EXPERIENCE_RELEVANT,
    "proposes_solution": PROPOSES_SOLUTION,
    "reasoned_argument": REASONED_ARGUMENT,
    "tone": TONE,
    "readability": READABILITY,
    "standalone": STANDALONE,
    "stance": STANCE,
    # exclusion
    "sarcasm": SARCASM,
    "on_topic": ON_TOPIC,
    "unverified_claim": UNVERIFIED_CLAIM,
    "personal_attack": PERSONAL_ATTACK,
    "group_hostility": GROUP_HOSTILITY,
    "profanity_or_threat": PROFANITY_OR_THREAT,
}

# Ids by answer shape, so the reader in classification.py never has to guess.
NOUL_IDS: tuple[str, ...] = tuple(
    qid for qid, q in BATTERY.items() if isinstance(q, Noul)
)
SCORE_IDS: tuple[str, ...] = tuple(
    qid for qid, q in BATTERY.items() if isinstance(q, Score)
)
CHOICE_IDS: tuple[str, ...] = tuple(
    qid for qid, q in BATTERY.items() if isinstance(q, Choice)
)


def top_level(question_id: str) -> int:
    """The highest level number of a Score question, used to normalise it to 0–1."""
    question = BATTERY[question_id]
    if not isinstance(question, Score):
        raise TypeError(f"{question_id} is not a Score question")
    return len(question.criteria) - 1
