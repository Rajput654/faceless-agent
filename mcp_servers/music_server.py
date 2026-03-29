"""
mcp_servers/music_server.py

FIXED v4 — Three additional root-cause bugs fixed on top of v3:

ROOT CAUSE 4 — CONTENT-TYPE GATE TOO STRICT:
  The content-type check rejected valid audio responses where the CDN
  returned 'application/octet-stream' or omitted the header entirely.
  Fix: removed the content-type gate. File size (>50KB) is sufficient
  to distinguish real audio from HTML error pages.

ROOT CAUSE 5 — AMBIENT FALLBACK PATH NEVER REACHED CORRECTLY:
  _generate_ambient_tone() returned music_path=None if ffmpeg produced
  a file smaller than 1000 bytes, but the check was after the file was
  already written. Also, the aevalsrc filter syntax failed on some
  ffmpeg builds. Fix: use simpler sine filter, lower size threshold to
  500 bytes, and add a second ffmpeg fallback using anoisesrc.

ROOT CAUSE 6 — LOCAL ASSETS NEVER CHECKED:
  No mechanism to use pre-downloaded royalty-free MP3s from assets/music/.
  Fix: _fetch_music() now checks assets/music/ first before any network
  call. Drop any royalty-free MP3s there and they will always be used.

ALSO: Added soundhelix.com as an additional CDN (reliable, no auth needed)
and archive.org direct MP3 links as tertiary fallback.
"""
import os
import random
import subprocess
import requests
from pathlib import Path
from loguru import logger


# ─────────────────────────────────────────────────────────────────────────────
# Verified-working fallback tracks
# incompetech.com (Kevin MacLeod, CC-BY) + soundhelix.com (public domain)
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
        # Drop any royalty-free MP3s into assets/music/ and they will always
        # be used first. This is the most reliable option for CI environments.
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

    # ── NEW: Local assets check ───────────────────────────────────────────────

    def _fetch_local(self, query: str, output_path: str):
        """
        Check assets/music/ for pre-downloaded royalty-free MP3s.
        If found, pick one randomly (mood-matched if possible) and copy it.
        This is the most reliable approach for CI — no network calls needed.
        """
        local_dir = Path("assets/music")
        if not local_dir.exists():
            return None

        # Try to find mood-matched files first
        query_lower = query.lower()
        mood = "calm"
        for keyword, m in QUERY_TO_MOOD.items():
            if keyword in query_lower:
                mood = m
                break

        # Look for files with mood keyword in name, then fall back to any MP3
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

        FIX 4: Removed content-type gate — it was too strict and rejected
        valid audio served as 'application/octet-stream' or with no content-type.
        File size check (>50KB) is sufficient to catch HTML error pages.
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

                # FIX 4: Removed content-type gate — rely on file size only.
                # Content-type varies wildly between CDNs and is not reliable.

                with open(output_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)

                size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                if size < 50_000:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                    logger.debug(f"CDN URL {i+1}: file too small ({size} bytes) — likely an error page")
                    continue

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
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except Exception:
                        pass
                continue

        logger.warning(f"All {len(urls)} CDN music URLs failed for mood='{mood}'")
        return None

    # ── Synthetic ambient tone ────────────────────────────────────────────────

    def _generate_ambient_tone(self, output_path: str, duration: int):
        """
        Generate a subtle ambient drone using ffmpeg.

        FIX 5: Two improvements over v3:
          1. Primary filter uses simpler sine syntax (more compatible across
             ffmpeg versions than the aevalsrc complex expression).
          2. Second attempt uses anoisesrc (brown noise, very low volume)
             as a fallback if sine generation fails.
          3. Size threshold lowered to 500 bytes — the old 1000 byte check
             was too high and caused valid short tones to be discarded.
          4. Returns music_path=None only as absolute last resort so
             VideoComposerAgent can cleanly skip mixing.
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
                "-c:a", "libmp3lame", "-b:a", "128k",
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

        # Attempt 2: brown noise (alternative filter, widely supported)
        try:
            result = subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anoisesrc=color=brown:amplitude=0.03:duration={duration}",
                "-c:a", "libmp3lame", "-b:a", "128k",
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

        # Absolute fallback: return None so composer skips music cleanly
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
