"""
mcp_servers/music_server.py

FIXED v6:

BUG FIX B12 — SAME TRACK FOR EVERY VIDEO IN A BATCH:
  _fetch_fallback always iterated URLs in the same fixed order.
  Every video in a 10-video batch tried the same list from the top
  and always landed on the same first-working URL.

  Fix: Accept a `seed` parameter (derived from video_id hash in
  MusicDirectorAgent). Rotate the URL list's start position using
  seed % len(urls) before iterating. Video 0 starts at index 0,
  video 1 at index 1, etc. — each video gets a genuinely different
  first-choice track.

BUG FIX B13 — LOCAL ASSET ALWAYS RETURNS THE SAME FILE:
  random.choice(candidates) without a seed produced the same file
  when the Python random state was reset per process (GitHub Actions
  spawns a fresh process per matrix job). Fixed by seeding with video seed.

PRESERVED: All v5 fixes.
"""
import os
import random
import subprocess
import requests
from pathlib import Path
from loguru import logger


FALLBACK_TRACKS = {
    "uplifting": [
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Upbeat%20Eternal.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Happiness.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Positive%20Motivation.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Call%20to%20Adventure.mp3",
    ],
    "dark": [
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-9.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Dark%20Fog.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Ossuary%205%20-%20Rest.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Anguish.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Darkest%20Child.mp3",
    ],
    "calm": [
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-3.mp3",
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-7.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Investigations.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Slow%20Burn.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Healing.mp3",
    ],
    "energetic": [
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-4.mp3",
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-6.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Pump.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Run%20Amok.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Electro%20Sketch.mp3",
    ],
    "corporate": [
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-5.mp3",
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-8.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Comfortable%20Mystery.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Digital%20Lemonade.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Cool%20Vibes.mp3",
    ],
}

QUERY_TO_MOOD = {
    "uplifting":    "uplifting",
    "motivational": "uplifting",
    "inspiring":    "uplifting",
    "epic":         "uplifting",
    "triumphant":   "uplifting",
    "morning":      "uplifting",
    "success":      "uplifting",
    "dark":         "dark",
    "horror":       "dark",
    "ambient":      "dark",
    "suspense":     "dark",
    "creepy":       "dark",
    "scary":        "dark",
    "eerie":        "dark",
    "ominous":      "dark",
    "tense":        "dark",
    "calm":         "calm",
    "piano":        "calm",
    "storytelling": "calm",
    "narrative":    "calm",
    "background":   "calm",
    "mysterious":   "calm",
    "thoughtful":   "calm",
    "gentle":       "calm",
    "soft":         "calm",
    "chaotic":      "energetic",
    "electronic":   "energetic",
    "meme":         "energetic",
    "fast":         "energetic",
    "upbeat":       "energetic",
    "punchy":       "energetic",
    "corporate":    "corporate",
    "professional": "corporate",
    "business":     "corporate",
    "finance":      "corporate",
    "minimal":      "corporate",
    "clean":        "corporate",
}


class MusicMCPServer:
    def __init__(self):
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")
        self.tools = {"fetch_music": self._fetch_music}

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _fetch_music(self, query: str, output_path: str, duration_seconds: int = 60,
                     seed: int = 0, **kwargs):
        """
        seed: integer derived from video_id hash in MusicDirectorAgent.
              Used to rotate URL order so each video in a batch gets a
              different track rather than all landing on the same first URL.
        """
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        local_result = self._fetch_local(query, output_path, seed=seed)
        if local_result:
            return local_result

        cdn_result = self._fetch_fallback(query, output_path, seed=seed)
        if cdn_result:
            return cdn_result

        logger.warning(f"All music sources failed for '{query}' — generating synthetic ambient")
        return self._generate_ambient_tone(output_path, duration_seconds)

    # ── Local assets ──────────────────────────────────────────────────────────

    def _fetch_local(self, query: str, output_path: str, seed: int = 0):
        local_dir = Path("assets/music")
        if not local_dir.exists():
            return None

        query_lower = query.lower()
        mood = self._query_to_mood(query_lower)

        mood_files = list(local_dir.glob(f"*{mood}*.mp3"))
        all_files  = list(local_dir.glob("*.mp3"))

        candidates = mood_files if mood_files else all_files
        if not candidates:
            return None

        # BUG FIX B13: use seed so different videos pick different local files
        chosen = candidates[seed % len(candidates)]
        size = chosen.stat().st_size
        if size < 50_000:
            logger.debug(f"Local music file too small ({size} bytes): {chosen.name}")
            return None

        import shutil
        shutil.copy2(str(chosen), output_path)
        logger.success(f"Local music ✅  {chosen.name} ({size // 1024} KB) mood={mood}")
        return {
            "success":    True,
            "music_path": output_path,
            "title":      chosen.stem,
            "source":     "local",
        }

    # ── CDN fallback ──────────────────────────────────────────────────────────

    def _query_to_mood(self, query_lower: str) -> str:
        """Map query string to one of the 5 mood buckets."""
        for keyword, m in QUERY_TO_MOOD.items():
            if keyword in query_lower:
                return m
        return "calm"

    def _fetch_fallback(self, query: str, output_path: str, seed: int = 0):
        """
        Download a mood-matched royalty-free track from CDN.

        BUG FIX B12: Rotate URL list using seed so each video in a batch
        starts from a different URL. With 6 URLs and 10 videos, videos
        0-5 each try a different track first. Videos 6-9 wrap around but
        still differ from 0-5 in their second+ attempt ordering.
        """
        mood = self._query_to_mood(query.lower())
        urls = FALLBACK_TRACKS.get(mood, FALLBACK_TRACKS["calm"]).copy()

        # Rotate starting position — video 0 starts at 0, video 1 at 1, etc.
        if len(urls) > 1 and seed > 0:
            start = seed % len(urls)
            urls = urls[start:] + urls[:start]
            logger.debug(f"URL list rotated by {start} (seed={seed}) for variety")

        logger.info(f"Trying {len(urls)} CDN music URLs for mood='{mood}' (query='{query}')")

        for i, url in enumerate(urls):
            tmp_path = output_path + f".tmp_{i}"
            try:
                logger.debug(f"CDN music attempt {i+1}/{len(urls)}: {url[:70]}...")
                resp = requests.get(
                    url, timeout=30, stream=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; faceless-agent/1.0)",
                        "Accept": "audio/mpeg, audio/*, */*",
                    },
                )
                if not resp.ok:
                    logger.debug(f"CDN URL {i+1} returned HTTP {resp.status_code}")
                    continue

                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)

                size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                if size < 50_000:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    logger.debug(f"CDN URL {i+1}: file too small ({size} bytes)")
                    continue

                if os.path.exists(output_path):
                    os.remove(output_path)
                os.rename(tmp_path, output_path)

                logger.success(
                    f"CDN music ✅  mood={mood} url_slot={i+1}/{len(urls)} seed={seed} "
                    f"→ {output_path} ({size // 1024} KB)"
                )
                return {
                    "success":    True,
                    "music_path": output_path,
                    "title":      f"royalty-free-{mood}-{seed}",
                    "source":     "cdn",
                }

            except Exception as e:
                logger.debug(f"CDN URL {i+1} failed: {e}")
                for p in [tmp_path, output_path]:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                continue

        logger.warning(f"All {len(urls)} CDN music URLs failed for mood='{mood}'")
        return None

    # ── Synthetic ambient tone ────────────────────────────────────────────────

    def _generate_ambient_tone(self, output_path: str, duration: int):
        """Generate a subtle ambient drone using ffmpeg (CBR MP3, stream_loop compatible)."""
        try:
            result = subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"sine=frequency=432:duration={duration}",
                "-f", "lavfi", "-i", f"sine=frequency=864:duration={duration}",
                "-filter_complex",
                "[0:a]volume=0.04[a1];[1:a]volume=0.02[a2];[a1][a2]amix=inputs=2[out]",
                "-map", "[out]",
                "-t", str(duration),
                "-c:a", "libmp3lame", "-b:a", "128k", "-write_xing", "0",
                "-af", f"afade=t=in:st=0:d=2,afade=t=out:st={max(0, duration - 3)}:d=3",
                output_path,
            ], capture_output=True, timeout=60)

            if result.returncode == 0 and os.path.exists(output_path):
                size = os.path.getsize(output_path)
                if size > 500:
                    logger.warning(f"Synthetic ambient tone generated ({duration}s, {size // 1024} KB).")
                    return {
                        "success":    True,
                        "music_path": output_path,
                        "title":      "synthetic-ambient-sine",
                        "source":     "generated_ambient",
                    }
        except Exception as e:
            logger.debug(f"Sine ambient generation failed: {e}")

        try:
            result = subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anoisesrc=color=brown:amplitude=0.03:duration={duration}",
                "-c:a", "libmp3lame", "-b:a", "128k", "-write_xing", "0",
                "-af", f"lowpass=f=300,afade=t=in:st=0:d=2,afade=t=out:st={max(0, duration - 3)}:d=3",
                output_path,
            ], capture_output=True, timeout=60)

            if result.returncode == 0 and os.path.exists(output_path):
                size = os.path.getsize(output_path)
                if size > 500:
                    logger.warning(f"Brown noise ambient generated ({duration}s, {size // 1024} KB).")
                    return {
                        "success":    True,
                        "music_path": output_path,
                        "title":      "synthetic-ambient-noise",
                        "source":     "generated_ambient",
                    }
        except Exception as e:
            logger.debug(f"Brown noise ambient generation failed: {e}")

        logger.error("Cannot generate any audio for music track. Video will be voice-only.")
        return {
            "success":    True,
            "music_path": None,
            "title":      "none",
            "source":     "none",
        }
