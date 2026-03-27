"""
agents/music_director.py
Fetches background music appropriate for the video's emotion and niche.
"""
import os
from loguru import logger
from mcp_servers.music_server import MusicMCPServer


NICHE_MUSIC_QUERIES = {
    "motivation": ["uplifting motivational", "epic cinematic", "inspiring background"],
    "horror": ["dark ambient horror", "suspenseful scary", "creepy atmospheric"],
    "reddit_story": ["storytelling background", "calm narrative music", "ambient background"],
    "brainrot": ["chaotic electronic", "funny meme music", "upbeat energetic"],
    "finance": ["corporate background", "calm piano background", "professional ambient"],
}


class MusicDirectorAgent:
    def __init__(self, config):
        self.config = config
        self.music_server = MusicMCPServer()
        self.music_config = config.get("music", {})

    def run(self, script: dict, video_id: str, output_dir: str = "/tmp", *args, **kwargs):
        logger.info(f"MusicDirectorAgent fetching music for video: {video_id}")

        niche = os.environ.get("NICHE", self.config.get("video", {}).get("niche", "motivation"))
        emotion = script.get("emotion", "inspiration")
        music_path = f"{output_dir}/{video_id}_music.mp3"

        queries = NICHE_MUSIC_QUERIES.get(niche, ["background music cinematic"])

        for query in queries:
            result = self.music_server.call(
                "fetch_music",
                query=query,
                output_path=music_path,
                duration_seconds=self.music_config.get("duration_seconds", 60),
            )
            if result.get("success"):
                logger.success(f"Music fetched: {music_path}")
                return {
                    "success": True,
                    "music_path": music_path,
                    "title": result.get("title", query),
                    "volume_reduction": self.music_config.get("volume_reduction", 0.12),
                }

        logger.warning("Music fetch failed. Video will have no background music.")
        return {"success": False, "music_path": None, "error": "All music sources failed"}
