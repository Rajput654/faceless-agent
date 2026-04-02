"""
agents/script_deduplicator.py

Maintains a persistent registry of all scripts used on the YouTube channel.
Before a new script is accepted, it is compared against the registry.
If a duplicate (or near-duplicate) is detected, the script is rejected
so the caller can generate a fresh one.

HOW IT WORKS
────────────
1. On first run (or when explicitly refreshed), it fetches all uploaded
   video titles + descriptions from YouTube via the Data API v3 and
   seeds the local registry (scripts_registry.json).

2. Every time ScriptWriterAgent produces a script, this agent checks:
     a) Exact title match  (normalised, case-insensitive)
     b) Hook similarity    (SequenceMatcher ratio >= HOOK_SIMILARITY_THRESHOLD)
     c) Script body similarity (SequenceMatcher ratio >= BODY_SIMILARITY_THRESHOLD)

3. If any check triggers, is_duplicate() returns True so the caller retries.

4. When a script passes, register_script() saves it to the registry so
   future runs can detect it even without a live YouTube API call.

REGISTRY FILE
─────────────
Location:  scripts_registry.json  (project root, committed to .gitignore)
Structure:
  {
    "version": 1,
    "channel_fetched_at": "<ISO timestamp or null>",
    "entries": [
      {
        "video_id":   "video_001",
        "title":      "...",
        "hook":       "...",
        "script_hash": "<sha256 of normalised script body>",
        "script_snippet": "first 120 chars of script",
        "registered_at": "<ISO timestamp>",
        "source":     "youtube | local"
      },
      ...
    ]
  }

CONFIGURATION (via environment variables)
──────────────────────────────────────────
SCRIPT_REGISTRY_PATH          path to registry JSON  (default: scripts_registry.json)
HOOK_SIMILARITY_THRESHOLD     float 0-1              (default: 0.82)
BODY_SIMILARITY_THRESHOLD     float 0-1              (default: 0.75)
REGISTRY_REFRESH_HOURS        hours between auto-refreshes from YouTube (default: 24)
MAX_DEDUP_RETRIES             how many times to retry script gen before giving up (default: 3)
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

REGISTRY_PATH              = os.environ.get("SCRIPT_REGISTRY_PATH", "scripts_registry.json")
HOOK_SIMILARITY_THRESHOLD  = float(os.environ.get("HOOK_SIMILARITY_THRESHOLD",  "0.82"))
BODY_SIMILARITY_THRESHOLD  = float(os.environ.get("BODY_SIMILARITY_THRESHOLD",  "0.75"))
REGISTRY_REFRESH_HOURS     = int(os.environ.get("REGISTRY_REFRESH_HOURS",       "24"))
MAX_DEDUP_RETRIES          = int(os.environ.get("MAX_DEDUP_RETRIES",             "3"))

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    import re
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _sha256(text: str) -> str:
    return hashlib.sha256(_normalise(text).encode()).hexdigest()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Main class ───────────────────────────────────────────────────────────────

class ScriptDeduplicatorAgent:
    """
    Drop-in deduplication guard for ScriptWriterAgent.

    Usage in VideoWorkflow (or wherever ScriptWriterAgent is called):

        from agents.script_deduplicator import ScriptDeduplicatorAgent

        deduplicator = ScriptDeduplicatorAgent(config)
        deduplicator.refresh_from_youtube_if_stale()   # once per batch

        for attempt in range(MAX_DEDUP_RETRIES + 1):
            script_data = script_writer.run(topic_brief, video_id)
            if not deduplicator.is_duplicate(script_data):
                deduplicator.register_script(script_data, video_id)
                break
            logger.warning(f"Duplicate script detected — regenerating (attempt {attempt+1})")
        else:
            logger.error("Could not generate a unique script after retries")
    """

    def __init__(self, config: dict):
        self.config         = config
        self.registry_path  = Path(REGISTRY_PATH)
        self._registry: dict = self._load_registry()

    # ── Registry I/O ─────────────────────────────────────────────────────────

    def _load_registry(self) -> dict:
        if self.registry_path.exists():
            try:
                with open(self.registry_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(
                    f"ScriptDeduplicator: loaded registry "
                    f"({len(data.get('entries', []))} entries) "
                    f"from {self.registry_path}"
                )
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Registry file corrupt ({e}) — starting fresh")

        return {
            "version":            1,
            "channel_fetched_at": None,
            "entries":            [],
        }

    def _save_registry(self):
        try:
            with open(self.registry_path, "w", encoding="utf-8") as f:
                json.dump(self._registry, f, indent=2, ensure_ascii=False)
        except OSError as e:
            logger.error(f"ScriptDeduplicator: could not save registry: {e}")

    # ── YouTube channel fetch ─────────────────────────────────────────────────

    def refresh_from_youtube_if_stale(self) -> int:
        """
        Fetch the channel's uploaded videos from YouTube and seed the registry.
        Only fetches if the registry is older than REGISTRY_REFRESH_HOURS hours
        OR if it has never been fetched.

        Returns the number of new entries added.
        """
        fetched_at = self._registry.get("channel_fetched_at")
        if fetched_at:
            try:
                last = datetime.fromisoformat(fetched_at)
                age  = datetime.now(timezone.utc) - last
                if age < timedelta(hours=REGISTRY_REFRESH_HOURS):
                    logger.info(
                        f"ScriptDeduplicator: registry is fresh "
                        f"(fetched {age.seconds // 3600}h ago, refresh every {REGISTRY_REFRESH_HOURS}h)"
                    )
                    return 0
            except ValueError:
                pass

        logger.info("ScriptDeduplicator: fetching YouTube channel history...")
        videos = self._fetch_channel_videos()
        if not videos:
            logger.warning(
                "ScriptDeduplicator: no YouTube videos fetched "
                "(check YOUTUBE_CLIENT_ID_A / YOUTUBE_REFRESH_TOKEN_A)"
            )
            return 0

        added = 0
        existing_hashes = {e["script_hash"] for e in self._registry["entries"]}

        for v in videos:
            title       = v.get("title", "")
            description = v.get("description", "")
            yt_video_id = v.get("yt_video_id", "")

            # We don't have the original script — use title + description as proxy
            proxy_script = f"{title}. {description}"
            h            = _sha256(proxy_script)

            if h in existing_hashes:
                continue

            entry = {
                "video_id":       yt_video_id,
                "title":          title,
                "hook":           description[:120] if description else title,
                "script_hash":    h,
                "script_snippet": proxy_script[:120],
                "registered_at":  _now_iso(),
                "source":         "youtube",
            }
            self._registry["entries"].append(entry)
            existing_hashes.add(h)
            added += 1

        self._registry["channel_fetched_at"] = _now_iso()
        self._save_registry()

        logger.success(
            f"ScriptDeduplicator: channel fetch complete — "
            f"{added} new entries added ({len(videos)} videos total)"
        )
        return added

    def _fetch_channel_videos(self) -> list:
        """
        Fetch all uploaded video titles + descriptions from the YouTube
        channel associated with project A credentials.

        Uses the YouTube Data API v3:
          1. channels.list → get the "uploads" playlist ID
          2. playlistItems.list (paginated) → get all video IDs + snippets

        Returns a list of dicts with keys: yt_video_id, title, description.
        """
        try:
            import requests
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request as GoogleRequest

            client_id     = os.environ.get("YOUTUBE_CLIENT_ID_A", "")
            client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET_A", "")
            refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN_A", "")

            if not all([client_id, client_secret, refresh_token]):
                logger.warning(
                    "ScriptDeduplicator: YouTube credentials not set "
                    "(YOUTUBE_CLIENT_ID_A / YOUTUBE_CLIENT_SECRET_A / YOUTUBE_REFRESH_TOKEN_A). "
                    "Skipping channel fetch — registry will be local-only."
                )
                return []

            creds = Credentials(
                token         = None,
                refresh_token = refresh_token,
                client_id     = client_id,
                client_secret = client_secret,
                token_uri     = "https://oauth2.googleapis.com/token",
            )
            creds.refresh(GoogleRequest())
            access_token = creds.token

            headers = {"Authorization": f"Bearer {access_token}"}

            # Step 1: Get uploads playlist ID
            ch_resp = requests.get(
                f"{YOUTUBE_API_BASE}/channels",
                params={"part": "contentDetails", "mine": "true"},
                headers=headers,
                timeout=15,
            )
            ch_resp.raise_for_status()
            ch_data = ch_resp.json()

            items = ch_data.get("items", [])
            if not items:
                logger.warning("ScriptDeduplicator: no channel found for these credentials")
                return []

            uploads_playlist_id = (
                items[0]
                .get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads", "")
            )
            if not uploads_playlist_id:
                logger.warning("ScriptDeduplicator: could not find uploads playlist ID")
                return []

            logger.info(f"ScriptDeduplicator: uploads playlist = {uploads_playlist_id}")

            # Step 2: Paginate through playlistItems
            videos   = []
            page_token = None
            page_num   = 0

            while True:
                page_num += 1
                params = {
                    "part":       "snippet",
                    "playlistId": uploads_playlist_id,
                    "maxResults": 50,
                }
                if page_token:
                    params["pageToken"] = page_token

                pl_resp = requests.get(
                    f"{YOUTUBE_API_BASE}/playlistItems",
                    params=params,
                    headers=headers,
                    timeout=15,
                )
                pl_resp.raise_for_status()
                pl_data = pl_resp.json()

                for item in pl_data.get("items", []):
                    snippet    = item.get("snippet", {})
                    yt_video_id = snippet.get("resourceId", {}).get("videoId", "")
                    title       = snippet.get("title", "")
                    description = snippet.get("description", "")

                    if title and title != "Deleted video" and title != "Private video":
                        videos.append({
                            "yt_video_id": yt_video_id,
                            "title":       title,
                            "description": description[:500],
                        })

                page_token = pl_data.get("nextPageToken")
                if not page_token:
                    break

                logger.info(f"ScriptDeduplicator: fetched page {page_num} ({len(videos)} videos so far)")

            logger.info(f"ScriptDeduplicator: total videos on channel = {len(videos)}")
            return videos

        except ImportError:
            logger.warning(
                "ScriptDeduplicator: google-auth not installed. "
                "Run: pip install google-auth google-auth-oauthlib"
            )
            return []
        except Exception as e:
            logger.error(f"ScriptDeduplicator: YouTube fetch failed: {e}")
            return []

    # ── Duplicate detection ───────────────────────────────────────────────────

    def is_duplicate(self, script_data: dict) -> bool:
        """
        Returns True if the script is too similar to an existing entry.

        Checks (in order, short-circuits on first match):
          1. Exact normalised title match
          2. Hook similarity >= HOOK_SIMILARITY_THRESHOLD
          3. Script body similarity >= BODY_SIMILARITY_THRESHOLD
          4. Script body SHA-256 hash exact match
        """
        title        = script_data.get("title", "")
        hook         = script_data.get("hook", "")
        script_body  = script_data.get("script", "")
        script_hash  = _sha256(script_body) if script_body else ""
        norm_title   = _normalise(title)

        for entry in self._registry["entries"]:
            entry_title = _normalise(entry.get("title", ""))
            entry_hook  = entry.get("hook", "")
            entry_hash  = entry.get("script_hash", "")

            # Check 1: exact title
            if norm_title and entry_title and norm_title == entry_title:
                logger.warning(
                    f"[Dedup] EXACT TITLE MATCH: '{title}' already exists "
                    f"(registered: {entry.get('registered_at', '?')}, "
                    f"source: {entry.get('source', '?')})"
                )
                return True

            # Check 2: hook similarity
            if hook and entry_hook:
                hook_sim = _similarity(hook, entry_hook)
                if hook_sim >= HOOK_SIMILARITY_THRESHOLD:
                    logger.warning(
                        f"[Dedup] SIMILAR HOOK ({hook_sim:.2f} >= {HOOK_SIMILARITY_THRESHOLD}): "
                        f"'{hook[:60]}' ≈ '{entry_hook[:60]}' "
                        f"(title: '{entry.get('title', '?')}')"
                    )
                    return True

            # Check 3: script body similarity (only if both are available)
            entry_snippet = entry.get("script_snippet", "")
            if script_body and entry_snippet and len(entry_snippet) > 30:
                body_sim = _similarity(script_body[:300], entry_snippet[:300])
                if body_sim >= BODY_SIMILARITY_THRESHOLD:
                    logger.warning(
                        f"[Dedup] SIMILAR BODY ({body_sim:.2f} >= {BODY_SIMILARITY_THRESHOLD}): "
                        f"title='{title}' ≈ '{entry.get('title', '?')}'"
                    )
                    return True

            # Check 4: exact hash
            if script_hash and entry_hash and script_hash == entry_hash:
                logger.warning(
                    f"[Dedup] EXACT HASH MATCH: '{title}' has identical script body "
                    f"to '{entry.get('title', '?')}'"
                )
                return True

        return False

    # ── Registration ─────────────────────────────────────────────────────────

    def register_script(self, script_data: dict, video_id: str):
        """
        Add a successfully generated (and accepted) script to the registry.
        Call this AFTER is_duplicate() returns False and the video is produced.
        """
        title       = script_data.get("title", "")
        hook        = script_data.get("hook", "")
        script_body = script_data.get("script", "")

        entry = {
            "video_id":       video_id,
            "title":          title,
            "hook":           hook,
            "script_hash":    _sha256(script_body) if script_body else "",
            "script_snippet": script_body[:120] if script_body else "",
            "registered_at":  _now_iso(),
            "source":         "local",
        }

        self._registry["entries"].append(entry)
        self._save_registry()

        logger.info(
            f"ScriptDeduplicator: registered '{title}' "
            f"(total: {len(self._registry['entries'])} entries)"
        )

    # ── Stats / debug ─────────────────────────────────────────────────────────

    def stats(self) -> dict:
        entries  = self._registry.get("entries", [])
        youtube  = [e for e in entries if e.get("source") == "youtube"]
        local    = [e for e in entries if e.get("source") == "local"]
        return {
            "total":              len(entries),
            "from_youtube":       len(youtube),
            "from_local":         len(local),
            "channel_fetched_at": self._registry.get("channel_fetched_at"),
            "registry_path":      str(self.registry_path.absolute()),
        }

    def clear_local_entries(self):
        """Remove local (non-YouTube) entries — useful for testing."""
        before = len(self._registry["entries"])
        self._registry["entries"] = [
            e for e in self._registry["entries"] if e.get("source") != "local"
        ]
        after = len(self._registry["entries"])
        self._save_registry()
        logger.info(f"ScriptDeduplicator: cleared {before - after} local entries")
