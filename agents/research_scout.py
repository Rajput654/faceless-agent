"""
agents/research_scout.py

Scrapes Reddit + uses Groq LLM to find viral topics for YouTube Shorts.

FIX 1 — Batched topic generation:
  Previously a single LLM call asked for all 10 topics at once with max_tokens=2000.
  On Groq's free tier this frequently hit the output token limit mid-JSON, causing
  json.loads to fail silently. The fallback then triggered, which has only 3 entries
  per niche — these 3 entries were repeated to fill the batch of 10, resulting in
  10 videos with identical concepts.

  Fix: topics are now generated in small batches of 3-4 per LLM call, aggregated
  into the full count. Each batch call uses max_tokens=800, preventing truncation.

FIX 2 — Expanded FALLBACK_TOPICS:
  Each niche now has 10 unique fallback entries so that even when ALL LLM calls
  fail, the 10-video batch contains 10 distinct topics.

FIX 3 — Better error logging:
  The raw LLM response is now logged on parse failure so the actual problem
  (truncated JSON, wrong format, etc.) is visible in the CI logs.
"""
import os
import json
import time
import random
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

try:
    import praw
except ImportError:
    praw = None

try:
    from groq import Groq
except ImportError:
    Groq = None


# ---------------------------------------------------------------------------
# FIX 2: Expanded fallback pools — 10 unique entries per niche
# Previously only 3 entries per niche caused the same topics to repeat.
# ---------------------------------------------------------------------------
FALLBACK_TOPICS = {
    "motivation": [
        {"topic": "The morning routine that changed everything", "hook": "Nobody told you this about 5 AM...", "angle": "Science-backed morning habits", "virality_score": 8.5, "emotion": "inspiration", "source": "fallback"},
        {"topic": "Why most people never achieve their goals", "hook": "Stop doing this if you want success...", "angle": "Psychology of habit formation", "virality_score": 8.0, "emotion": "urgency", "source": "fallback"},
        {"topic": "The 2-minute rule that beats procrastination", "hook": "Nobody told you this about productivity...", "angle": "Atomic habits technique", "virality_score": 7.5, "emotion": "curiosity", "source": "fallback"},
        {"topic": "What millionaires do before 6 AM", "hook": "The day you wake up at 5 AM everything shifts...", "angle": "Millionaire morning habits", "virality_score": 8.3, "emotion": "inspiration", "source": "fallback"},
        {"topic": "The discipline secret nobody talks about", "hook": "Stop waiting for motivation to show up...", "angle": "Discipline over motivation", "virality_score": 8.1, "emotion": "urgency", "source": "fallback"},
        {"topic": "One decision that separates winners from everyone else", "hook": "Most people make this choice wrong every single day...", "angle": "Decision-making psychology", "virality_score": 7.9, "emotion": "curiosity", "source": "fallback"},
        {"topic": "The compound effect nobody teaches you", "hook": "Small actions today create massive results...", "angle": "Compound habit building", "virality_score": 7.8, "emotion": "inspiration", "source": "fallback"},
        {"topic": "Why your comfort zone is making you weaker", "hook": "Every day you stay comfortable you pay a hidden price...", "angle": "Growth mindset science", "virality_score": 8.2, "emotion": "urgency", "source": "fallback"},
        {"topic": "The 1% improvement rule that changes everything", "hook": "Getting 1% better every day sounds small...", "angle": "Marginal gains philosophy", "virality_score": 7.7, "emotion": "inspiration", "source": "fallback"},
        {"topic": "Why you need to fail faster to succeed", "hook": "The most successful people fail more than anyone...", "angle": "Failure as accelerator", "virality_score": 8.0, "emotion": "curiosity", "source": "fallback"},
    ],
    "horror": [
        {"topic": "The house at the end of the street", "hook": "I found something I cannot explain...", "angle": "True events style horror", "virality_score": 8.0, "emotion": "fear", "source": "fallback"},
        {"topic": "The noise at 3 AM", "hook": "The noise started every night at exactly 3:17 AM...", "angle": "Paranormal suspense", "virality_score": 7.8, "emotion": "dread", "source": "fallback"},
        {"topic": "The figure at the end of the hallway", "hook": "This photo was taken 3 seconds before...", "angle": "Found footage horror", "virality_score": 8.2, "emotion": "fear", "source": "fallback"},
        {"topic": "The door that would not stay closed", "hook": "I locked it three times. It was open again.", "angle": "Atmospheric slow burn", "virality_score": 7.9, "emotion": "dread", "source": "fallback"},
        {"topic": "The voicemail from an unknown number", "hook": "I still have the voicemail saved on my phone...", "angle": "Modern paranormal", "virality_score": 8.1, "emotion": "fear", "source": "fallback"},
        {"topic": "What I found in the basement", "hook": "The previous owners never mentioned the basement...", "angle": "Discovery horror", "virality_score": 7.7, "emotion": "dread", "source": "fallback"},
        {"topic": "The security camera footage nobody can explain", "hook": "The timestamp says 2:43 AM. I was asleep.", "angle": "True events style", "virality_score": 8.3, "emotion": "fear", "source": "fallback"},
        {"topic": "The neighbor who knew too much", "hook": "She described my dream before I told anyone about it.", "angle": "Psychological horror", "virality_score": 7.6, "emotion": "dread", "source": "fallback"},
        {"topic": "The text message sent from my dead phone", "hook": "My phone was off. I have the screenshot.", "angle": "Technology horror", "virality_score": 8.0, "emotion": "fear", "source": "fallback"},
        {"topic": "The child who described a life they never lived", "hook": "He was four years old and knew things he should not.", "angle": "Reincarnation horror", "virality_score": 7.8, "emotion": "dread", "source": "fallback"},
    ],
    "reddit_story": [
        {"topic": "I accidentally exposed my boss's secret", "hook": "TIFU, and I cannot believe I am telling you this...", "angle": "Workplace drama", "virality_score": 8.2, "emotion": "shock", "source": "fallback"},
        {"topic": "My neighbor's strange request", "hook": "So I have to tell you what just happened...", "angle": "Neighborhood drama", "virality_score": 7.5, "emotion": "curiosity", "source": "fallback"},
        {"topic": "I found out my best friend had a secret identity", "hook": "Update for everyone who told me to leave: I did...", "angle": "Betrayal revelation", "virality_score": 8.4, "emotion": "shock", "source": "fallback"},
        {"topic": "The coworker who tried to get me fired", "hook": "She smiled at me every morning. I had no idea.", "angle": "Workplace betrayal", "virality_score": 8.0, "emotion": "shock", "source": "fallback"},
        {"topic": "I returned someone's lost wallet and regretted it", "hook": "TIFU by doing the right thing...", "angle": "Good deed gone wrong", "virality_score": 7.8, "emotion": "curiosity", "source": "fallback"},
        {"topic": "My landlord's secret room", "hook": "There was a door in my apartment I could not open.", "angle": "Discovery drama", "virality_score": 8.1, "emotion": "curiosity", "source": "fallback"},
        {"topic": "I overheard a conversation that changed everything", "hook": "I was not supposed to be in that hallway.", "angle": "Accidental witness", "virality_score": 7.9, "emotion": "shock", "source": "fallback"},
        {"topic": "The family dinner that revealed a 20-year lie", "hook": "My aunt said something at Christmas that unraveled everything.", "angle": "Family secret revelation", "virality_score": 8.3, "emotion": "shock", "source": "fallback"},
        {"topic": "My roommate's bizarre nighttime routine", "hook": "I set up a camera after the third week.", "angle": "Strange discovery", "virality_score": 7.7, "emotion": "curiosity", "source": "fallback"},
        {"topic": "The job interview question that got me the job", "hook": "They asked me something completely unexpected.", "angle": "Career win story", "virality_score": 7.6, "emotion": "curiosity", "source": "fallback"},
    ],
    "brainrot": [
        {"topic": "Sigma grindset facts that broke the internet", "hook": "Okay this is going to break your brain but...", "angle": "Absurdist Gen Z humor", "virality_score": 8.5, "emotion": "chaos", "source": "fallback"},
        {"topic": "The most unhinged life advice", "hook": "Brain.exe has stopped working after this fact:", "angle": "Internet culture chaos", "virality_score": 8.0, "emotion": "amusement", "source": "fallback"},
        {"topic": "Facts so cursed you cannot unhear them", "hook": "The most cursed fact you will learn today:", "angle": "Disturbing facts comedy", "virality_score": 8.3, "emotion": "chaos", "source": "fallback"},
        {"topic": "Things that exist and should not", "hook": "We need to talk about something deeply wrong...", "angle": "Absurdist observation", "virality_score": 7.9, "emotion": "amusement", "source": "fallback"},
        {"topic": "The algorithm knows something we do not", "hook": "I asked the AI one question and it got weird fast...", "angle": "AI conspiracy comedy", "virality_score": 8.2, "emotion": "chaos", "source": "fallback"},
        {"topic": "Reality is stranger than any meme", "hook": "Scientists published this and nobody noticed...", "angle": "Real life is brainrot", "virality_score": 7.8, "emotion": "amusement", "source": "fallback"},
        {"topic": "The simulation is glitching again", "hook": "Three separate people reported this same thing yesterday.", "angle": "Glitch in the matrix comedy", "virality_score": 8.1, "emotion": "chaos", "source": "fallback"},
        {"topic": "Gen Z discovered something boomers hid from us", "hook": "They did not want us to know this one thing.", "angle": "Generational chaos content", "virality_score": 7.7, "emotion": "amusement", "source": "fallback"},
        {"topic": "The most unhinged Wikipedia rabbit hole", "hook": "I clicked one link and ended up in a dark place...", "angle": "Internet archaeology", "virality_score": 8.0, "emotion": "chaos", "source": "fallback"},
        {"topic": "Lore drops that hit different at 3 AM", "hook": "This information should not be free.", "angle": "Midnight brainrot", "virality_score": 7.6, "emotion": "amusement", "source": "fallback"},
    ],
    "finance": [
        {"topic": "The savings account mistake costing you thousands", "hook": "The number one money mistake killing your wealth:", "angle": "High-yield savings comparison", "virality_score": 8.5, "emotion": "urgency", "source": "fallback"},
        {"topic": "Why you should never pay off your mortgage early", "hook": "Nobody taught you this about money in school...", "angle": "Counterintuitive finance", "virality_score": 8.0, "emotion": "curiosity", "source": "fallback"},
        {"topic": "The credit score hack your bank hides from you", "hook": "If you earn under $50,000 do this immediately:", "angle": "Credit optimization", "virality_score": 8.3, "emotion": "urgency", "source": "fallback"},
        {"topic": "Why the S&P 500 beats 95% of professional investors", "hook": "The number one money mistake killing your wealth:", "angle": "Index fund investing", "virality_score": 7.9, "emotion": "curiosity", "source": "fallback"},
        {"topic": "The 50-30-20 budget rule is outdated", "hook": "Nobody taught you this about budgeting in school...", "angle": "Modern budgeting methods", "virality_score": 7.7, "emotion": "urgency", "source": "fallback"},
        {"topic": "How inflation is silently destroying your savings", "hook": "Your money is losing value while you sleep.", "angle": "Inflation education", "virality_score": 8.1, "emotion": "urgency", "source": "fallback"},
        {"topic": "The Roth IRA trick most people discover too late", "hook": "If you are under 40 you need to hear this now.", "angle": "Retirement account strategy", "virality_score": 8.2, "emotion": "urgency", "source": "fallback"},
        {"topic": "Why rich people rent and poor people buy", "hook": "Everything you were told about homeownership was wrong.", "angle": "Contrarian real estate take", "virality_score": 7.8, "emotion": "curiosity", "source": "fallback"},
        {"topic": "The tax deduction that 90% of workers miss", "hook": "The IRS is not going to remind you about this.", "angle": "Tax optimization", "virality_score": 8.4, "emotion": "urgency", "source": "fallback"},
        {"topic": "How to turn $100 a month into $1 million", "hook": "The math on this will make you uncomfortable.", "angle": "Compound interest illustration", "virality_score": 8.0, "emotion": "curiosity", "source": "fallback"},
    ],
}


class ResearchScoutAgent:
    def __init__(self, config):
        self.config = config
        self.niche = os.environ.get("NICHE", config.get("video", {}).get("niche", "motivation"))
        self.groq_key = os.environ.get("GROQ_API_KEY", "")
        self.reddit_id = os.environ.get("REDDIT_CLIENT_ID", "")
        self.reddit_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")

    def _get_niche(self) -> str:
        """Always read niche fresh — may have been updated after __init__."""
        return os.environ.get("NICHE", self.config.get("video", {}).get("niche", "motivation"))

    def _get_reddit_posts(self):
        niche = self._get_niche()
        if not praw or not self.reddit_id or not self.reddit_secret:
            logger.warning("Reddit credentials missing or praw not installed. Skipping Reddit scrape.")
            return []

        try:
            reddit = praw.Reddit(
                client_id=self.reddit_id,
                client_secret=self.reddit_secret,
                user_agent=os.environ.get("REDDIT_USER_AGENT", "faceless-agent/1.0"),
            )
            subreddits = self.config.get("reddit", {}).get("subreddits", {}).get(niche, ["GetMotivated"])
            min_score = self.config.get("reddit", {}).get("min_score", 1000)
            post_limit = self.config.get("reddit", {}).get("post_limit", 50)

            posts = []
            for sub_name in subreddits[:3]:
                try:
                    sub = reddit.subreddit(sub_name)
                    for post in sub.hot(limit=post_limit // len(subreddits[:3])):
                        if post.score >= min_score and not post.stickied:
                            posts.append({
                                "title": post.title,
                                "score": post.score,
                                "url": f"https://reddit.com{post.permalink}",
                                "subreddit": sub_name,
                                "text": post.selftext[:500] if post.selftext else "",
                            })
                except Exception as e:
                    logger.warning(f"Failed to scrape r/{sub_name}: {e}")

            logger.info(f"Scraped {len(posts)} Reddit posts")
            return posts
        except Exception as e:
            logger.error(f"Reddit scraping failed: {e}")
            return []

    # FIX 1: Generate a small batch of topics (3-4) per LLM call.
    # Previously a single call for 10 topics caused JSON truncation on Groq's
    # free tier, silently triggering the fallback with only 3 unique topics.
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _generate_single_batch(self, posts: list, count: int, client, niche: str) -> list:
        """
        Generate exactly `count` topics in one LLM call.
        Kept small (3-4) to avoid hitting Groq output token limits mid-JSON.
        """
        posts_text = (
            "\n".join([f"- {p['title']} (score: {p['score']})" for p in posts[:20]])
            if posts else "No Reddit posts available — generate original ideas."
        )

        model = self.config.get("llm", {}).get("primary_model", "llama-3.3-70b-versatile")

        prompt = f"""Generate exactly {count} unique YouTube Shorts topics for the '{niche}' niche.

Trending posts for inspiration:
{posts_text}

Return ONLY a valid JSON array of exactly {count} objects. No markdown, no explanation, no preamble:
[
  {{
    "topic": "specific topic title",
    "hook": "opening line that grabs attention in first 2 seconds",
    "angle": "unique angle or perspective",
    "virality_score": 8.5,
    "emotion": "primary emotion (inspiration/fear/shock/curiosity/urgency/amusement/chaos/dread)",
    "source": "reddit"
  }}
]

IMPORTANT: Return ONLY the JSON array. Start with [ and end with ]. No other text."""

        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=self.config.get("llm", {}).get("temperature", 0.8),
            max_tokens=800,  # tight limit prevents truncation for small batches
        )

        raw = response.choices[0].message.content.strip()

        # FIX 3: log raw response on parse failure for CI debugging
        try:
            # Strip markdown fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            # Find the JSON array bounds in case there's surrounding text
            start = raw.find("[")
            end = raw.rfind("]")
            if start != -1 and end != -1 and end > start:
                raw = raw[start:end + 1]

            batch = json.loads(raw)

            if not isinstance(batch, list):
                raise ValueError(f"Expected list, got {type(batch).__name__}: {raw[:200]}")

            if len(batch) == 0:
                raise ValueError("Empty list returned from LLM")

            return batch

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(
                f"JSON parse failed for batch of {count}: {e}\n"
                f"Raw response (first 500 chars): {raw[:500]}"
            )
            raise  # let @retry handle it

    def _generate_topics_with_llm(self, posts: list) -> list | None:
        """
        FIX 1: Generate topics in small batches of 3-4 to prevent JSON truncation.

        Previously this made ONE call for all daily_count topics. At max_tokens=2000
        on Groq's free tier the JSON was frequently cut off mid-array, causing
        json.loads to fail. The retry then exhausted attempts, the fallback triggered,
        and all 10 videos used the same 3 fallback concepts.

        Now: generate in batches of BATCH_SIZE, aggregate, return full list.
        """
        if not Groq or not self.groq_key:
            logger.warning("Groq not available. Using fallback topics.")
            return None

        niche = self._get_niche()
        client = Groq(api_key=self.groq_key)
        daily_count = self.config.get("video", {}).get("daily_count", 10)
        BATCH_SIZE = 3  # safe size that never truncates at max_tokens=800

        all_topics = []
        batch_num = 0

        while len(all_topics) < daily_count:
            remaining = daily_count - len(all_topics)
            this_batch = min(BATCH_SIZE, remaining)
            batch_num += 1

            logger.info(
                f"Topic generation batch {batch_num}: "
                f"requesting {this_batch} topics "
                f"({len(all_topics)}/{daily_count} so far) | niche={niche}"
            )

            try:
                batch = self._generate_single_batch(posts, this_batch, client, niche)
                # Validate each entry has required keys
                valid = []
                for item in batch:
                    if isinstance(item, dict) and "topic" in item and "hook" in item:
                        # Ensure niche-appropriate emotion defaults
                        if not item.get("emotion"):
                            item["emotion"] = "curiosity"
                        valid.append(item)
                    else:
                        logger.warning(f"Skipping malformed topic entry: {item}")

                all_topics.extend(valid[:this_batch])
                logger.success(
                    f"Batch {batch_num}: got {len(valid)} valid topics. "
                    f"Total: {len(all_topics)}/{daily_count}"
                )

            except Exception as e:
                logger.warning(
                    f"Batch {batch_num} failed after retries: {e}. "
                    f"Filling remaining {remaining} slots with fallbacks."
                )
                # Fill remaining slots from fallback pool rather than returning None
                fallback_pool = FALLBACK_TOPICS.get(niche, FALLBACK_TOPICS["motivation"])
                needed = daily_count - len(all_topics)
                # Shuffle fallbacks so repeated use varies order
                shuffled = fallback_pool.copy()
                random.shuffle(shuffled)
                extended = (shuffled * ((needed // len(shuffled)) + 1))[:needed]
                all_topics.extend(extended)
                break

            # Small delay between batch calls to respect Groq rate limits
            if len(all_topics) < daily_count:
                time.sleep(3)

        return all_topics if all_topics else None

    def run(self, *args, **kwargs):
        niche = self._get_niche()
        logger.info(f"ResearchScoutAgent starting for niche: {niche}")

        posts = self._get_reddit_posts()

        try:
            topics = self._generate_topics_with_llm(posts)
            if topics and len(topics) > 0:
                # Deduplicate by topic title before returning
                seen_topics = set()
                unique_topics = []
                for t in topics:
                    key = t.get("topic", "").lower().strip()
                    if key and key not in seen_topics:
                        seen_topics.add(key)
                        unique_topics.append(t)
                    elif not key:
                        unique_topics.append(t)

                logger.success(
                    f"Generated {len(unique_topics)} unique topics via LLM "
                    f"(deduplicated from {len(topics)})"
                )
                return unique_topics
        except Exception as e:
            logger.warning(f"LLM topic generation failed entirely: {e}. Using fallback topics.")

        # Full fallback — shuffle so repeated runs vary order
        fallback = FALLBACK_TOPICS.get(niche, FALLBACK_TOPICS["motivation"])
        daily_count = self.config.get("video", {}).get("daily_count", 10)

        shuffled_fallback = fallback.copy()
        random.shuffle(shuffled_fallback)

        if len(shuffled_fallback) >= daily_count:
            topics = shuffled_fallback[:daily_count]
        else:
            topics = (shuffled_fallback * ((daily_count // len(shuffled_fallback)) + 1))[:daily_count]

        logger.info(f"Using {len(topics)} fallback topics for niche='{niche}'")
        return topics
