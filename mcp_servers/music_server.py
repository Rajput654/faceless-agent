"""
mcp_servers/music_server.py

Fetches background music.  Sources in priority order:
  1. Pixabay Audio API  — free, no attribution (correct endpoint)
  2. Free Music Archive — public domain tracks via direct URL
  3. Silent audio       — generates 60s of silence so pipeline never fails
"""
import os
import subprocess
import requests
from pathlib import Path
from loguru import logger


# Fallback: royalty-free public domain tracks from FMA / other reliable CDNs
# These are real, working direct MP3 URLs for common moods
FALLBACK_TRACKS = {
    "uplifting":    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
    "dark":         "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3",
    "calm":         "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-3.mp3",
    "energetic":    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-4.mp3",
    "corporate":    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-5.mp3",
}

QUERY_TO_MOOD = {
    "uplifting":    "uplifting",
    "motivational": "uplifting",
    "inspiring":    "uplifting",
    "epic":         "uplifting",
    "dark":         "dark",
    "horror":       "dark",
    "ambient":      "dark",
    "suspense":     "dark",
    "calm":         "calm",
    "piano":        "calm",
    "storytelling": "calm",
    "narrative":    "calm",
    "chaotic":      "energetic",
    "electronic":   "energetic",
    "meme":         "energetic",
    "fast":         "energetic",
    "corporate":    "corporate",
    "professional": "corporate",
    "business":     "corporate",
}


class MusicMCPServer:
    def __init__(self):
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")
        self.tools = {"fetch_music": self._fetch_music}

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _fetch_music(self, query: str, output_path: str, duration_seconds: int = 60, **kwargs):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # ── 1. Pixabay Audio API ─────────────────────────────────────────────
        # Correct endpoint: /api/music/ with &media_type not required
        if self.pixabay_key:
            result = self._fetch_pixabay(query, output_path)
            if result:
                return result

        # ── 2. Fallback: public domain tracks ────────────────────────────────
        result = self._fetch_fallback(query, output_path)
        if result:
            return result

        # ── 3. Last resort: generate silence so pipeline never crashes ────────
        result = self._generate_silence(output_path, duration_seconds)
        if result:
            logger.warning("Using silent audio — no background music available")
            return result

        return {"success": False, "error": "All music sources failed"}

    # ── Pixabay ───────────────────────────────────────────────────────────────

    def _fetch_pixabay(self, query: str, output_path: str):
        """
        Correct Pixabay Audio API endpoint.
        The 404s were caused by wrong endpoint URL — the API moved.
        Correct URL: https://pixabay.com/api/?key=...&q=...&media_type=music
        (The separate /api/music/ endpoint is no longer active.)
        """
        try:
            # Try the content API with media_type=music
            params = {
                "key":        self.pixabay_key,
                "q":          query,
                "media_type": "music",
                "per_page":   10,
                "page":       1,
            }
            resp = requests.get(
                "https://pixabay.com/api/",
                params=params, timeout=15
            )

            if resp.status_code == 404:
                # Some API versions still use the old audio endpoint with different params
                params2 = {
                    "key":      self.pixabay_key,
                    "q":        query,
                    "per_page": 10,
                }
                resp = requests.get(
                    "https://pixabay.com/api/music/",
                    params=params2, timeout=15
                )

            if not resp.ok:
                logger.warning(f"Pixabay music API returned {resp.status_code} for '{query}'")
                return None

            data = resp.json()
            hits = data.get("hits", [])
            if not hits:
                logger.warning(f"Pixabay: no music hits for '{query}'")
                return None

            for hit in hits:
                audio_url = (
                    hit.get("audio")
                    or hit.get("previewURL")
                    or hit.get("audioURL")
                    or hit.get("webformatURL", "")
                )
                if not audio_url:
                    continue

                audio_resp = requests.get(audio_url, timeout=60)
                if not audio_resp.ok:
                    continue

                with open(output_path, "wb") as f:
                    f.write(audio_resp.content)

                size = os.path.getsize(output_path)
                if size < 10_000:
                    os.remove(output_path)
                    continue

                logger.success(f"Pixabay music ✅  '{query}' → {output_path} ({size//1024} KB)")
                return {
                    "success":    True,
                    "music_path": output_path,
                    "title":      hit.get("tags", query),
                    "source":     "pixabay",
                }

            return None
        except Exception as e:
            logger.warning(f"Pixabay music error for '{query}': {e}")
            return None

    # ── Fallback: public domain tracks ───────────────────────────────────────

    def _fetch_fallback(self, query: str, output_path: str):
        """Download a mood-matched royalty-free track."""
        # Determine mood from query keywords
        query_lower = query.lower()
        mood = "calm"
        for keyword, m in QUERY_TO_MOOD.items():
            if keyword in query_lower:
                mood = m
                break

        url = FALLBACK_TRACKS.get(mood, FALLBACK_TRACKS["calm"])
        try:
            resp = requests.get(url, timeout=60, stream=True)
            resp.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)

            size = os.path.getsize(output_path)
            if size < 10_000:
                os.remove(output_path)
                return None

            logger.info(f"Fallback music track ({mood}) → {output_path} ({size//1024} KB)")
            return {
                "success":    True,
                "music_path": output_path,
                "title":      f"royalty-free-{mood}",
                "source":     "fallback",
            }
        except Exception as e:
            logger.warning(f"Fallback music download failed: {e}")
            return None

    # ── Silence generator ─────────────────────────────────────────────────────

    def _generate_silence(self, output_path: str, duration: int):
        """Generate a silent MP3 so the pipeline always has an audio file."""
        try:
            result = subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
                "-t", str(duration),
                "-c:a", "libmp3lame", "-b:a", "128k",
                output_path,
            ], capture_output=True, timeout=30)
            if result.returncode == 0 and os.path.exists(output_path):
                return {
                    "success":    True,
                    "music_path": output_path,
                    "title":      "silence",
                    "source":     "generated",
                }
        except Exception as e:
            logger.error(f"Silence generation failed: {e}")
        return None
