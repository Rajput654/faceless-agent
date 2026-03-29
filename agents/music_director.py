"""
agents/music_director.py

FIXED v4 — Two critical bugs patched:

BUG FIX A — AMBIENT FALLBACK DISCARDED (loop logic):
  Previous code used `continue` when source == "generated_ambient", meaning
  the ambient tone returned by music_server was thrown away and the loop kept
  retrying. After all queries exhausted, the final fallback call ALSO returned
  ambient — but by then the file from a previous attempt may have been
  overwritten, and the result was still not guaranteed to reach the caller.

  Fix: The loop now stores the first ambient result it receives in
  `ambient_fallback`. Real CDN music causes an immediate return. After all
  queries are exhausted, `ambient_fallback` is returned if available. This
  ensures ambient audio is ALWAYS returned as long as ffmpeg works.

BUG FIX B — MUSIC_PATH=NONE CRASHED PIPELINE SILENTLY:
  If both CDN and ambient generation failed (music_path=None), the returned
  dict had success=True but music_path=None. VideoComposerAgent handled this
  correctly, but the log made it look like music was fetched. Added explicit
  warning logging when returning None so it's visible in CI.

ALSO: Extended query list to give more CDN attempts before accepting ambient.
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

        niche   = os.environ.get("NICHE", self.config.get("video", {}).get("niche", "motivation"))
        emotion = script.get("emotion", "inspiration")
        music_path = f"{output_dir}/{video_id}_music.mp3"
        duration   = self.music_config.get("duration_seconds", 60)

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

        logger.info(
            f"Music queries: {len(unique_queries)} | niche={niche} emotion={emotion}"
        )

        # FIX A: Store the first ambient result instead of discarding it.
        # Real CDN music → return immediately.
        # Ambient tone → save it and keep trying for real music.
        # After loop exhausted → return ambient if we have it.
        ambient_fallback = None

        for i, query in enumerate(unique_queries):
            logger.debug(f"Music query {i+1}/{len(unique_queries)}: '{query}'")
            try:
                result = self.music_server.call(
                    "fetch_music",
                    query=query,
                    output_path=music_path,
                    duration_seconds=duration,
                )

                if not result.get("success"):
                    continue

                music_path_result = result.get("music_path")
                source = result.get("source", "unknown")

                if not music_path_result:
                    # music_path=None means even ambient failed — keep trying
                    logger.debug(f"Query '{query}' returned music_path=None")
                    continue

                if source not in ("generated_ambient", "none"):
                    # Got real CDN or local music — return immediately
                    logger.success(
                        f"Music fetched ✅  source={source} | "
                        f"query='{query}' | path={music_path_result}"
                    )
                    return {
                        "success":          True,
                        "music_path":       music_path_result,
                        "title":            result.get("title", query),
                        "source":           source,
                        "volume_reduction": self.music_config.get("volume_reduction", 0.12),
                    }
                else:
                    # FIX A: Got ambient fallback — save it, keep trying for real music
                    if ambient_fallback is None:
                        ambient_fallback = result
                        logger.debug(
                            f"Ambient tone saved as fallback (source={source}). "
                            f"Continuing to search for CDN music..."
                        )
                    continue

            except Exception as e:
                logger.debug(f"Music query '{query}' raised exception: {e}")
                continue

        # ── All CDN queries exhausted ─────────────────────────────────────────

        # FIX A: Return the stored ambient fallback if we have one
        if ambient_fallback and ambient_fallback.get("music_path"):
            logger.warning(
                f"No CDN music found after {len(unique_queries)} queries. "
                f"Using synthetic ambient tone as background music."
            )
            return {
                "success":          True,
                "music_path":       ambient_fallback["music_path"],
                "title":            ambient_fallback.get("title", "ambient"),
                "source":           ambient_fallback.get("source", "generated_ambient"),
                "volume_reduction": self.music_config.get("volume_reduction", 0.12),
            }

        # No ambient was stored — make one final guaranteed attempt
        logger.warning(
            f"All {len(unique_queries)} music queries failed with no ambient stored. "
            f"Making final ambient generation attempt."
        )
        final_result = self.music_server.call(
            "fetch_music",
            query=unique_queries[0] if unique_queries else "calm background",
            output_path=music_path,
            duration_seconds=duration,
        )

        final_music_path = final_result.get("music_path")

        # FIX B: Explicit warning when returning None so it's visible in CI
        if not final_music_path:
            logger.warning(
                "Music generation completely failed. Video will be voice-only. "
                "To fix: add MP3 files to assets/music/ or check ffmpeg installation."
            )
        else:
            logger.info(
                f"Final music fallback: source={final_result.get('source')} | "
                f"path={final_music_path}"
            )

        # Always return success=True — music is optional, never crash the pipeline
        return {
            "success":          True,
            "music_path":       final_music_path,
            "title":            final_result.get("title", "background music"),
            "source":           final_result.get("source", "none"),
            "volume_reduction": self.music_config.get("volume_reduction", 0.12),
        }
