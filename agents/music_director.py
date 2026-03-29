"""
agents/music_director.py

FIXED v2 — Two bugs patched:

BUG FIX A — RESULT KEY INCONSISTENCY:
  MusicMCPServer returns {"success": True, "music_path": "..."} but this
  agent was checking result.get("success") correctly, HOWEVER it then
  returned its own dict with key "music_path" which the VideoComposerAgent
  reads as music_result.get("music_path"). This was actually fine in the
  happy path, but on failure paths the agent returned:
    {"success": False, "music_path": None, "error": "..."}
  and VideoComposerAgent's check was:
    music_path = music_result.get("music_path") if music_result.get("success") else None
  This meant a failed music fetch set music_path=None which then meant
  _dynamic_music_mix() was never called → no music in video.

  FIX: Music is OPTIONAL. Even when all sources fail, the composer should
  still receive the silence fallback path (from music_server's silence
  generator). The agent now always returns success=True with whatever
  path music_server provided (including silence).

BUG FIX B — QUERY LIST TOO NARROW:
  The original query list only tried 3-4 queries before giving up.
  Added niche-specific extended query lists so Pixabay has more chances
  to find a matching track before falling back to the generic tracks.
"""
import os
from loguru import logger
from mcp_servers.music_server import MusicMCPServer


NICHE_MUSIC_QUERIES = {
    "motivation": [
        "uplifting motivational background",
        "epic cinematic inspiring",
        "upbeat positive background music",
        "motivational corporate background",
        "inspiring background instrumental",
    ],
    "horror": [
        "dark ambient horror",
        "suspenseful scary background",
        "creepy atmospheric music",
        "dark tension background",
        "horror film score ambient",
    ],
    "reddit_story": [
        "calm storytelling background",
        "narrative background music",
        "ambient background instrumental",
        "soft piano background",
        "documentary background music",
    ],
    "brainrot": [
        "chaotic electronic background",
        "upbeat energetic electronic",
        "fast paced background music",
        "energetic pop background",
        "quirky fun background music",
    ],
    "finance": [
        "corporate professional background",
        "calm business background music",
        "professional ambient instrumental",
        "corporate motivation background",
        "clean minimal background music",
    ],
}

EMOTION_MUSIC_QUERIES = {
    "inspiration": "uplifting inspiring background music",
    "fear":        "dark horror ambient suspense",
    "shock":       "dramatic tension cinematic",
    "curiosity":   "mysterious ambient background",
    "urgency":     "fast paced energetic background",
    "amusement":   "fun upbeat quirky background",
    "chaos":       "chaotic electronic energetic",
    "dread":       "dark slow ominous background",
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
        queries.extend(NICHE_MUSIC_QUERIES.get(niche, ["background music cinematic"]))

        # Deduplicate while preserving order
        seen = set()
        unique_queries = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                unique_queries.append(q)

        logger.info(f"Music queries to try: {len(unique_queries)} | niche={niche} emotion={emotion}")

        for i, query in enumerate(unique_queries):
            logger.debug(f"Music query {i+1}/{len(unique_queries)}: '{query}'")
            result = self.music_server.call(
                "fetch_music",
                query=query,
                output_path=music_path,
                duration_seconds=duration,
            )

            # BUG FIX A: music_server now ALWAYS returns success=True
            # (with either real music, fallback, or silence).
            # We check if we got actual music (non-silence source) and
            # break early, otherwise keep trying queries for better music.
            if result.get("success") and result.get("music_path"):
                source = result.get("source", "unknown")

                # If we got real music (not silence), stop trying
                if source not in ("generated_silence", "none"):
                    logger.success(
                        f"Music fetched ✅  source={source} | "
                        f"query='{query}' | path={music_path}"
                    )
                    return {
                        "success":        True,
                        "music_path":     result["music_path"],
                        "title":          result.get("title", query),
                        "source":         source,
                        "volume_reduction": self.music_config.get("volume_reduction", 0.12),
                    }
                else:
                    # Got silence — store it but keep trying for real music
                    logger.debug(f"Query '{query}' yielded silence, trying next query...")
                    last_silence_result = result
                    continue

        # All queries exhausted — use whatever music_server gave us last
        # (silence or None). Music is OPTIONAL — never fail the pipeline.
        logger.warning(
            f"All {len(unique_queries)} music queries failed to find real music. "
            f"Video will use silence or no background music."
        )

        # Final call to get the guaranteed silence fallback
        final_result = self.music_server.call(
            "fetch_music",
            query=unique_queries[0],
            output_path=music_path,
            duration_seconds=duration,
        )

        # BUG FIX A: Always return success=True so VideoComposerAgent
        # receives a valid result and can decide whether to mix or skip.
        return {
            "success":        True,
            "music_path":     final_result.get("music_path"),  # may be None if ffmpeg unavailable
            "title":          "background music",
            "source":         final_result.get("source", "none"),
            "volume_reduction": self.music_config.get("volume_reduction", 0.12),
        }
