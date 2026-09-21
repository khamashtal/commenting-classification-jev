# Identifying good comments

**Philippa Law 4 March 2025 (updated Feb 2026\)**

&nbsp;

# What problems are we trying to solve?

&nbsp;

The Community team is charged with raising the profile of our readers, showcasing their best contributions, and making sure their views are heard throughout our journalism. They currently include **up to 100 readers’ comments a day in carousels** on the homepage, [Your Say](https://www.telegraph.co.uk/your-say/) and on the app. They also **pin around 500 good quality comments a week** to the top of threads, to set a positive tone for the conversation.

&nbsp;

**Increase volume:** We wish to substantially ramp up the number of pinned comments and carousels on the website and app. However, we cannot do this through manual processes alone. Our capacity is limited by our manpower.

&nbsp;

**Reduce time:** Sifting through comment threads is currently a time-consuming, manual process, particularly on long threads (1000+ comments), which are common on politics stories and liveblogs.

&nbsp;

**Identify diverse perspectives:** Our commenters tend to fall into the ‘Advocate’ segment of the audience. We would like to be able to uncover good comments from our ‘Traditionalist’ and ‘Curator’ segments so we can better represent them in carousels, pinning and stories.&nbsp;

### Previous approach

&nbsp;

In 2024, we trialled Viafoura’s ‘Top Comments’ tool, which automatically identifies quality comments and publishes them in a prominent position in the comments section. This tool did not work for us as it failed to identify the most valuable comments and had a tendency to promote sarcastic comments. We continue to use it as a backend tool, which is moderately useful.

&nbsp;

# What are our objectives?

&nbsp;

* To publish comment carousels more quickly on fast-moving stories.  
* To publish more comment carousels within articles.  
* To publish the best possible quality comment carousels, particularly on the Your Say page, homepage and app.  
* To pin comments systematically and comprehensively across the Telegraph, so the best of readers’ comments are visible on almost any article a user chooses to read.

&nbsp;

# What is our proposal?

&nbsp;

We propose to build a tool that automatically identifies comments that would be suitable for a) carousels and b) pinning, in real time, on the fly. The requirements for these differ, as per below.

&nbsp;

Community Editors would continue to manually select comments, pin comments and build comment carousels, but would use the tool to speed up the process of identifying high quality comments.

&nbsp;

We are open to how the tool might work in terms of UI. For carousels, we suggest that the Editor might enter the URL of a comments section to generate a selection of high quality comments to work with. For pinning comments, it may be more complicated, as we would like to find a way to enable Editors to see which recent articles do not yet have pinned comments and generate an up to date selection of the best to choose from. For both purposes, we would need to integrate with existing tools (Viafoura, Particles).

&nbsp;

# What are the product requirements?

&nbsp;

* Automatically identify comments that would be suitable for carousels and pinning.  
* Analyse the comments in real time, including the most recent comments.  
* Return a sensible number of comments to choose from, more than enough for a carousel/pinning, but not so many that sifting these comments becomes a burden in itself.  
* Return the username of the poster, the comment text, a direct link to the article and a direct link to the comment. Indicate whether the comment is an original post or a reply.

### What is out of scope for the POC?

&nbsp;

* Automatically generating draft carousels or publishing carousels.  
* Automatically pinning comments or integrating the tool directly with Viafoura.

&nbsp;

### What’s the difference between pinned comments and carousels?

There’s only one difference these days: A pinned comment must be a top-level post; replies cannot be pinned. Comments featured in carousels can be replies to posts (although top-level posts are preferred).

&nbsp;

While we historically edited comments to paste manually into carousels, we no longer do this. Pinned comments and carousel comments are now very similar and we suggest treating them as one category for simplicity.

&nbsp;

**What comments should the tool identify?**

&nbsp;

For both pins and carousels, comments that centre on personal experience are preferred above all others. **When Editors are looking for comments to pin, this is what they’re skim-reading for:**

&nbsp;

1. Personal experience and anecdotes

→ I/me/my/we/us/our are necessary but not sufficient indicators of personal experience. Most such comments are opinion (“I can’t stand…” / “We should get rid of…”).

→ Relations are a good indicator (wife, husband, son, daughter etc).

&nbsp;

Where personal experience is not available on a particular article, we would look for comments that are opinion. Things to bear in mind with opinion comments:

&nbsp;

2. Represent the overriding opinion, or spread of opinions, in that comment section

→ Comments should be representative of the comments section on that article. Dissenting views can be pinned if they are likely to generate constructive discussion, but only alongside more commonly expressed views.

&nbsp;

3. Suggested solutions

→ e.g. “It’s not fair that house buyers can pull out all the way to exchange. Why doesn’t England adopt the same system as Scotland? It’s much better because…”&nbsp;

→ We like to see suggested solutions to an issue, but beware of sarcasm. I wonder if genuine suggestions tend to have more replies as other readers debate them?

&nbsp;

4. Measured tones

→ All caps or multiple typos are counter-indicators of measured tones. Some pinned comments include a few words written in all caps, but we wouldn’t expect to pin a comment that was *all* all caps.

→ According to AI analysis of a [sample of pinned comments vs approved comments](https://docs.google.com/spreadsheets/d/1-8kPiY47zs89JRg78uqiVhYL0wzBoWOdM0EpdrJXN30/edit?usp=sharing), pinned comments use slightly more sentences per comment on average and more quotations, indicating a more narrative or reflective style. Non-pinned comments use more ALL CAPS and punctuation, suggesting a more emphatic or reactive tone.

&nbsp;

Other indicators of quality:

&nbsp;

5. Word count

→ Good quality comments tend to be not too long, not too short. Very short (perhaps under 15 words) suggests a lack of substance. Around 20-100 words is good. There may be outliers for exceptional personal stories that run longer.

→ Personal experience comments are usually longer than opinion/reaction comments.

→ Longer comments are more likely to be included if they have a paragraph break before/around the 100 word mark, for readability.

→ AI analysis of a [sample of pinned comments vs approved comments](https://docs.google.com/spreadsheets/d/1-8kPiY47zs89JRg78uqiVhYL0wzBoWOdM0EpdrJXN30/edit?usp=sharing) showed that pinned comments are generally longer than non-pinned comments (median 45 words vs median 25 words). The longest pinned comment in the sample ran to 282 words.

&nbsp;

6. Readability

→ Short sentences with punctuation. Capitals at the start of a sentence.

→ One or two typos or spelling mistakes are acceptable. Multiple typos are usually associated with a poorer quality comment.

→ AI analysis of a [sample of pinned comments vs approved comments](https://docs.google.com/spreadsheets/d/1-8kPiY47zs89JRg78uqiVhYL0wzBoWOdM0EpdrJXN30/edit?usp=sharing) showed that pinned comments contained fewer typos (uncapitalised sentences, missing spaces, repeated characters) than non-pinned comments.

&nbsp;

**We should actively sift out:**

&nbsp;

7. Sarcasm

→ most commonly seen in apparent ‘praise’ of articles/Telegraph (“top journalism, worth every penny\!” etc)

→ also comparisons to the Guardian, references to “cultural enrichment” or “joys of multiculturalism”, thanking the moderators for their work

&nbsp;

8. Anything that contravenes our [community guidelines](https://www.telegraph.co.uk/about-us/community-guidelines/), even if it hasn’t been flagged or removed (yet)

→ You might also find it helpful to speak to Viafoura (Jeannie Doherty [jeannie@viafoura.com](mailto:jeannie@viafoura.com)) to obtain a copy of the word list used to automatically identify potentially problematic content.

&nbsp;

9. Claims not present in the original article, presented as facts

→ e.g. “When the Chernobyl kids came over for post radiation help to the uk they were giving them tinned Tomatoes to help remove their Toxic levels”

→ Avoid any stats or historical points that we would have to look up to check; these are common in war threads.

&nbsp;

10. URLs

→ We don’t check the content of linked pages and can’t guarantee they will remain the same, so comments containing URLs should be excluded, unless they’re on the [telegraph.co.uk](http://telegraph.co.uk) domain.

&nbsp;

**Contact details**

For more details, you’re welcome to contact the Community Editorial team ([editorialcommunity@telegraph.co.uk](mailto:editorialcommunity@telegraph.co.uk)) led by Rachel Obordo, or the Community Moderation team ([moderation@telegraph.co.uk](mailto:moderation@telegraph.co.uk)) led by Will Whittington.

&nbsp;