"""
mcp_servers/social_server.py
Handles YouTube video uploads using the YouTube Data API v3.
"""
import os
import json
import time
import random
from loguru import logger

try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials
    GOOGLE_LIBS = True
except ImportError:
    GOOGLE_LIBS = False


class SocialMCPServer:
    def __init__(self):
        self.tools = {
            "upload_youtube": self._upload_youtube,
        }

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _get_youtube_client(self, project: str = "A"):
        if not GOOGLE_LIBS:
            raise RuntimeError("google-api-python-client not installed")

        client_id = os.environ.get(f"YOUTUBE_CLIENT_ID_{project}", "")
        client_secret = os.environ.get(f"YOUTUBE_CLIENT_SECRET_{project}", "")
        refresh_token = os.environ.get(f"YOUTUBE_REFRESH_TOKEN_{project}", "")

        if not all([client_id, client_secret, refresh_token]):
            raise ValueError(f"Missing YouTube credentials for project {project}")

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        return build("youtube", "v3", credentials=creds)

    def _upload_youtube(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: list,
        category_id: str = "22",
        privacy: str = "public",
        made_for_kids: bool = False,
        project: str = "A",
        **kwargs,
    ):
        if not os.path.exists(video_path):
            return {"success": False, "error": f"Video file not found: {video_path}"}

        if not GOOGLE_LIBS:
            logger.warning("Google API libraries not installed. Simulating upload.")
            return {
                "success": True,
                "youtube_url": f"https://youtube.com/shorts/SIMULATED_{int(time.time())}",
                "video_id": f"SIMULATED_{int(time.time())}",
                "simulated": True,
            }

        try:
            youtube = self._get_youtube_client(project)

            body = {
                "snippet": {
                    "title": title[:100],
                    "description": description[:5000],
                    "tags": tags[:500],
                    "categoryId": category_id,
                },
                "status": {
                    "privacyStatus": privacy,
                    "selfDeclaredMadeForKids": made_for_kids,
                },
            }

            media = MediaFileUpload(video_path, chunksize=1024 * 1024, resumable=True, mimetype="video/mp4")
            request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info(f"Upload progress: {int(status.progress() * 100)}%")

            video_id = response.get("id", "")
            youtube_url = f"https://youtube.com/shorts/{video_id}"
            logger.success(f"Uploaded to YouTube: {youtube_url}")
            return {"success": True, "youtube_url": youtube_url, "video_id": video_id}

        except Exception as e:
            logger.error(f"YouTube upload failed: {e}")
            return {"success": False, "error": str(e)}
