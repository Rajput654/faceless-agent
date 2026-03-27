"""
agents/social_publisher.py
Publishes the final video to YouTube with proper metadata.
"""
import os
from loguru import logger
from mcp_servers.social_server import SocialMCPServer


class SocialPublisherAgent:
    def __init__(self, config):
        self.config = config
        self.social_server = SocialMCPServer()
        self.yt_config = config.get("youtube", {})

    def run(self, video_path: str, script: dict, video_index: int = 0, *args, **kwargs):
        logger.info(f"SocialPublisherAgent uploading video: {video_path}")

        if not os.path.exists(video_path):
            return {"status": "failed", "error": f"Video not found: {video_path}"}

        title = script.get("title", "Amazing Short")[:100]
        description = script.get("description", "#Shorts")[:5000]
        tags = script.get("tags", ["shorts", "viral"])

        # Add #Shorts to description for YouTube Shorts detection
        if "#Shorts" not in description and "#shorts" not in description:
            description = description + "\n\n#Shorts"

        # Select YouTube project (A or B) alternating by video index
        project = "A" if video_index % 2 == 0 else "B"

        result = self.social_server.call(
            "upload_youtube",
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            category_id=self.yt_config.get("category_id", "22"),
            privacy=self.yt_config.get("privacy", "public"),
            made_for_kids=self.yt_config.get("made_for_kids", False),
            project=project,
        )

        if result.get("success"):
            logger.success(f"Published: {result.get('youtube_url')}")
            return {
                "status": "published",
                "youtube_url": result.get("youtube_url"),
                "youtube_video_id": result.get("video_id"),
                "simulated": result.get("simulated", False),
            }
        else:
            logger.error(f"Publishing failed: {result.get('error')}")
            return {"status": "failed", "error": result.get("error")}
