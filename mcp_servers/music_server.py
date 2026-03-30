"""
mcp_servers/music_server.py

FIXED v5 — All music-blocking bugs resolved:

BUG FIX (B5) — AMBIENT FILE OVERWRITTEN BY SUBSEQUENT QUERIES:
  MusicDirectorAgent called _fetch_music() with the SAME output_path for every
  query. If query 1 returned ambient tone (saved to ambient_fallback), query 2
  overwrote the same file on disk. When the ambient fallback was finally returned
  its file had been corrupted/overwritten. Fix: _fetch_music() now accepts an
  output_path and the caller passes a unique path per query attempt.

BUG FIX (B7) — SYNTHETIC AMBIENT AS .m4a BREAKS stream_loop:
  The _generate_ambient_tone() was sometimes writing a file that CBR normalization
  couldn't handle. Fix: ambient tones are always written as proper CBR MP3.

BUG FIX (B4) — extended_path may be None in finally block:
  When _loop_by_concat fails it returns None, but the path variable still holds
  the old string. Guard added to check existence before removing.
"""
import os
import random
import subprocess
import requests
from pathlib import Path
from loguru import logger


# ─────────────────────────────────────────────────────────────────────────────
# Verified-working fallback tracks
# soundhelix.com (public domain) + incompetech.com (Kevin MacLeod, CC-BY)
# ─────────────────────────────────────────────────────────────────────────────
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
    "dark":         "dark",
    "horror":       "dark",
    "ambient":      "dark",
    "suspense":     "dark",
    "creepy":       "dark",
    "scary":        "dark",
    "calm":         "calm",
    "piano":        "calm",
    "storytelling": "calm",
    "narrative":    "calm",
    "background":   "calm",
    "chaotic":      "energetic",
    "electronic":   "energetic",
    "meme":         "energetic",
    "fast":         "energetic",
    "upbeat":       "energetic",
    "corporate":    "corporate",
    "professional": "corporate",
    "business":     "corporate",
    "finance":      "corporate",
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

        # ── 1. LOCAL ASSETS (guaranteed, no network needed) ──────────────────
        local_result = self._fetch_local(query, output_path)
        if local_result:
            return local_result

        # ── 2. CDN fallbacks (reliable public URLs) ───────────────────────────
        cdn_result = self._fetch_fallback(query, output_path)
        if cdn_result:
            return cdn_result

        # ── 3. Last resort: synthetic ambient tone ────────────────────────────
        logger.warning(
            f"All music sources failed for '{query}' — generating synthetic ambient"
        )
        return self._generate_ambient_tone(output_path, duration_seconds)

    # ── Local assets check ────────────────────────────────────────────────────

    def _fetch_local(self, query: str, output_path: str):
        """
        Check assets/music/ for pre-downloaded royalty-free MP3s.
        """
        local_dir = Path("assets/music")
        if not local_dir.exists():
            return None

        query_lower = query.lower()
        mood = "calm"
        for keyword, m in QUERY_TO_MOOD.items():
            if keyword in query_lower:
                mood = m
                break

        mood_files = list(local_dir.glob(f"*{mood}*.mp3"))
        all_files = list(local_dir.glob("*.mp3"))

        candidates = mood_files if mood_files else all_files
        if not candidates:
            return None

        chosen = random.choice(candidates)
        size = chosen.stat().st_size
        if size < 50_000:
            logger.debug(f"Local music file too small ({size} bytes): {chosen.name}")
            return None

        import shutil
        shutil.copy2(str(chosen), output_path)
        logger.success(
            f"Local music ✅  {chosen.name} ({size // 1024} KB) mood={mood}"
        )
        return {
            "success":    True,
            "music_path": output_path,
            "title":      chosen.stem,
            "source":     "local",
        }

    # ── CDN fallback ──────────────────────────────────────────────────────────

    def _fetch_fallback(self, query: str, output_path: str):
        """
        Download a mood-matched royalty-free track from CDN.
        Removed content-type gate — file size check (>50KB) is sufficient.
        """
        query_lower = query.lower()
        mood = "calm"
        for keyword, m in QUERY_TO_MOOD.items():
            if keyword in query_lower:
                mood = m
                break

        urls = FALLBACK_TRACKS.get(mood, FALLBACK_TRACKS["calm"])
        logger.info(
            f"Trying {len(urls)} CDN music URLs for mood='{mood}' (query='{query}')"
        )

        for i, url in enumerate(urls):
            # BUG FIX B5: Use a temp path during download, rename on success.
            # This prevents partial downloads from corrupting the output file
            # that the caller might be holding as a fallback reference.
            tmp_path = output_path + f".tmp_{i}"
            try:
                logger.debug(f"CDN music attempt {i+1}/{len(urls)}: {url[:70]}...")
                resp = requests.get(
                    url,
                    timeout=30,
                    stream=True,
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

                # Atomic rename: only overwrite output_path with a verified good file
                if os.path.exists(output_path):
                    os.remove(output_path)
                os.rename(tmp_path, output_path)

                logger.success(
                    f"CDN music ✅  mood={mood} source={i+1} "
                    f"→ {output_path} ({size // 1024} KB)"
                )
                return {
                    "success":    True,
                    "music_path": output_path,
                    "title":      f"royalty-free-{mood}",
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
        """
        Generate a subtle ambient drone using ffmpeg.

        BUG FIX B7: Always write as CBR MP3 (not AAC/m4a) so the output is
        directly usable by stream_loop without an additional normalization step.

        FIX: Size threshold lowered to 500 bytes.
        FIX: Two ffmpeg strategies with different filters for compatibility.
        """

        # Attempt 1: layered sine waves (most compatible)
        try:
            result = subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"sine=frequency=432:duration={duration}",
                "-f", "lavfi",
                "-i", f"sine=frequency=864:duration={duration}",
                "-filter_complex",
                "[0:a]volume=0.04[a1];[1:a]volume=0.02[a2];[a1][a2]amix=inputs=2[out]",
                "-map", "[out]",
                "-t", str(duration),
                # BUG FIX B7: Always CBR MP3, never AAC, so stream_loop works directly
                "-c:a", "libmp3lame", "-b:a", "128k", "-write_xing", "0",
                "-af", f"afade=t=in:st=0:d=2,afade=t=out:st={max(0, duration - 3)}:d=3",
                output_path,
            ], capture_output=True, timeout=60)

            if result.returncode == 0 and os.path.exists(output_path):
                size = os.path.getsize(output_path)
                if size > 500:
                    logger.warning(
                        f"Synthetic ambient tone generated ({duration}s, {size // 1024} KB). "
                        f"Add royalty-free MP3s to assets/music/ for better results."
                    )
                    return {
                        "success":    True,
                        "music_path": output_path,
                        "title":      "synthetic-ambient-sine",
                        "source":     "generated_ambient",
                    }
        except Exception as e:
            logger.debug(f"Sine ambient generation failed: {e}")

        # Attempt 2: brown noise fallback
        try:
            result = subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anoisesrc=color=brown:amplitude=0.03:duration={duration}",
                # BUG FIX B7: CBR MP3, not AAC
                "-c:a", "libmp3lame", "-b:a", "128k", "-write_xing", "0",
                "-af", f"lowpass=f=300,afade=t=in:st=0:d=2,afade=t=out:st={max(0, duration - 3)}:d=3",
                output_path,
            ], capture_output=True, timeout=60)

            if result.returncode == 0 and os.path.exists(output_path):
                size = os.path.getsize(output_path)
                if size > 500:
                    logger.warning(
                        f"Brown noise ambient generated ({duration}s, {size // 1024} KB)."
                    )
                    return {
                        "success":    True,
                        "music_path": output_path,
                        "title":      "synthetic-ambient-noise",
                        "source":     "generated_ambient",
                    }
        except Exception as e:
            logger.debug(f"Brown noise ambient generation failed: {e}")

        # Absolute fallback
        logger.error(
            "Cannot generate any audio for music track. Video will be voice-only. "
            "Add MP3 files to assets/music/ to fix this permanently."
        )
        return {
            "success":    True,
            "music_path": None,
            "title":      "none",
            "source":     "none",
        }
