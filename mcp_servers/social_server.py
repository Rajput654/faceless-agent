"""
mcp_servers/social_server.py
Handles YouTube video uploads using the YouTube Data API v3.

Project selection:
  - Even-indexed videos → project A
  - Odd-indexed videos  → project B  (falls back to A if B creds not set)
"""
import os
import time
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

    def _has_credentials(self, project: str) -> bool:
        return all([
            os.environ.get(f"YOUTUBE_CLIENT_ID_{project}"),
            os.environ.get(f"YOUTUBE_CLIENT_SECRET_{project}"),
            os.environ.get(f"YOUTUBE_REFRESH_TOKEN_{project}"),
        ])

    def _get_youtube_client(self, project: str = "A"):
        if not GOOGLE_LIBS:
            raise RuntimeError("google-api-python-client not installed")

        # FIX: fall back to project A if requested project has no credentials
        if not self._has_credentials(project):
            if project != "A" and self._has_credentials("A"):
                logger.warning(
                    f"YouTube project {project} credentials not set — "
                    f"falling back to project A"
                )
                project = "A"
            else:
                raise ValueError(
                    f"Missing YouTube credentials for project {project}. "
                    f"Set YOUTUBE_CLIENT_ID_{project}, YOUTUBE_CLIENT_SECRET_{project}, "
                    f"and YOUTUBE_REFRESH_TOKEN_{project} in GitHub Secrets."
                )

        creds = Credentials(
            token=None,
            refresh_token=os.environ[f"YOUTUBE_REFRESH_TOKEN_{project}"],
            client_id=os.environ[f"YOUTUBE_CLIENT_ID_{project}"],
            client_secret=os.environ[f"YOUTUBE_CLIENT_SECRET_{project}"],
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

            media = MediaFileUpload(
                video_path, chunksize=1024 * 1024, resumable=True, mimetype="video/mp4"
            )
            request = youtube.videos().insert(
                part=",".join(body.keys()), body=body, media_body=media
            )

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
