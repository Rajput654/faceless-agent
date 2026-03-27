"""
mcp_servers/analytics_server.py
Tracks video performance in Supabase database.
"""
import os
from loguru import logger

try:
    from supabase import create_client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False


class AnalyticsMCPServer:
    def __init__(self):
        self.supabase_url = os.environ.get("SUPABASE_URL", "")
        self.supabase_key = os.environ.get("SUPABASE_KEY", "")
        self._client = None
        self.tools = {
            "save_video": self._save_video,
            "save_topic": self._save_topic,
            "save_batch_run": self._save_batch_run,
        }

    @property
    def client(self):
        if self._client is None and SUPABASE_AVAILABLE and self.supabase_url and self.supabase_key:
            try:
                self._client = create_client(self.supabase_url, self.supabase_key)
            except Exception as e:
                logger.warning(f"Supabase connection failed: {e}")
        return self._client

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _save_video(self, video_id: str, title: str, topic: str, niche: str, youtube_url: str = None, quality_score: float = 0.0, status: str = "pending", **kwargs):
        if not self.client:
            logger.debug("Supabase not configured, skipping video save.")
            return {"success": True, "simulated": True}
        try:
            data = {
                "video_id": video_id,
                "title": title,
                "topic": topic,
                "niche": niche,
                "youtube_url": youtube_url,
                "quality_score": quality_score,
                "status": status,
                "metadata": kwargs,
            }
            self.client.table("videos").upsert(data).execute()
            return {"success": True}
        except Exception as e:
            logger.warning(f"Failed to save video to Supabase: {e}")
            return {"success": False, "error": str(e)}

    def _save_topic(self, topic: str, niche: str, **kwargs):
        if not self.client:
            return {"success": True, "simulated": True}
        try:
            data = {"topic": topic, "niche": niche, **{k: v for k, v in kwargs.items() if k in ["hook", "angle", "emotion", "virality_score", "source"]}}
            self.client.table("topics").insert(data).execute()
            return {"success": True}
        except Exception as e:
            logger.warning(f"Failed to save topic: {e}")
            return {"success": False, "error": str(e)}

    def _save_batch_run(self, total: int, passed: int, failed: int, niche: str, **kwargs):
        if not self.client:
            return {"success": True, "simulated": True}
        try:
            self.client.table("batch_runs").insert({
                "total_videos": total, "passed": passed, "failed": failed, "niche": niche
            }).execute()
            return {"success": True}
        except Exception as e:
            logger.warning(f"Failed to save batch run: {e}")
            return {"success": False, "error": str(e)}
