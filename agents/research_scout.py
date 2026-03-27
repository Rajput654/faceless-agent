"""
agents/research_scout.py
Scrapes Reddit + uses Groq LLM to find viral topics for YouTube Shorts.
"""
import os
import json
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


FALLBACK_TOPICS = {
    "motivation": [
        {"topic": "The morning routine that changed everything", "hook": "Nobody told you this about 5 AM...", "angle": "Science-backed morning habits", "virality_score": 8.5, "emotion": "inspiration", "source": "fallback"},
        {"topic": "Why most people never achieve their goals", "hook": "Stop doing this if you want success...", "angle": "Psychology of habit formation", "virality_score": 8.0, "emotion": "urgency", "source": "fallback"},
        {"topic": "The 2-minute rule that beats procrastination", "hook": "Nobody told you this about productivity...", "angle": "Atomic habits technique", "virality_score": 7.5, "emotion": "curiosity", "source": "fallback"},
    ],
    "horror": [
        {"topic": "The house at the end of the street", "hook": "I found something I cannot explain...", "angle": "True events style horror", "virality_score": 8.0, "emotion": "fear", "source": "fallback"},
        {"topic": "The noise at 3 AM", "hook": "The noise started every night at exactly 3:17 AM...", "angle": "Paranormal suspense", "virality_score": 7.8, "emotion": "dread", "source": "fallback"},
    ],
    "reddit_story": [
        {"topic": "I accidentally exposed my boss's secret", "hook": "TIFU, and I cannot believe I am telling you this...", "angle": "Workplace drama", "virality_score": 8.2, "emotion": "shock", "source": "fallback"},
        {"topic": "My neighbor's strange request", "hook": "So I have to tell you what just happened...", "angle": "Neighborhood drama", "virality_score": 7.5, "emotion": "curiosity", "source": "fallback"},
    ],
    "brainrot": [
        {"topic": "Sigma grindset facts that broke the internet", "hook": "Okay this is going to break your brain but...", "angle": "Absurdist Gen Z humor", "virality_score": 8.5, "emotion": "chaos", "source": "fallback"},
        {"topic": "The most unhinged life advice", "hook": "Brain.exe has stopped working after this fact:", "angle": "Internet culture chaos", "virality_score": 8.0, "emotion": "amusement", "source": "fallback"},
    ],
    "finance": [
        {"topic": "The savings account mistake costing you thousands", "hook": "The number one money mistake killing your wealth:", "angle": "High-yield savings comparison", "virality_score": 8.5, "emotion": "urgency", "source": "fallback"},
        {"topic": "Why you should never pay off your mortgage early", "hook": "Nobody taught you this about money in school...", "angle": "Counterintuitive finance", "virality_score": 8.0, "emotion": "curiosity", "source": "fallback"},
    ],
}


class ResearchScoutAgent:
    def __init__(self, config):
        self.config = config
        self.niche = os.environ.get("NICHE", config.get("video", {}).get("niche", "motivation"))
        self.groq_key = os.environ.get("GROQ_API_KEY", "")
        self.reddit_id = os.environ.get("REDDIT_CLIENT_ID", "")
        self.reddit_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")

    def _get_reddit_posts(self):
        if not praw or not self.reddit_id or not self.reddit_secret:
            logger.warning("Reddit credentials missing or praw not installed. Skipping Reddit scrape.")
            return []

        try:
            reddit = praw.Reddit(
                client_id=self.reddit_id,
                client_secret=self.reddit_secret,
                user_agent=os.environ.get("REDDIT_USER_AGENT", "faceless-agent/1.0"),
            )
            subreddits = self.config.get("reddit", {}).get("subreddits", {}).get(self.niche, ["GetMotivated"])
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _generate_topics_with_llm(self, posts):
        if not Groq or not self.groq_key:
            logger.warning("Groq not available. Using fallback topics.")
            return None

        client = Groq(api_key=self.groq_key)
        daily_count = self.config.get("video", {}).get("daily_count", 10)

        posts_text = "\n".join([f"- {p['title']} (score: {p['score']})" for p in posts[:20]]) if posts else "No Reddit posts available."

        prompt = f"""You are a viral YouTube Shorts topic researcher specializing in {self.niche} content.

Analyze these trending Reddit posts and generate {daily_count} unique YouTube Shorts topics:
{posts_text}

For each topic, respond with ONLY a JSON array (no markdown, no explanation):
[
  {{
    "topic": "specific topic title",
    "hook": "opening line that grabs attention in first 2 seconds",
    "angle": "unique angle or perspective",
    "virality_score": 8.5,
    "emotion": "primary emotion (inspiration/fear/shock/curiosity/urgency/amusement)",
    "source": "reddit"
  }}
]

Generate exactly {daily_count} topics. Make them specific, emotionally resonant, and optimized for {self.niche} niche. Return ONLY the JSON array."""

        model = self.config.get("llm", {}).get("primary_model", "llama-3.3-70b-versatile")
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=self.config.get("llm", {}).get("temperature", 0.8),
            max_tokens=self.config.get("llm", {}).get("max_tokens", 2000),
        )

        raw = response.choices[0].message.content.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        topics = json.loads(raw)
        return topics

    def run(self, *args, **kwargs):
        logger.info(f"ResearchScoutAgent starting for niche: {self.niche}")

        posts = self._get_reddit_posts()

        try:
            topics = self._generate_topics_with_llm(posts)
            if topics and len(topics) > 0:
                logger.success(f"Generated {len(topics)} topics via LLM")
                return topics
        except Exception as e:
            logger.warning(f"LLM topic generation failed: {e}. Using fallback topics.")

        # Fallback
        fallback = FALLBACK_TOPICS.get(self.niche, FALLBACK_TOPICS["motivation"])
        daily_count = self.config.get("video", {}).get("daily_count", 10)
        topics = (fallback * ((daily_count // len(fallback)) + 1))[:daily_count]
        logger.info(f"Using {len(topics)} fallback topics")
        return topics
