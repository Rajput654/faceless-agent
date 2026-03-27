"""
agents/visual_director.py
Fetches or generates background images for each scene in the script.
"""
import os
from loguru import logger
from mcp_servers.image_server import ImageMCPServer


NICHE_VISUAL_STYLES = {
    "motivation": "cinematic motivational success achievement",
    "horror": "dark scary horror atmospheric shadows",
    "reddit_story": "everyday life candid realistic scene",
    "brainrot": "colorful chaotic meme absurd internet",
    "finance": "professional finance money business clean",
}

EMOTION_VISUAL_STYLES = {
    "inspiration": "golden hour sunrise achievement success",
    "fear": "dark fog shadows eerie night",
    "shock": "dramatic contrast high impact",
    "curiosity": "mysterious atmospheric discovery",
    "urgency": "bold dynamic action movement",
    "amusement": "colorful fun bright playful",
    "chaos": "abstract chaotic energetic vibrant",
}


class VisualDirectorAgent:
    def __init__(self, config):
        self.config = config
        self.image_server = ImageMCPServer()
        self.image_config = config.get("images", {})
        self.video_config = config.get("video", {})

    def _build_query(self, script: dict) -> str:
        niche = os.environ.get("NICHE", self.config.get("video", {}).get("niche", "motivation"))
        emotion = script.get("emotion", "inspiration")
        topic = script.get("topic_brief", {}).get("topic", "") or script.get("title", "")

        # Combine topic keywords + emotion style + niche style
        niche_style = NICHE_VISUAL_STYLES.get(niche, "cinematic background")
        emotion_style = EMOTION_VISUAL_STYLES.get(emotion, "")

        # Use first 3 words of topic to keep query tight
        topic_keywords = " ".join(topic.split()[:3]) if topic else ""

        parts = [p for p in [topic_keywords, emotion_style, niche_style] if p]
        return " ".join(parts)[:100]

    def run(self, script: dict, video_id: str, output_dir: str = "/tmp", *args, **kwargs):
        logger.info(f"VisualDirectorAgent fetching visuals for video: {video_id}")

        # Determine how many images to fetch based on duration
        duration = self.video_config.get("duration_seconds", 55)
        # 1 image per ~10 seconds, minimum 3, maximum 8
        num_images = max(3, min(8, duration // 10))

        width = self.image_config.get("width", 1080)
        height = self.image_config.get("height", 1920)

        query = self._build_query(script)
        logger.info(f"Image query: '{query}' | Fetching {num_images} images")

        image_paths = [
            f"{output_dir}/{video_id}_scene_{i:02d}.jpg"
            for i in range(num_images)
        ]

        result = self.image_server.call(
            "fetch_images",
            query=query,
            output_paths=image_paths,
            width=width,
            height=height,
        )

        fetched_images = [
            img["path"]
            for img in result.get("images", [])
            if img.get("success") and os.path.exists(img.get("path", ""))
        ]

        if fetched_images:
            logger.success(f"Fetched {len(fetched_images)}/{num_images} images for {video_id}")
            return {
                "success": True,
                "image_paths": fetched_images,
                "query_used": query,
            }

        # Fallback: try a simpler generic query
        logger.warning(f"Primary image fetch failed, trying generic fallback query")
        niche = os.environ.get("NICHE", self.config.get("video", {}).get("niche", "motivation"))
        fallback_query = NICHE_VISUAL_STYLES.get(niche, "cinematic background landscape")

        result = self.image_server.call(
            "fetch_images",
            query=fallback_query,
            output_paths=image_paths,
            width=width,
            height=height,
        )

        fetched_images = [
            img["path"]
            for img in result.get("images", [])
            if img.get("success") and os.path.exists(img.get("path", ""))
        ]

        if fetched_images:
            logger.success(f"Fallback fetch got {len(fetched_images)} images")
            return {
                "success": True,
                "image_paths": fetched_images,
                "query_used": fallback_query,
            }

        logger.error("All image fetching failed.")
        return {
            "success": False,
            "image_paths": [],
            "error": "All image sources failed",
        }
