import os

from dotenv import load_dotenv
from typesafe_sdk import Choice, TypeSafeClient

load_dotenv(override=True)

api_key = os.environ.get("api_key")
if not api_key:
    raise RuntimeError("api_key is not set; add it to the .env file")

client = TypeSafeClient(api_key=api_key)


persona_reflections = {
    "demographer": "Here are high-level demographic observations and socioeconomic reflections for the 'Alienated Agnostics' value segment based on the aggregate data:\n\n* **Gender Parity:** The segment exhibits a remarkably balanced gender split (52% female, 48% male), indicating that the shared values or attitudes defining this group are not driven by gender-specific demographic imbalances.\n* **Mature, Aging Demographic Structure:** The profile skews noticeably older, with 43% of the segment aged 55 and above (peaking in the 65\u201374 cohort at 18%). However, it maintains a consistent baseline across younger adult cohorts (13%\u201315% per decade bracket), reflecting a multi-generational reach anchored by older adults.\n* **Exceptional Human Capital Accumulation:** Educational attainment is significantly higher than national baselines; 48% hold tertiary qualifications (undergraduate degrees or higher), including a striking 21% with postgraduate or doctoral-level credentials. Only 3% report having no formal qualifications.\n* **Dominance of White-Collar Social Grades:** Socioeconomic stratification is heavily skewed toward middle and upper-middle-class households. Over 63% of primary earners work in higher, intermediate, or junior managerial and professional roles (Social Grades A, B, and C1), pointing to high structural status and employment in knowledge-based industries.\n* **Low Material Precarity:** Despite the label \"Alienated,\" the structural indicators reveal low levels of acute economic distress; only 3% are unemployed and 4% rely solely on state pensions. This suggests that alienation in this group is likely socio-cultural or political rather than driven by direct economic marginalization.\n* **Regional Anchor in Economic Cores:** Nearly 30% of the population resides in London and the South East, aligning the segment with the UK\u2019s primary economic engines and high-cost living areas, though there remains a broad baseline presence across northern and midland regions.\n* **Bimodal Settlement Pattern:** Living conditions reflect a spatial split between urban and exurban/rural environments: 29% reside in large cities or their suburbs, while 37% live in small towns or rural areas outside towns. This suggests diverse physical living environments despite a unified socioeconomic profile.\n* **Intellectual and Knowledge-Worker Profile:** The convergence of advanced qualifications, managerial/professional roles, and sub-segment behavioral traits (e.g., Knowledge Seekers, Wealth Optimisers) characterizes this segment as a highly educated, analytical cohort whose disaffection may stem from critical institutional critique rather than systemic disenfranchisement.",
    "psychologist": 'Here are the expert psychological observations and reflections on the \'Alienated Agnostics\' segment based on the survey data:\n\n* **Pervasive Moral and Normative Detachment:** Across almost all core moral foundations (Care, Fairness, Loyalty, Authority, Sanctity), this group consistently exhibits an exceptionally high rate of neutral responses ("Neither agree nor disagree" ranging from 42% to 59%). This suggests deep normative ambivalence, psychological disengagement, or affective burnout regarding abstract moral principles.\n* **High Threat Perception and Existential Anxiety:** Despite their general normative indifference, an overwhelming 88% believe the world is becoming increasingly dangerous, and a majority (54%) view their local area as unsafe. This reveals a pervasive background state of hyper-vigilance and perceived vulnerability.\n* **Social Atomization and Low Interpersonal Trust:** A majority (54%) harbor generalized interpersonal cynicism, agreeing that "most people cannot be trusted." Combined with low commitment to institutional buffers (e.g., moderate-to-neutral stances on national pride and family loyalty), individuals in this segment appear psychologically isolated without strong relational or community safety nets.\n* **Reactive Need for Environmental Order:** While not deeply dogmatic, a majority support functional safety measures such as censorship (59%) and stiffer legal sentences (58%). This reflects a pragmatic, self-protective desire for baseline external control and social predictability to manage their heightened threat perception, rather than a moralistic desire to punish.\n* **Hedged Internal Locus of Control:** On questions of meritocracy and agency, responses cluster around moderate mid-points with a slight tilt toward hard work and personal responsibility. They possess a cautiously internal locus of control, believing individual effort matters, while remaining implicitly aware that circumstances can limit success.\n* **Ambivalence Toward Authority and Social Norms:** Their attitudes toward authority figures and social conventions are characterized by passive compliance rather than active reverence or rebellion. They lean slightly toward instilling basic manners and respect in children, yet remain hesitant to mandate absolute obedience or suppression of critical thinking.\n* **Bounded Punitiveness:** Despite supporting harsher prison sentences to maintain order, a majority (57%) reject the extreme measure of the death penalty. This indicates an underlying psychological line against absolute institutional overreach or irreversible harm, aligning with their moderate empathy baseline.\n* **Emotional Distancing as a Defense Mechanism:** The neutral stance on empathy-related statements (such as compassion for suffering) paired with high threat sensitivity suggests the use of emotional withdrawal as a psychological coping mechanism. Disengaging emotionally may protect them from feeling overwhelmed by a world they perceive as volatile and untrustworthy.',
    "political_scientist": 'Based on the provided aggregate survey data for the \'Alienated Agnostics\' (Cluster 0), here are expert political science observations regarding their ideological framework, civic participation, and sociopolitical identity:\n\n* **Duty-Driven Participation Plagued by Institutional Alienation:** Despite expressing profound distrust in political actors\u201472% believe politicians do not care about people like them\u2014the cluster exhibits high turnout in major elections (78% voted in the 2024 GE; 73% in Brexit). This reflects a strong normative sense of civic duty that operates independently of political trust or perceived internal efficacy.\n* **Hyper-Dealigned and Fragmented Electoral Behavior:** Partisan and Brexit identities are weakly held, with very few identifying strongly with any political party. This lack of affective partisan alignment manifests in a highly fragmented 2024 vote split primarily between Labour (33%), Conservatives (25%), and Reform UK (12%), marking this segment as quintessential swing-voter territory.\n* **Strong Center-Periphery and Anti-Metropolitan Grievances:** A core driver of this group\'s alienation is geographic and political centralisation. Over two-thirds agree that too many decisions are made in London (68%) and that Londoners live in a detached bubble (64%), pointing to a potent anti-metropolitan resentment that cuts across standard left-right lines.\n* **Economic Insecurity and Class Discontent:** The cohort displays a clear economic grievance profile: 65% believe there is "one law for the rich and one for the poor," 61% feel left behind by broader economic growth, and 54% view corporate management as inherently predatory toward workers. \n* **Pragmatic, Center-Left Economic Preferences:** While distrustful of system mechanics, their policy preferences lean moderately social-democratic rather than radically anti-capitalist. There is a pluralistic consensus favoring income redistribution (45% agree vs. 25% disagree), alongside widespread skepticism about whether ordinary working people receive a fair share of national wealth.\n* **Cultural Conservatism on Salient "Culture War" Cleavages:** On sociocultural issues, the group skews moderately traditionalist. A majority favors moving past imperial history rather than dwelling on historic wrongs (62%), believes public discourse is overly sensitive regarding race (52%), and feels British citizens are subordinated to immigrants in policy prioritization (48%).\n* **Passive Civic Engagement Profile:** Mobilization patterns are overwhelmingly conventional, local, and low-barrier (77% donate to charity, 48% sign petitions, 34% volunteer locally). Disruptive or expressive forms of political activism\u2014such as protesting (11%), donating to political parties (18%), or sharing political content on social media (22%)\u2014are largely rejected.',
    "behavioral_economist": "Here are expert observations and reflections on the behavioral economics, media consumption, and financial priorities of the 'Alienated Agnostics' segment (Cluster 0):\n\n*   **Low Intertemporal Discounting and Durable Goods Preference:** A striking 69% of this segment agrees that it is better to spend more upfront on quality items than to purchase cheap alternatives. This demonstrates a low discount rate for future utility, prioritizing durability, functional longevity, and reduced replacement costs over immediate cost-saving.\n*   **Intergenerational Altruism Drives Financial Utility:** Long-term financial motivation is significantly anchored in bequest motives (64% agree on leaving a financial legacy) rather than pure self-regarding consumption. However, a slight intention-action gap exists, as formal financial planning/budgeting lags behind bequest desires (54% actively plan), suggesting moderate present bias in financial execution.\n*   **High Utility from Process and Experiential Consumption:** Utility is heavily derived from effort-intensive and experiential activities rather than instant gratification. This is evidenced by high engagement in cooking from scratch (69%) and cultural travel (64%), reflecting a consumption function that values self-efficacy, health capital, and experiential learning.\n*   **Pragmatic, Low-Friction Information Acquisition:** Media consumption is strictly utilitarian. The majority favor efficient, low-search-cost formats\u2014such as quick daily checks (31%) or morning/evening summaries (28%)\u2014focusing on national news, international affairs, and key facts rather than opinion pieces or commentator feeds.\n*   **Rejection of Algorithmic and Social Echo Chambers:** This cluster actively avoids high-conflict, algorithmically driven news feeds (only 20% use X/Twitter weekly, 7% follow individual journalists, and under 15% value news aligned with their existing political views). They prefer linear, institutionally validated broadcast news (Live TV 52%, BBC Online 46%).\n*   **Inelastic Demand for Differentiated Niche Publications:** While general willingness to pay for digital news sites is low (53% unlikely to pay), price elasticity drops significantly for specialized or high-identity publications. Subscribers show a high propensity to convert on niche, curated, or opinionated print/digital bundles (e.g., high subscription capture in *The Telegraph*, *Private Eye*, and *The Spectator* relative to standard broadsheets).\n*   **High \"Need for Cognition\" in Leisure Choices:** Leisure preferences lean heavily toward low-friction intellectual stimulation. Over 60% engage in puzzle gaming (crosswords, Sudoku, Wordle) on a regular basis, and 66% explicitly seek out entertainment that assumes intellectual engagement, pointing to a strong cognitive utility component in passive time use.\n*   **Utility Messaging over Social Broadcast:** Digital communication is leveraged primarily for functional, close-network coordination (WhatsApp at 63% and Facebook at 58%) rather than broadcast-style status signaling or public discourse (LinkedIn at 15%, Threads at 4%, Substack at 2%).",
}

likert_question = Choice(
    instructions=(
        "Analyze the provided aggregate population data for the 'Alienated Agnostics'. "
        "Instead of predicting what a single individual would say, evaluate what percentage of "
        "this specific population cohort will select each option based on their mixed feelings "
        "about institutional distrust versus alternative media sources."
    ),
    criteria={
        "strongly_agree": "A small fraction who completely rely on alternative channels for all their news.",
        "agree": "A solid portion who lean into social media explicitly to bypass traditional media narratives.",
        "slightly_agree": "A large segment who are deeply cynical of all media, but acknowledge social media has raw variance.",
        "neutral": "The largest group who feel indifferent, uninvested, or believe social platforms and legacy media are equally flawed.",
        "slightly_disagree": "A segment beginning to feel that algorithms are just creating alternative filter bubbles.",
        "disagree": "A group who explicitly feel social platforms actively manipulate and restrict their worldview.",
        "strongly_disagree": "The extreme fraction who view social networks as toxic, weaponised echo chambers.",
    },
)

response = client.system_one(
    state=persona_reflections,
    questions={
        "political_efficacy_survey": likert_question,
    },
)

# 1. Maintain a strict, ordered list of your keys to preserve the Likert flow
likert_order = [
    "strongly_agree",
    "agree",
    "slightly_agree",
    "neutral",
    "slightly_disagree",
    "disagree",
    "strongly_disagree",
]

# 2. Map the programmatic keys directly to the clean display strings you want
display_labels = {
    "strongly_agree": "Strongly agree",
    "agree": "Agree",
    "slightly_agree": "Slightly agree",
    "neutral": "Neutral",
    "slightly_disagree": "Slightly disagree",
    "disagree": "Disagree",
    "strongly_disagree": "Strongly disagree",
}

# 3. Print the final results in perfect sequential order
survey_answer = response.answers["political_efficacy_survey"]

print(f"Predicted Top Choice: {display_labels[survey_answer.choice]}")
print(f"Model Confidence: {survey_answer.confidence:.4f}\n")

print("--- Predicted Choice Probability Distribution ---")
for key in likert_order:
    # Safely fetch the probability from Jev's output map using the ordered key
    probability = survey_answer.probabilities.get(key, 0.0)
    print(f"{display_labels[key]}: {probability * 100:.2f}%")
