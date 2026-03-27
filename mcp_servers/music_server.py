"""
mcp_servers/music_server.py
Fetches background music from Pixabay (free, no attribution required).
"""
import os
import requests
from pathlib import Path
from loguru import logger


class MusicMCPServer:
    def __init__(self):
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")
        self.tools = {
            "fetch_music": self._fetch_music,
        }

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _fetch_music(self, query: str, output_path: str, duration_seconds: int = 60, **kwargs):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if not self.pixabay_key:
            logger.warning("No Pixabay API key. Skipping music fetch.")
            return {"success": False, "error": "No Pixabay API key"}

        try:
            params = {
                "key": self.pixabay_key,
                "q": query,
                "per_page": 10,
                "page": 1,
            }
            resp = requests.get("https://pixabay.com/api/music/", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", [])

            if not hits:
                # Try generic query
                params["q"] = "background music cinematic"
                resp = requests.get("https://pixabay.com/api/music/", params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                hits = data.get("hits", [])

            if not hits:
                return {"success": False, "error": "No music found"}

            music = hits[0]
            music_url = music.get("audio", music.get("previewURL", ""))
            if not music_url:
                return {"success": False, "error": "No audio URL in response"}

            audio_resp = requests.get(music_url, timeout=60)
            audio_resp.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(audio_resp.content)

            logger.success(f"Music downloaded: {output_path} ({os.path.getsize(output_path)} bytes)")
            return {
                "success": True,
                "music_path": output_path,
                "title": music.get("tags", "background music"),
                "duration": music.get("duration", 0),
            }
        except Exception as e:
            logger.error(f"Music fetch failed: {e}")
            return {"success": False, "error": str(e)}
