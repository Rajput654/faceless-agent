"""
agents/social_publisher.py

Publishes the final video to YouTube with proper metadata.
Always uses YouTube project A (single-channel setup).

FIX: thumbnail_path is now accepted as a parameter in run() and forwarded
to SocialMCPServer._upload_youtube() so that custom thumbnails are actually
set on the uploaded video via the YouTube thumbnails.set() API.

Previously the thumbnail was generated in video_workflow.py and stored in
state.thumbnail_path, but this agent never received or used it — the
upload call was made without a thumbnail and YouTube used its auto-generated
still frame instead.
"""
import os
from loguru import logger
from mcp_servers.social_server import SocialMCPServer


class SocialPublisherAgent:
    def __init__(self, config):
        self.config = config
        self.social_server = SocialMCPServer()
        self.yt_config = config.get("youtube", {})

    def run(
        self,
        video_path: str,
        script: dict,
        video_index: int = 0,
        thumbnail_path: str = None,   # FIX: was missing, thumbnail was never forwarded
        *args,
        **kwargs,
    ):
        logger.info(f"SocialPublisherAgent uploading video: {video_path}")

        if not os.path.exists(video_path):
            return {"status": "failed", "error": f"Video not found: {video_path}"}

        title = script.get("title", "Amazing Short")[:100]
        description = script.get("description", "#Shorts")[:5000]
        tags = script.get("tags", ["shorts", "viral"])

        # Ensure #Shorts in description for YouTube Shorts detection
        if "#Shorts" not in description and "#shorts" not in description:
            description = description + "\n\n#Shorts"

        # Log thumbnail status for debugging
        if thumbnail_path and os.path.exists(thumbnail_path):
            thumb_size = os.path.getsize(thumbnail_path)
            logger.info(
                f"Thumbnail ready for upload: {thumbnail_path} "
                f"({thumb_size // 1024} KB)"
            )
        elif thumbnail_path:
            logger.warning(
                f"Thumbnail path provided but file not found: {thumbnail_path}"
            )
            thumbnail_path = None
        else:
            logger.info("No thumbnail provided — YouTube will use auto-generated frame")

        result = self.social_server.call(
            "upload_youtube",
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            category_id=self.yt_config.get("category_id", "22"),
            privacy=self.yt_config.get("privacy", "public"),
            made_for_kids=self.yt_config.get("made_for_kids", False),
            project="A",
            thumbnail_path=thumbnail_path,   # FIX: now forwarded to API
        )

        if result.get("success"):
            youtube_url = result.get("youtube_url")
            thumbnail_uploaded = result.get("thumbnail_uploaded", False)

            logger.success(
                f"Published: {youtube_url} | "
                f"thumbnail={'uploaded' if thumbnail_uploaded else 'auto-generated'}"
            )
            return {
                "status": "published",
                "youtube_url": youtube_url,
                "youtube_video_id": result.get("video_id"),
                "thumbnail_uploaded": thumbnail_uploaded,
                "simulated": result.get("simulated", False),
            }
        else:
            logger.error(f"Publishing failed: {result.get('error')}")
            return {"status": "failed", "error": result.get("error")}
