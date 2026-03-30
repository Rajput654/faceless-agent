"""
agents/music_director.py

FIXED v5 — All music-blocking bugs resolved:

BUG FIX B5/B10 — AMBIENT FILE OVERWRITTEN BY SUBSEQUENT QUERIES:
  Every query iteration called music_server with the SAME output_path.
  If query 1 returned an ambient tone (stored in ambient_fallback dict),
  query 2 then overwrote that exact file on disk. When the loop finished
  and ambient_fallback was returned, its file no longer existed or was
  corrupted by a partial download from a later attempt.

  Fix: Each query attempt uses a UNIQUE temp path (_attempt_{i}.mp3).
  When a real CDN track is found, it's moved to the canonical output_path.
  The ambient fallback temp file is moved to the canonical path only at
  the very end, after all CDN attempts have finished.

BUG FIX B10 — SAME PATH FOR ALL RETRIES:
  Related to above — unique paths per attempt prevent any cross-contamination.
"""
import os
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

        niche    = os.environ.get("NICHE", self.config.get("video", {}).get("niche", "motivation"))
        emotion  = script.get("emotion", "inspiration")
        # Canonical output path — only written when we have a confirmed good file
        final_music_path = f"{output_dir}/{video_id}_music.mp3"
        duration = self.music_config.get("duration_seconds", 60)

        # Build prioritized query list
        queries = []
        if emotion in EMOTION_MUSIC_QUERIES:
            queries.append(EMOTION_MUSIC_QUERIES[emotion])
        queries.extend(NICHE_MUSIC_QUERIES.get(niche, ["calm background instrumental"]))

        # Deduplicate
        seen = set()
        unique_queries = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                unique_queries.append(q)

        logger.info(
            f"Music queries: {len(unique_queries)} | niche={niche} emotion={emotion}"
        )

        # BUG FIX B5/B10: Use UNIQUE temp path per attempt so they never overwrite each other.
        # ambient_fallback_path tracks the first ambient file written so we can use it
        # at the end if no CDN music was found — and it won't have been overwritten.
        ambient_fallback_path = None
        attempt_temp_paths = []  # track all temp files for cleanup

        try:
            for i, query in enumerate(unique_queries):
                # Each attempt gets its own isolated temp file
                attempt_path = f"{output_dir}/{video_id}_music_attempt_{i}.mp3"
                attempt_temp_paths.append(attempt_path)

                logger.debug(f"Music query {i+1}/{len(unique_queries)}: '{query}'")
                try:
                    result = self.music_server.call(
                        "fetch_music",
                        query=query,
                        output_path=attempt_path,   # BUG FIX: unique per attempt
                        duration_seconds=duration,
                    )

                    if not result.get("success"):
                        continue

                    music_path_result = result.get("music_path")
                    source = result.get("source", "unknown")

                    if not music_path_result or not os.path.exists(music_path_result):
                        logger.debug(f"Query '{query}' returned missing music_path")
                        continue

                    file_size = os.path.getsize(music_path_result)
                    if file_size < 10_000:
                        logger.debug(f"Query '{query}' returned tiny file ({file_size} bytes)")
                        continue

                    if source not in ("generated_ambient", "none"):
                        # Got real CDN or local music — move to final path and return
                        if music_path_result != final_music_path:
                            shutil.move(music_path_result, final_music_path)
                        logger.success(
                            f"Music fetched ✅  source={source} | "
                            f"query='{query}' | path={final_music_path} | {file_size//1024} KB"
                        )
                        return {
                            "success":          True,
                            "music_path":       final_music_path,
                            "title":            result.get("title", query),
                            "source":           source,
                            "volume_reduction": self.music_config.get("volume_reduction", 0.12),
                        }
                    else:
                        # BUG FIX B5: Save the FIRST ambient result's UNIQUE path.
                        # It won't be overwritten because subsequent attempts use different paths.
                        if ambient_fallback_path is None:
                            ambient_fallback_path = music_path_result
                            logger.debug(
                                f"Ambient tone saved as fallback at unique path: "
                                f"{ambient_fallback_path} ({file_size//1024} KB). "
                                f"Continuing CDN search..."
                            )
                        continue

                except Exception as e:
                    logger.debug(f"Music query '{query}' raised exception: {e}")
                    continue

            # ── All CDN queries exhausted ─────────────────────────────────────

            # BUG FIX B5: ambient_fallback_path is a unique file, not overwritten
            if ambient_fallback_path and os.path.exists(ambient_fallback_path):
                size = os.path.getsize(ambient_fallback_path)
                if size > 500:
                    # Move the ambient file to the canonical final path
                    if ambient_fallback_path != final_music_path:
                        shutil.move(ambient_fallback_path, final_music_path)
                        ambient_fallback_path = final_music_path
                    logger.warning(
                        f"No CDN music found after {len(unique_queries)} queries. "
                        f"Using synthetic ambient tone ({size//1024} KB) as background music."
                    )
                    return {
                        "success":          True,
                        "music_path":       final_music_path,
                        "title":            "ambient",
                        "source":           "generated_ambient",
                        "volume_reduction": self.music_config.get("volume_reduction", 0.12),
                    }

            # No ambient was stored — make one final guaranteed attempt directly to final path
            logger.warning(
                f"All {len(unique_queries)} music queries failed with no ambient stored. "
                f"Making final ambient generation attempt directly to canonical path."
            )
            final_result = self.music_server.call(
                "fetch_music",
                query=unique_queries[0] if unique_queries else "calm background",
                output_path=final_music_path,
                duration_seconds=duration,
            )

            final_music = final_result.get("music_path")

            if not final_music or not os.path.exists(final_music):
                logger.warning(
                    "Music generation completely failed. Video will be voice-only. "
                    "To fix: add MP3 files to assets/music/ or check ffmpeg installation."
                )
                final_music = None

            return {
                "success":          True,
                "music_path":       final_music,
                "title":            final_result.get("title", "background music"),
                "source":           final_result.get("source", "none"),
                "volume_reduction": self.music_config.get("volume_reduction", 0.12),
            }

        finally:
            # Clean up all attempt temp files EXCEPT the one that became our result
            for tmp_path in attempt_temp_paths:
                if (tmp_path
                        and tmp_path != final_music_path
                        and tmp_path != ambient_fallback_path
                        and os.path.exists(tmp_path)):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
