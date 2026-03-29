"""
agents/music_director.py

FIXED v3 — Two bugs patched:

BUG FIX A — last_silence_result REFERENCED BEFORE ASSIGNMENT:
  In the query loop, when music_server returns source="generated_silence",
  the code did `last_silence_result = result` then `continue`. But if ALL
  queries returned real failures (success=False or raised exceptions before
  reaching the silence generator), `last_silence_result` was never assigned.
  The final fallback call at the end of the loop handles this correctly now,
  but `last_silence_result` was also never actually used — the variable was
  set but then ignored in favor of a fresh `music_server.call()` at the end.
  Removed the dead variable entirely. Simplified to: try each query for real
  music, stop on first success, then make one final guaranteed call at end.

BUG FIX B — MUSIC NEVER FETCHED (follows from music_server.py fix):
  The MusicMCPServer previously always failed (Pixabay has no audio API,
  CDN URLs were dead) and fell through to silence. Now that music_server.py
  uses working incompetech.com CDN URLs, this agent will actually receive
  real music on the first or second query attempt in most cases.
  Extended the query list to give more chances before accepting silence.
"""
import os
from loguru import logger
from mcp_servers.music_server import MusicMCPServer


NICHE_MUSIC_QUERIES = {
    "motivation": [
        "uplifting motivational background",
        "epic inspiring background",
        "upbeat positive background",
        "motivational corporate background",
        "inspiring background instrumental",
        "uplifting energetic background",
    ],
    "horror": [
        "dark ambient horror",
        "suspenseful scary background",
        "creepy atmospheric",
        "dark tension background",
        "horror ambient dark",
        "scary dark suspense",
    ],
    "reddit_story": [
        "calm storytelling background",
        "narrative background music",
        "calm ambient background",
        "soft piano background",
        "calm background instrumental",
        "storytelling narrative calm",
    ],
    "brainrot": [
        "chaotic energetic electronic",
        "upbeat fast background",
        "energetic electronic background",
        "fast upbeat background",
        "chaotic fast electronic",
        "energetic upbeat background",
    ],
    "finance": [
        "corporate professional background",
        "calm business background",
        "professional ambient instrumental",
        "corporate calm background",
        "clean minimal background",
        "professional corporate calm",
    ],
}

EMOTION_MUSIC_QUERIES = {
    "inspiration": "uplifting inspiring background",
    "fear":        "dark horror ambient",
    "shock":       "dramatic tension",
    "curiosity":   "calm mysterious background",
    "urgency":     "fast energetic background",
    "amusement":   "upbeat fun background",
    "chaos":       "chaotic energetic electronic",
    "dread":       "dark slow ambient",
}


class MusicDirectorAgent:
    def __init__(self, config):
        self.config = config
        self.music_server = MusicMCPServer()
        self.music_config = config.get("music", {})

    def run(self, script: dict, video_id: str, output_dir: str = "/tmp", *args, **kwargs):
        logger.info(f"MusicDirectorAgent fetching music for video: {video_id}")

        niche = os.environ.get("NICHE", self.config.get("video", {}).get("niche", "motivation"))
        emotion = script.get("emotion", "inspiration")
        music_path = f"{output_dir}/{video_id}_music.mp3"
        duration = self.music_config.get("duration_seconds", 60)

        # Build prioritized query list
        queries = []
        if emotion in EMOTION_MUSIC_QUERIES:
            queries.append(EMOTION_MUSIC_QUERIES[emotion])
        queries.extend(NICHE_MUSIC_QUERIES.get(niche, ["calm background instrumental"]))

        # Deduplicate while preserving order
        seen = set()
        unique_queries = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                unique_queries.append(q)

        logger.info(f"Music queries: {len(unique_queries)} | niche={niche} emotion={emotion}")

        # FIX A: Removed last_silence_result — was set but never used.
        # Simplified loop: try each query, return immediately on real music.
        for i, query in enumerate(unique_queries):
            logger.debug(f"Music query {i+1}/{len(unique_queries)}: '{query}'")
            try:
                result = self.music_server.call(
                    "fetch_music",
                    query=query,
                    output_path=music_path,
                    duration_seconds=duration,
                )

                if result.get("success") and result.get("music_path"):
                    source = result.get("source", "unknown")

                    # Stop trying if we got real music (not the ambient fallback)
                    if source not in ("generated_ambient", "none"):
                        logger.success(
                            f"Music fetched ✅  source={source} | "
                            f"query='{query}' | path={music_path}"
                        )
                        return {
                            "success":          True,
                            "music_path":       result["music_path"],
                            "title":            result.get("title", query),
                            "source":           source,
                            "volume_reduction": self.music_config.get("volume_reduction", 0.12),
                        }
                    else:
                        # Got ambient fallback — keep trying for CDN music
                        logger.debug(f"Query '{query}' returned ambient fallback, trying next...")
                        continue

            except Exception as e:
                logger.debug(f"Music query '{query}' raised exception: {e}")
                continue

        # All queries exhausted — make one final call to get the best
        # available result (CDN music, ambient tone, or None for voice-only).
        logger.warning(
            f"All {len(unique_queries)} music queries failed to find CDN music. "
            f"Using ambient fallback."
        )
        final_result = self.music_server.call(
            "fetch_music",
            query=unique_queries[0] if unique_queries else "calm background",
            output_path=music_path,
            duration_seconds=duration,
        )

        # Always return success=True — music is optional, never crash pipeline.
        return {
            "success":          True,
            "music_path":       final_result.get("music_path"),  # None = voice-only
            "title":            final_result.get("title", "background music"),
            "source":           final_result.get("source", "none"),
            "volume_reduction": self.music_config.get("volume_reduction", 0.12),
        }
