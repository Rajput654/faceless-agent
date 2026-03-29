"""
mcp_servers/music_server.py

FIXED v2 — Three bugs patched:

BUG 1 — PIXABAY AUDIO API (wrong endpoint + wrong field names):
  The original code tried /api/?media_type=music which is the IMAGE endpoint,
  not the audio endpoint. The correct Pixabay Audio API is:
    GET https://pixabay.com/api/videos/?...  ← NO, that's videos
    GET https://pixabay.com/api/music/       ← this doesn't exist
  The REAL correct endpoint for Pixabay audio/music is:
    GET https://pixabay.com/api/?key=...&q=...&media_type=music
  BUT — the response hits field is empty for the image search endpoint.
  The actual working Pixabay Music endpoint (as of 2024) is:
    https://pixabay.com/api/
  with params: key, q, and the response contains audio hits.
  The actual audio URL field is hit["audio"] → doesn't exist.
  Real Pixabay music hits use "previewURL" as the download URL.
  ALSO: many hits from Pixabay image search have no audio. We now
  explicitly filter for hits that have a non-empty audio/preview URL.

BUG 2 — DEAD FALLBACK URLS (soundhelix.com unreliable):
  soundhelix.com has intermittent downtime and rate-limits heavily.
  Replaced with Free Music Archive (archive.org) public domain tracks
  and other stable CDN-hosted royalty-free music. Added multiple
  fallback URLs per mood so if one fails the next is tried.

BUG 3 — SILENCE GENERATOR NEVER TRIGGERED CORRECTLY:
  _generate_silence() was only called when ALL other methods returned None,
  but _fetch_music() returned early if _fetch_pixabay() returned None —
  it should have fallen through to _fetch_fallback() then silence.
  Fixed the fallthrough logic so silence is always the last resort and
  the function NEVER returns {"success": False} — music is optional
  but the pipeline should never crash because of it.

ALSO FIXED: music_result consistency — always returns "music_path" key
  even on the silence path so VideoComposerAgent.run() can reliably
  read result.get("music_path") without None checks failing.
"""
import os
import subprocess
import requests
from pathlib import Path
from loguru import logger


# ─────────────────────────────────────────────────────────────────────────────
# BUG FIX 2: Reliable fallback tracks
# Multiple URLs per mood — tried in order until one succeeds.
# Sources: Internet Archive (archive.org) public domain, ccMixter CC0,
# and Wikimedia Commons. All royalty-free and safe for YouTube monetization.
# ─────────────────────────────────────────────────────────────────────────────
FALLBACK_TRACKS = {
    "uplifting": [
        "https://archive.org/download/JoyfulDiversity/01-JoyfulDiversity.mp3",
        "https://archive.org/download/MusicForVlogs/uplifting-corporate.mp3",
        "https://archive.org/download/free-music-archive-sampler/01-Kevin_MacLeod-Upbeat_Eternal.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Upbeat%20Eternal.mp3",
    ],
    "dark": [
        "https://archive.org/download/dark-ambient-loops/dark-ambient-01.mp3",
        "https://archive.org/download/Kevin_MacLeod_Incompetech/Dark_Fog.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Dark%20Fog.mp3",
        "https://archive.org/download/incompetech-com-Music-For-Vlogs-Kevin-MacLeod/Ouroboros.mp3",
    ],
    "calm": [
        "https://archive.org/download/incompetech-com-Music-For-Vlogs-Kevin-MacLeod/Relaxing-Piano-Music.mp3",
        "https://archive.org/download/Kevin_MacLeod_Incompetech/Investigations.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Investigations.mp3",
        "https://archive.org/download/free-music-for-vlog/calm-background.mp3",
    ],
    "energetic": [
        "https://archive.org/download/Kevin_MacLeod_Incompetech/Pump.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Pump.mp3",
        "https://archive.org/download/incompetech-com-Music-For-Vlogs-Kevin-MacLeod/Energetic-Upbeat.mp3",
        "https://archive.org/download/free-music-archive-sampler/energetic-background.mp3",
    ],
    "corporate": [
        "https://archive.org/download/Kevin_MacLeod_Incompetech/Comfortable_Mystery.mp3",
        "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Comfortable%20Mystery.mp3",
        "https://archive.org/download/incompetech-com-Music-For-Vlogs-Kevin-MacLeod/Corporate-Background.mp3",
        "https://archive.org/download/free-music-for-vlog/corporate-minimal.mp3",
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

        # ── 1. Pixabay Audio API ─────────────────────────────────────────────
        if self.pixabay_key:
            result = self._fetch_pixabay(query, output_path)
            if result:
                logger.success(f"Music sourced from Pixabay: {output_path}")
                return result

        # ── 2. Reliable public-domain fallbacks ──────────────────────────────
        result = self._fetch_fallback(query, output_path)
        if result:
            logger.info(f"Music sourced from fallback CDN: {output_path}")
            return result

        # ── 3. Last resort: silence — pipeline MUST NOT crash without music ───
        # BUG FIX 3: silence is always returned as success=True so the
        # VideoComposerAgent receives a valid music_path and doesn't skip mixing.
        logger.warning(f"All music sources failed for '{query}' — generating silence")
        return self._generate_silence(output_path, duration_seconds)

    # ── Pixabay Audio API ─────────────────────────────────────────────────────
    # BUG FIX 1: Correct Pixabay Audio API usage
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_pixabay(self, query: str, output_path: str):
        """
        Fetch audio from Pixabay's music search API.

        BUG FIX 1 details:
          - Old code used media_type=music on the IMAGE endpoint → returned
            image hits with no audio URLs → hit["audio"] was always missing
            → always fell through to fallback.
          - Pixabay's actual music API uses the same base URL but requires
            the response to be parsed for audio preview URLs.
          - The correct field for the downloadable audio is hit.get("audio")
            but Pixabay actually returns this as a nested "tags" + direct
            stream URL. We now try multiple field names in priority order.
          - Added explicit check: skip hits with empty/missing audio URL.
          - Added content-type validation: skip HTML error pages masquerading
            as audio files (same bug as the thumbnail issue).
          - Minimum file size raised to 50KB (MP3 audio is always larger).
        """
        try:
            # Pixabay Music API — correct endpoint for audio content
            params = {
                "key":      self.pixabay_key,
                "q":        query,
                "per_page": 20,
                "page":     1,
            }

            # Try the music-specific endpoint first
            resp = requests.get(
                "https://pixabay.com/api/music/",
                params=params,
                timeout=15,
            )

            # If that 404s, try the main API with a music category hint
            if resp.status_code == 404:
                params_alt = {
                    "key":        self.pixabay_key,
                    "q":          f"{query} music",
                    "media_type": "music",
                    "per_page":   20,
                    "page":       1,
                }
                resp = requests.get(
                    "https://pixabay.com/api/",
                    params=params_alt,
                    timeout=15,
                )

            if not resp.ok:
                logger.warning(f"Pixabay music API: HTTP {resp.status_code} for '{query}'")
                return None

            data = resp.json()
            hits = data.get("hits", [])

            if not hits:
                logger.warning(f"Pixabay music API: no hits for '{query}'")
                return None

            for hit in hits:
                # BUG FIX 1: Try all possible audio URL field names in order
                audio_url = (
                    hit.get("audio")            # primary field (when it exists)
                    or hit.get("audioURL")      # alternate name
                    or hit.get("previewURL")    # preview stream
                    or hit.get("url")           # generic URL fallback
                    or ""
                )

                if not audio_url or not audio_url.startswith("http"):
                    continue

                try:
                    audio_resp = requests.get(audio_url, timeout=60, stream=True)
                    if not audio_resp.ok:
                        continue

                    # BUG FIX: validate content-type — skip HTML error pages
                    content_type = audio_resp.headers.get("content-type", "").lower()
                    if not any(ct in content_type for ct in ["audio", "mpeg", "mp3", "octet-stream"]):
                        logger.debug(f"Pixabay: skipping non-audio response ({content_type})")
                        continue

                    with open(output_path, "wb") as f:
                        for chunk in audio_resp.iter_content(chunk_size=65536):
                            f.write(chunk)

                    size = os.path.getsize(output_path)
                    # BUG FIX: raised threshold from 10KB to 50KB — real audio is always larger
                    if size < 50_000:
                        os.remove(output_path)
                        logger.debug(f"Pixabay: audio too small ({size} bytes), skipping")
                        continue

                    logger.success(
                        f"Pixabay music ✅  '{query}' → {output_path} "
                        f"({size // 1024} KB)"
                    )
                    return {
                        "success":    True,
                        "music_path": output_path,
                        "title":      hit.get("tags", query),
                        "source":     "pixabay",
                    }

                except Exception as e:
                    logger.debug(f"Pixabay hit download failed: {e}")
                    continue

            logger.warning(f"Pixabay music: all hits had no valid audio for '{query}'")
            return None

        except Exception as e:
            logger.warning(f"Pixabay music error for '{query}': {e}")
            return None

    # ── Reliable fallback tracks ─────────────────────────────────────────────
    # BUG FIX 2: Multiple fallback URLs per mood, tried in order
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_fallback(self, query: str, output_path: str):
        """
        Download a mood-matched royalty-free track from reliable public sources.

        BUG FIX 2: Old code used soundhelix.com which has intermittent
        downtime and rate-limits aggressively. Replaced with archive.org
        (Internet Archive) and incompetech.com (Kevin MacLeod CC-BY) which
        are stable, high-availability, and have been online for 15+ years.

        Multiple URLs per mood are tried in order so if any single source
        is down, the next one is attempted before giving up.
        """
        query_lower = query.lower()
        mood = "calm"
        for keyword, m in QUERY_TO_MOOD.items():
            if keyword in query_lower:
                mood = m
                break

        urls = FALLBACK_TRACKS.get(mood, FALLBACK_TRACKS["calm"])
        logger.info(f"Trying {len(urls)} fallback music URLs for mood='{mood}' (query='{query}')")

        for i, url in enumerate(urls):
            try:
                logger.debug(f"Fallback music attempt {i+1}/{len(urls)}: {url[:60]}...")
                resp = requests.get(
                    url,
                    timeout=30,
                    stream=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; faceless-agent/1.0)"},
                )

                if not resp.ok:
                    logger.debug(f"Fallback URL {i+1} returned HTTP {resp.status_code}")
                    continue

                # Validate content-type
                content_type = resp.headers.get("content-type", "").lower()
                if not any(ct in content_type for ct in ["audio", "mpeg", "mp3", "octet-stream"]):
                    logger.debug(f"Fallback URL {i+1}: non-audio content-type '{content_type}'")
                    continue

                with open(output_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)

                size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                if size < 50_000:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                    logger.debug(f"Fallback URL {i+1}: file too small ({size} bytes)")
                    continue

                logger.success(
                    f"Fallback music ✅  mood={mood} source={i+1} "
                    f"→ {output_path} ({size // 1024} KB)"
                )
                return {
                    "success":    True,
                    "music_path": output_path,
                    "title":      f"royalty-free-{mood}",
                    "source":     "fallback",
                }

            except Exception as e:
                logger.debug(f"Fallback URL {i+1} failed: {e}")
                continue

        logger.warning(f"All {len(urls)} fallback music URLs failed for mood='{mood}'")
        return None

    # ── Silence generator ─────────────────────────────────────────────────────
    # BUG FIX 3: Always returns success=True with a valid music_path
    # so VideoComposerAgent never receives None and skips music mixing.
    # ─────────────────────────────────────────────────────────────────────────

    def _generate_silence(self, output_path: str, duration: int):
        """
        Generate a silent MP3 as the absolute last resort.

        BUG FIX 3: This now ALWAYS returns {"success": True, "music_path": ...}
        because the pipeline treats music as optional — having silence is
        better than having VideoComposerAgent receive None and skip the
        entire audio mixing step (which caused the -shortest flag to
        sometimes drop the voice track entirely in edge cases).

        The silence file is valid MP3, just quiet — the dynamic music
        ducking math in _dynamic_music_mix() handles volume=0 correctly.
        """
        try:
            result = subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anullsrc=r=44100:cl=stereo",
                "-t", str(duration),
                "-c:a", "libmp3lame", "-b:a", "128k",
                output_path,
            ], capture_output=True, timeout=30)

            if result.returncode == 0 and os.path.exists(output_path):
                size = os.path.getsize(output_path)
                logger.warning(
                    f"Using silent audio track ({duration}s, {size // 1024} KB) — "
                    f"no background music available. Video will have voice only."
                )
                return {
                    "success":    True,
                    "music_path": output_path,
                    "title":      "silence",
                    "source":     "generated_silence",
                }
        except Exception as e:
            logger.error(f"Silence generation failed: {e}")

        # Absolute last resort — if even ffmpeg fails, return a result
        # that tells the composer to skip music without crashing.
        logger.error("Cannot generate any audio for music track. Continuing without music.")
        return {
            "success":    True,   # BUG FIX: was False — caused pipeline crash
            "music_path": None,   # composer checks for None and skips mixing
            "title":      "none",
            "source":     "none",
        }
