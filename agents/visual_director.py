"""
agents/visual_director.py

Fetches real MP4 video clips for each scene. Falls back to still images
only when both Pexels and Pixabay video APIs fail, so the final video
is composed of moving footage rather than a static-photo slideshow.
"""
import os
from loguru import logger
from mcp_servers.image_server import ImageMCPServer
from mcp_servers.video_fetcher import VideoFetcherMCPServer


NICHE_VISUAL_STYLES = {
    "motivation": "success achievement cinematic sunrise",
    "horror":     "dark shadows horror atmospheric",
    "reddit_story": "everyday life realistic candid",
    "brainrot":   "colorful chaotic neon internet",
    "finance":    "professional business money clean",
}

EMOTION_VISUAL_STYLES = {
    "inspiration": "golden hour sunrise achievement",
    "fear":        "dark fog shadows eerie night",
    "shock":       "dramatic contrast high impact",
    "curiosity":   "mysterious atmospheric discovery",
    "urgency":     "bold dynamic action movement",
    "amusement":   "colorful fun bright playful",
    "chaos":       "abstract chaotic energetic vibrant",
}


class VisualDirectorAgent:
    def __init__(self, config):
        self.config         = config
        self.image_server   = ImageMCPServer()
        self.video_fetcher  = VideoFetcherMCPServer()
        self.image_config   = config.get("images", {})
        self.video_config   = config.get("video", {})

    def _get_niche(self) -> str:
        return os.environ.get(
            "NICHE",
            self.config.get("video", {}).get("niche", "motivation")
        )

    def _build_query(self, script: dict) -> str:
        niche   = self._get_niche()
        emotion = script.get("emotion", "inspiration")
        topic   = (
            script.get("topic_brief", {}).get("topic", "")
            or script.get("title", "")
        )
        niche_style   = NICHE_VISUAL_STYLES.get(niche, "cinematic background")
        emotion_style = EMOTION_VISUAL_STYLES.get(emotion, "")
        topic_keywords = " ".join(topic.split()[:3]) if topic else ""
        parts = [p for p in [topic_keywords, emotion_style, niche_style] if p]
        return " ".join(parts)[:100]

    def run(self, script: dict, video_id: str, output_dir: str = "/tmp", *args, **kwargs):
        niche   = self._get_niche()
        emotion = script.get("emotion", "inspiration")

        logger.info(f"VisualDirectorAgent → niche={niche} emotion={emotion} video={video_id}")

        # 1 clip per ~8 seconds of audio; min 4, max 8
        duration    = self.video_config.get("duration_seconds", 55)
        num_clips   = max(4, min(8, duration // 8))
        query       = self._build_query(script)

        logger.info(f"Fetching {num_clips} VIDEO CLIPS for query: '{query}'")

        # ── Step 1: try real video clips ──────────────────────────────────────
        clip_paths = [
            f"{output_dir}/{video_id}_clip_{i:02d}.mp4"
            for i in range(num_clips)
        ]

        result = self.video_fetcher.fetch_clips(
            query=query,
            output_paths=clip_paths,
            niche=niche,
            emotion=emotion,
            min_duration=4,
            max_duration=15,
        )

        successful_clips = [
            p for p in result.get("clip_paths", [])
            if p and os.path.exists(p) and os.path.getsize(p) > 50_000
        ]

        if len(successful_clips) >= 2:
            logger.success(
                f"Fetched {len(successful_clips)}/{num_clips} video clips for {video_id}"
            )
            return {
                "success":     True,
                "image_paths": successful_clips,   # kept as "image_paths" for API compat
                "is_video":    True,
                "query_used":  query,
            }

        # ── Step 2: generic query fallback for clips ──────────────────────────
        logger.warning("Primary video fetch insufficient, trying generic fallback query")
        fallback_query = NICHE_VISUAL_STYLES.get(niche, "cinematic background landscape")
        result = self.video_fetcher.fetch_clips(
            query=fallback_query,
            output_paths=clip_paths,
            niche=niche,
            emotion=emotion,
            min_duration=3,
            max_duration=20,
        )
        successful_clips = [
            p for p in result.get("clip_paths", [])
            if p and os.path.exists(p) and os.path.getsize(p) > 50_000
        ]
        if len(successful_clips) >= 2:
            logger.success(f"Generic video fetch got {len(successful_clips)} clips")
            return {
                "success":     True,
                "image_paths": successful_clips,
                "is_video":    True,
                "query_used":  fallback_query,
            }

        # ── Step 3: fall back to still images if no clips available ──────────
        logger.warning("Video clip fetch failed — falling back to still images with Ken Burns")
        width  = self.image_config.get("width", 1080)
        height = self.image_config.get("height", 1920)
        num_images = max(4, min(8, duration // 8))
        image_paths = [
            f"{output_dir}/{video_id}_scene_{i:02d}.jpg"
            for i in range(num_images)
        ]

        img_result = self.image_server.call(
            "fetch_images",
            query=query,
            output_paths=image_paths,
            width=width,
            height=height,
        )
        fetched_images = [
            img["path"]
            for img in img_result.get("images", [])
            if img.get("success") and os.path.exists(img.get("path", ""))
        ]

        if fetched_images:
            logger.info(f"Image fallback: {len(fetched_images)} still images")
            return {
                "success":     True,
                "image_paths": fetched_images,
                "is_video":    False,
                "query_used":  query,
            }

        logger.error("All visual fetching failed (clips and images)")
        return {
            "success":     False,
            "image_paths": [],
            "is_video":    False,
            "error":       "All visual sources failed",
        }
