"""
mcp_servers/video_fetcher.py

Fetches real MP4 stock video clips from:
  1. Pexels Videos API  (free, uses PEXELS_API_KEY)
  2. Pixabay Videos API (free, uses PIXABAY_API_KEY)

These replace static photos so the final output is a real video
composed of moving footage rather than a Ken-Burns slideshow.
"""
import os
import random
import requests
from pathlib import Path
from loguru import logger


# Niche → search terms that actually return good portrait-oriented clips
NICHE_VIDEO_QUERIES = {
    "motivation": [
        "success motivation", "running sunrise", "gym workout",
        "mountain summit", "city hustle", "determination",
    ],
    "horror": [
        "dark forest night", "abandoned house", "fog mystery",
        "thunderstorm night", "haunted shadows", "creepy door",
    ],
    "reddit_story": [
        "city life people", "coffee shop candid", "phone texting",
        "everyday life", "neighbourhood street", "office drama",
    ],
    "brainrot": [
        "colorful abstract", "neon lights city", "fast traffic timelapse",
        "internet scrolling", "video game screen", "chaotic crowd",
    ],
    "finance": [
        "stock market charts", "business meeting", "money wealth",
        "city skyscrapers", "laptop working", "financial graphs",
    ],
}

EMOTION_VIDEO_QUERIES = {
    "inspiration":  "sunrise achievement success",
    "fear":         "dark fog night horror",
    "shock":        "dramatic storm lightning",
    "curiosity":    "mysterious light discovery",
    "urgency":      "fast city rush traffic",
    "amusement":    "colorful fun celebration",
    "chaos":        "energetic crowd neon",
}


class VideoFetcherMCPServer:
    """Fetches short MP4 clips suitable for YouTube Shorts (portrait 9:16)."""

    def __init__(self):
        self.pexels_key  = os.environ.get("PEXELS_API_KEY", "")
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY", "")

    def fetch_clips(
        self,
        query:       str,
        output_paths: list,
        niche:       str = "motivation",
        emotion:     str = "inspiration",
        min_duration: int = 5,
        max_duration: int = 15,
    ) -> dict:
        """
        Fetch MP4 clips for each output path.
        Returns {"success": bool, "clip_paths": [str, ...], "source": str}
        """
        results = []
        queries = self._build_query_list(query, niche, emotion)

        for i, path in enumerate(output_paths):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            clip = None

            for q in queries:
                # Try Pexels first (better portrait content)
                if self.pexels_key:
                    clip = self._fetch_pexels_video(q, path, i, min_duration, max_duration)
                    if clip:
                        break

                # Try Pixabay
                if self.pixabay_key:
                    clip = self._fetch_pixabay_video(q, path, i, min_duration, max_duration)
                    if clip:
                        break

            if clip:
                results.append(clip)
            else:
                logger.warning(f"No clip found for slot {i} — will use image fallback")
                results.append(None)

        successful = [r for r in results if r is not None]
        return {
            "success":    len(successful) > 0,
            "clip_paths": [r["path"] if r else None for r in results],
            "clips":      results,
        }

    def _build_query_list(self, base_query: str, niche: str, emotion: str) -> list:
        queries = []
        if base_query:
            queries.append(base_query)
        emotion_q = EMOTION_VIDEO_QUERIES.get(emotion, "")
        if emotion_q:
            queries.append(emotion_q)
        queries.extend(NICHE_VIDEO_QUERIES.get(niche, ["cinematic background"]))
        # deduplicate while preserving order
        seen = set()
        unique = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                unique.append(q)
        return unique

    # ── Pexels Videos API ─────────────────────────────────────────────────────

    def _fetch_pexels_video(self, query, path, index, min_dur, max_dur):
        try:
            headers = {"Authorization": self.pexels_key}
            params = {
                "query":       query,
                "orientation": "portrait",
                "size":        "medium",
                "per_page":    15,
                "page":        (index // 15) + 1,
            }
            resp = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers, params=params, timeout=15
            )
            resp.raise_for_status()
            videos = resp.json().get("videos", [])
            if not videos:
                return None

            # Pick a clip that fits our duration window
            random.shuffle(videos)
            for video in videos:
                duration = video.get("duration", 0)
                if not (min_dur <= duration <= max_dur):
                    continue

                # Find the best portrait-oriented file (HD preferred)
                video_files = video.get("video_files", [])
                portrait_files = [
                    f for f in video_files
                    if f.get("width", 9999) < f.get("height", 0)  # portrait check
                ]
                if not portrait_files:
                    portrait_files = video_files  # fallback: take any

                # Sort by quality: prefer hd, then sd
                portrait_files.sort(
                    key=lambda f: (
                        1 if f.get("quality") == "hd" else
                        2 if f.get("quality") == "sd" else 3
                    )
                )
                if not portrait_files:
                    continue

                video_url = portrait_files[0].get("link", "")
                if not video_url:
                    continue

                clip_resp = requests.get(video_url, timeout=60, stream=True)
                clip_resp.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in clip_resp.iter_content(chunk_size=1024 * 64):
                        f.write(chunk)

                size = os.path.getsize(path)
                if size < 50_000:
                    os.remove(path)
                    continue

                logger.success(f"Pexels video ✅  {query!r} → {path} ({size/1024:.0f} KB, {duration}s)")
                return {
                    "path":     path,
                    "source":   "pexels",
                    "duration": duration,
                    "query":    query,
                }

            return None
        except Exception as e:
            logger.warning(f"Pexels video fetch failed ({query!r}): {e}")
            return None

    # ── Pixabay Videos API ────────────────────────────────────────────────────

    def _fetch_pixabay_video(self, query, path, index, min_dur, max_dur):
        try:
            params = {
                "key":        self.pixabay_key,
                "q":          query,
                "video_type": "film",
                "per_page":   20,
                "page":       1,
                "safesearch": "true",
            }
            resp = requests.get(
                "https://pixabay.com/api/videos/",
                params=params, timeout=15
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
            if not hits:
                return None

            random.shuffle(hits)
            for hit in hits:
                duration = hit.get("duration", 0)
                if not (min_dur <= duration <= max_dur):
                    continue

                videos = hit.get("videos", {})
                # Prefer medium quality for speed, fall back to small
                for quality in ("medium", "small", "large"):
                    vdata = videos.get(quality, {})
                    video_url = vdata.get("url", "")
                    if video_url:
                        break
                if not video_url:
                    continue

                clip_resp = requests.get(video_url, timeout=60, stream=True)
                clip_resp.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in clip_resp.iter_content(chunk_size=1024 * 64):
                        f.write(chunk)

                size = os.path.getsize(path)
                if size < 50_000:
                    os.remove(path)
                    continue

                logger.success(f"Pixabay video ✅  {query!r} → {path} ({size/1024:.0f} KB, {duration}s)")
                return {
                    "path":     path,
                    "source":   "pixabay",
                    "duration": duration,
                    "query":    query,
                }

            return None
        except Exception as e:
            logger.warning(f"Pixabay video fetch failed ({query!r}): {e}")
            return None
