"""
agents/music_director.py

FIXED v6:

BUG FIX B14 — MUSIC DOESN'T MATCH VIDEO CONTENT:
  Previously music was chosen based only on niche + emotion (both are the
  SAME for all 10 videos in a batch). The actual script — its specific topic,
  title keywords, hook language, and extracted key_facts — was completely
  ignored. A video about "morning routines" and a video about "beating
  procrastination" both got "uplifting motivational background" as query.

  Fix: _build_content_queries() extracts meaningful keywords from the
  script's title, topic, hook, and Pass-1 extractor output. It builds
  content-specific primary queries (e.g. "morning routine uplifting",
  "procrastination discipline tense") before falling back to the generic
  niche/emotion queries. The music now thematically fits what is being said.

BUG FIX B12 (propagation) — SAME TRACK FOR EVERY VIDEO IN A BATCH:
  MusicMCPServer now accepts a `seed` parameter. This agent derives a
  deterministic but unique seed from hash(video_id) and passes it on every
  call. Seed rotates the CDN URL list so each of the 10 videos in a batch
  tries a different track first.

PRESERVED: All v5 fixes (unique temp paths, ambient fallback integrity).
"""
import os
import re
import shutil
from loguru import logger
from mcp_servers.music_server import MusicMCPServer


NICHE_MUSIC_QUERIES = {
    "motivation": [
        "uplifting motivational background",
        "epic inspiring background",
        "upbeat positive background",
        "motivational corporate background",
        "inspiring background instrumental",
    ],
    "horror": [
        "dark ambient horror",
        "suspenseful scary background",
        "creepy atmospheric",
        "dark tension background",
        "horror ambient dark",
    ],
    "reddit_story": [
        "calm storytelling background",
        "narrative background music",
        "soft piano background",
        "calm ambient background",
        "calm background instrumental",
    ],
    "brainrot": [
        "chaotic energetic electronic",
        "upbeat fast background",
        "energetic electronic background",
        "fast upbeat background",
        "chaotic fast electronic",
    ],
    "finance": [
        "corporate professional background",
        "calm business background",
        "professional ambient instrumental",
        "corporate calm background",
        "clean minimal background",
    ],
}

EMOTION_MUSIC_BASE = {
    "inspiration": "uplifting inspiring",
    "urgency":     "tense urgent pulsing",
    "fear":        "dark eerie horror",
    "dread":       "dark slow ominous",
    "shock":       "dramatic tense",
    "curiosity":   "mysterious thoughtful",
    "amusement":   "playful upbeat quirky",
    "chaos":       "chaotic energetic punchy",
    "default":     "calm background",
}

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "this", "that", "is", "was", "are", "were", "has",
    "have", "had", "how", "why", "what", "when", "where", "who", "if",
    "you", "your", "my", "our", "we", "they", "it", "be", "been", "will",
    "would", "could", "should", "do", "did", "not", "no", "so", "then",
    "now", "here", "just", "still", "even", "most", "some", "all", "more",
    "about", "ever", "never", "stop", "start", "make", "get", "got",
    "told", "tell", "know", "thing", "things", "every", "each", "both",
}


class MusicDirectorAgent:
    def __init__(self, config):
        self.config = config
        self.music_server = MusicMCPServer()
        self.music_config = config.get("music", {})

    def _extract_content_words(self, script: dict, max_words: int = 4) -> list:
        sources = [
            script.get("title", ""),
            script.get("topic_brief", {}).get("topic", ""),
            script.get("hook", ""),
        ]
        extracted = script.get("_extracted", {})
        if extracted.get("core_mystery"):
            sources.append(extracted["core_mystery"])
        if extracted.get("emotional_trigger"):
            sources.append(extracted["emotional_trigger"])

        seen  = set()
        words = []
        for text in sources:
            clean = re.sub(r"[^\w\s]", "", text.lower())
            for word in clean.split():
                if word not in _STOPWORDS and len(word) > 3 and word not in seen:
                    seen.add(word)
                    words.append(word)
                if len(words) >= max_words:
                    return words
        return words

    def _build_content_queries(self, script: dict, niche: str, emotion: str) -> list:
        """
        Build a prioritized list of music search queries that reflect
        the SPECIFIC content of this script, not just the niche/emotion bucket.

        Priority:
          1. content_words + emotion_base  (most specific)
          2. content_words alone
          3. emotion_base alone
          4. niche fallback queries        (least specific)
        """
        queries       = []
        emotion_base  = EMOTION_MUSIC_BASE.get(emotion, EMOTION_MUSIC_BASE["default"])
        content_words = self._extract_content_words(script)

        if content_words:
            content_str = " ".join(content_words[:3])
            queries.append(f"{content_str} {emotion_base} background")
            queries.append(f"{content_str} background instrumental")

        queries.append(f"{emotion_base} background")

        seen = set(queries)
        for q in NICHE_MUSIC_QUERIES.get(niche, ["calm background instrumental"]):
            if q not in seen:
                queries.append(q)
                seen.add(q)

        logger.debug(
            f"Music queries for '{script.get('title', '')[:40]}': "
            + " | ".join(f'"{q}"' for q in queries[:3])
        )
        return queries

    def run(self, script: dict, video_id: str, output_dir: str = "/tmp", *args, **kwargs):
        logger.info(f"MusicDirectorAgent fetching music for video: {video_id}")

        niche    = os.environ.get("NICHE", self.config.get("video", {}).get("niche", "motivation"))
        emotion  = script.get("emotion", "inspiration")
        duration = self.music_config.get("duration_seconds", 60)

        final_music_path = f"{output_dir}/{video_id}_music.mp3"

        # Stable unique seed per video — portable across Python versions/processes
        seed = sum(ord(c) for c in video_id) % 997

        queries = self._build_content_queries(script, niche, emotion)

        logger.info(
            f"Music: niche={niche} emotion={emotion} seed={seed} | "
            f"{len(queries)} queries | topic='{script.get('title', '')[:40]}'"
        )

        ambient_fallback_path = None
        attempt_temp_paths    = []

        try:
            for i, query in enumerate(queries):
                attempt_path = f"{output_dir}/{video_id}_music_attempt_{i}.mp3"
                attempt_temp_paths.append(attempt_path)

                logger.debug(f"Music query {i+1}/{len(queries)}: '{query}'")
                try:
                    result = self.music_server.call(
                        "fetch_music",
                        query=query,
                        output_path=attempt_path,
                        duration_seconds=duration,
                        seed=seed,
                    )

                    if not result.get("success"):
                        continue

                    music_path_result = result.get("music_path")
                    source = result.get("source", "unknown")

                    if not music_path_result or not os.path.exists(music_path_result):
                        continue

                    file_size = os.path.getsize(music_path_result)
                    if file_size < 10_000:
                        continue

                    if source not in ("generated_ambient", "none"):
                        if music_path_result != final_music_path:
                            shutil.move(music_path_result, final_music_path)
                        logger.success(
                            f"Music fetched ✅  source={source} | "
                            f"query='{query}' | seed={seed} | "
                            f"{file_size // 1024} KB"
                        )
                        return {
                            "success":          True,
                            "music_path":       final_music_path,
                            "title":            result.get("title", query),
                            "source":           source,
                            "volume_reduction": self.music_config.get("volume_reduction", 0.12),
                        }
                    else:
                        if ambient_fallback_path is None:
                            ambient_fallback_path = music_path_result
                        continue

                except Exception as e:
                    logger.debug(f"Music query '{query}' raised exception: {e}")
                    continue

            # All CDN queries exhausted — use ambient fallback
            if ambient_fallback_path and os.path.exists(ambient_fallback_path):
                size = os.path.getsize(ambient_fallback_path)
                if size > 500:
                    if ambient_fallback_path != final_music_path:
                        shutil.move(ambient_fallback_path, final_music_path)
                        ambient_fallback_path = final_music_path
                    logger.warning(
                        f"No CDN music found. Using synthetic ambient tone "
                        f"({size // 1024} KB) as background music."
                    )
                    return {
                        "success":          True,
                        "music_path":       final_music_path,
                        "title":            "ambient",
                        "source":           "generated_ambient",
                        "volume_reduction": self.music_config.get("volume_reduction", 0.12),
                    }

            # Last-resort direct attempt
            logger.warning("All music queries failed. Making final ambient generation attempt.")
            final_result = self.music_server.call(
                "fetch_music",
                query=queries[0] if queries else "calm background",
                output_path=final_music_path,
                duration_seconds=duration,
                seed=seed,
            )
            final_music = final_result.get("music_path")
            if not final_music or not os.path.exists(final_music):
                logger.warning("Music generation completely failed. Video will be voice-only.")
                final_music = None

            return {
                "success":          True,
                "music_path":       final_music,
                "title":            final_result.get("title", "background music"),
                "source":           final_result.get("source", "none"),
                "volume_reduction": self.music_config.get("volume_reduction", 0.12),
            }

        finally:
            for tmp_path in attempt_temp_paths:
                if (
                    tmp_path
                    and tmp_path != final_music_path
                    and tmp_path != ambient_fallback_path
                    and os.path.exists(tmp_path)
                ):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
