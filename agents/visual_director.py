"""
agents/visual_director.py

UPGRADED: Script-driven visual queries using Pass 1 extractor output.

Instead of one generic query (topic + niche + emotion), this agent now:
  1. Reads the `extracted` dict written by the script writer's Pass 1
     to get scene-specific descriptions (core_mystery, key_facts, twist)
  2. Generates 4-6 DISTINCT scene prompts — one per visual segment
  3. For horror/motivation/brainrot: prioritizes Pollinations AI images
     (custom, consistent, scene-matched) over generic Pexels stock footage
  4. For reddit_story/finance: keeps Pexels video clips (realistic look)
  5. Applies emotion-aware Ken Burns preset selection (not random)

The result: every visual directly illustrates what is being said in the
narration at that moment, instead of generic B-roll that might not match.
"""
import os
import re
import random
import requests
from pathlib import Path
from loguru import logger
from mcp_servers.image_server import ImageMCPServer
from mcp_servers.video_fetcher import VideoFetcherMCPServer


# ---------------------------------------------------------------------------
# Niches that benefit from AI-generated images (consistent, scene-matched)
# vs niches that need realistic stock footage
# ---------------------------------------------------------------------------
AI_IMAGE_NICHES  = {"horror", "motivation", "brainrot"}
STOCK_VIDEO_NICHES = {"reddit_story", "finance"}

# ---------------------------------------------------------------------------
# Emotion → Ken Burns motion preset
# (overrides the random selection in video_server.py)
# horror/fear   → slow zoom-in  (creeping dread)
# motivation    → zoom-out      (expanding possibility)
# brainrot      → fast pan      (chaotic energy)
# finance       → subtle zoom   (calm authority)
# reddit_story  → slow pan      (like reading a thread)
# ---------------------------------------------------------------------------
EMOTION_KB_PRESET = {
    "fear":        "slow_zoom_in",
    "inspiration": "zoom_out",
    "urgency":     "zoom_out",
    "chaos":       "fast_pan",
    "amusement":   "fast_pan",
    "curiosity":   "slow_zoom_in",
    "shock":       "slow_zoom_in",
    "default":     "slow_zoom_in",
}

# ---------------------------------------------------------------------------
# Niche visual style suffixes appended to AI image prompts
# ---------------------------------------------------------------------------
NICHE_IMAGE_STYLE = {
    "motivation":   "cinematic, golden hour, dramatic lighting, photorealistic, 4K, vertical",
    "horror":       "dark, moody, hyper-realistic, atmospheric fog, horror film still, vertical",
    "reddit_story": "candid, realistic, everyday life, natural lighting, vertical photo",
    "brainrot":     "neon colors, chaotic, surreal, internet aesthetic, vertical",
    "finance":      "clean, professional, minimalist, corporate, sharp lighting, vertical",
}

# ---------------------------------------------------------------------------
# Fallback generic queries when extractor data is unavailable
# ---------------------------------------------------------------------------
NICHE_FALLBACK_QUERIES = {
    "motivation": [
        "person standing on mountain summit at sunrise",
        "runner crossing finish line dramatic lighting",
        "lone figure walking through fog determination",
        "hands reaching toward sky golden hour",
    ],
    "horror": [
        "dark empty hallway house at night",
        "shadows on wall abandoned building",
        "door slightly open darkness beyond",
        "fog covered road trees at midnight",
    ],
    "reddit_story": [
        "person typing on phone coffee shop",
        "two people arguing apartment",
        "shocked face looking at phone screen",
        "person walking away city street",
    ],
    "brainrot": [
        "neon lights abstract chaos colorful",
        "person screaming funny surreal",
        "internet meme energy chaotic background",
        "brain explosion colorful abstract",
    ],
    "finance": [
        "stock market graph going up dramatic",
        "wallet with money close up",
        "person looking at laptop financial data",
        "coins and bills arranged neatly",
    ],
}


class VisualDirectorAgent:
    def __init__(self, config):
        self.config        = config
        self.image_server  = ImageMCPServer()
        self.video_fetcher = VideoFetcherMCPServer()
        self.image_config  = config.get("images", {})
        self.video_config  = config.get("video", {})

    def _get_niche(self) -> str:
        return os.environ.get(
            "NICHE",
            self.config.get("video", {}).get("niche", "motivation")
        )

    # ------------------------------------------------------------------
    # CORE UPGRADE: Build scene-specific prompts from Pass 1 extractor
    # ------------------------------------------------------------------
    def _build_scene_prompts(self, script: dict, niche: str, num_scenes: int) -> list:
        """
        Generate distinct visual prompts for each scene by reading the
        structured data produced by the script writer's Pass 1 extractor.

        Falls back to generic queries if extractor data is absent.
        """
        style_suffix = NICHE_IMAGE_STYLE.get(niche, "cinematic, photorealistic, vertical")
        topic_brief  = script.get("topic_brief", {})
        emotion      = script.get("emotion", "curiosity")

        # Try to get Pass 1 extractor data (stored in topic_brief by script writer)
        # The script writer stores it inside the script JSON under "topic_brief"
        extracted = script.get("_extracted", {})  # set by run() below if available

        # Build base topic keywords from title/topic
        title   = script.get("title", "")
        topic   = topic_brief.get("topic", title)
        keywords = " ".join(topic.split()[:4])

        prompts = []

        # ── Scene 1: The Hook visual (most dramatic / attention-grabbing) ──
        hook = script.get("hook", "")
        if hook:
            # Strip common filler words from hook to get visual keywords
            hook_clean = re.sub(
                r'\b(the|a|an|and|or|but|in|on|at|to|for|of|with|by|this|that|is|was|are|were|has|have|had|you|i|we|they|it)\b',
                '', hook.lower()
            ).strip()
            hook_clean = re.sub(r'\s+', ' ', hook_clean)[:60]
            prompts.append(f"{hook_clean}, {style_suffix}")

        # ── Scenes from key_facts (Pass 1 extractor output) ──
        key_facts = extracted.get("key_facts", [])
        for fact in key_facts[:2]:
            if fact and len(fact) > 5:
                fact_clean = re.sub(r'[^\w\s]', '', fact)[:80]
                prompts.append(f"{fact_clean}, {style_suffix}")

        # ── Scene from core_mystery ──
        core_mystery = extracted.get("core_mystery", "")
        if core_mystery:
            mystery_clean = re.sub(r'[^\w\s]', '', core_mystery)[:80]
            prompts.append(f"{mystery_clean}, {style_suffix}")

        # ── Scene for the twist ──
        twist = extracted.get("twist", "")
        if twist:
            twist_clean = re.sub(r'[^\w\s]', '', twist)[:60]
            prompts.append(f"{twist_clean}, dramatic reveal, {style_suffix}")

        # ── Fill remaining slots with niche fallbacks ──
        fallback_queries = NICHE_FALLBACK_QUERIES.get(niche, [f"{keywords}, {style_suffix}"])
        while len(prompts) < num_scenes:
            prompts.append(f"{fallback_queries[len(prompts) % len(fallback_queries)]}, {style_suffix}")

        # Trim to exact number needed
        prompts = prompts[:num_scenes]

        logger.info(f"Scene prompts generated ({len(prompts)}):")
        for i, p in enumerate(prompts):
            logger.debug(f"  Scene {i+1}: {p[:80]}...")

        return prompts

    # ------------------------------------------------------------------
    # Pollinations AI image fetcher (free, no API key, scene-matched)
    # ------------------------------------------------------------------
    def _fetch_pollinations_image(self, prompt: str, output_path: str,
                                   width: int = 1080, height: int = 1920,
                                   seed: int = None) -> bool:
        """
        Fetch a custom AI-generated image from Pollinations.
        Returns True if successful.
        """
        import urllib.parse
        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            _seed = seed if seed is not None else random.randint(0, 99999)
            encoded = urllib.parse.quote(prompt)
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width={width}&height={height}&seed={_seed}&nologo=true&enhance=true"
            )
            resp = requests.get(url, timeout=90)
            resp.raise_for_status()

            # Validate it's actually an image (not an error HTML page)
            content_type = resp.headers.get("content-type", "")
            if "image" not in content_type and len(resp.content) < 10_000:
                logger.warning(f"Pollinations returned non-image content for: {prompt[:50]}")
                return False

            with open(output_path, "wb") as f:
                f.write(resp.content)

            size = os.path.getsize(output_path)
            if size < 5_000:
                os.remove(output_path)
                return False

            logger.success(f"Pollinations ✅ {output_path} ({size//1024} KB)")
            return True
        except Exception as e:
            logger.warning(f"Pollinations fetch failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Fetch a batch of scene images via Pollinations
    # ------------------------------------------------------------------
    def _fetch_ai_images(self, prompts: list, output_paths: list,
                          width: int = 1080, height: int = 1920) -> list:
        """
        Fetch one Pollinations image per scene prompt.
        Returns list of successfully fetched paths.
        """
        successful = []
        for i, (prompt, path) in enumerate(zip(prompts, output_paths)):
            # Use consistent seeds per scene index for reproducibility
            seed = i * 137 + 42
            ok = self._fetch_pollinations_image(prompt, path, width, height, seed)
            if ok:
                successful.append(path)
            else:
                # Try once more with a different seed
                ok = self._fetch_pollinations_image(prompt, path, width, height, seed + 1000)
                if ok:
                    successful.append(path)
                else:
                    logger.warning(f"Scene {i+1} image failed after retry")
        return successful

    # ------------------------------------------------------------------
    # Main run method
    # ------------------------------------------------------------------
    def run(self, script: dict, video_id: str, output_dir: str = "/tmp",
            extracted: dict = None, *args, **kwargs) -> dict:
        """
        Parameters
        ----------
        script    : Full script dict from ScriptWriterAgent
        video_id  : Unique video identifier
        output_dir: Working directory for temp files
        extracted : Pass 1 extractor output (optional, improves scene matching)
        """
        niche   = self._get_niche()
        emotion = script.get("emotion", "inspiration")

        # Store extractor data in script for _build_scene_prompts
        if extracted:
            script["_extracted"] = extracted

        logger.info(f"VisualDirectorAgent → niche={niche} emotion={emotion} video={video_id}")

        duration   = self.video_config.get("duration_seconds", 55)
        num_scenes = max(4, min(8, duration // 8))
        width      = self.image_config.get("width",  1080)
        height     = self.image_config.get("height", 1920)

        # ── Choose strategy based on niche ────────────────────────────────────
        use_ai_images = (niche in AI_IMAGE_NICHES)

        if use_ai_images:
            logger.info(f"Strategy: AI-generated images (Pollinations) for niche={niche}")
            return self._run_ai_images(
                script, video_id, output_dir, niche, emotion,
                num_scenes, width, height
            )
        else:
            logger.info(f"Strategy: Stock video clips (Pexels/Pixabay) for niche={niche}")
            return self._run_stock_video(
                script, video_id, output_dir, niche, emotion, num_scenes
            )

    # ------------------------------------------------------------------
    # Strategy A: AI-generated images (horror, motivation, brainrot)
    # ------------------------------------------------------------------
    def _run_ai_images(self, script, video_id, output_dir,
                        niche, emotion, num_scenes, width, height) -> dict:
        scene_prompts = self._build_scene_prompts(script, niche, num_scenes)
        image_paths   = [
            f"{output_dir}/{video_id}_scene_{i:02d}.jpg"
            for i in range(num_scenes)
        ]

        # Primary: Pollinations AI
        successful = self._fetch_ai_images(scene_prompts, image_paths, width, height)

        if len(successful) >= 2:
            logger.success(
                f"AI images: {len(successful)}/{num_scenes} fetched | "
                f"KB preset: {EMOTION_KB_PRESET.get(emotion, 'slow_zoom_in')}"
            )
            return {
                "success":        True,
                "image_paths":    successful,
                "is_video":       False,
                "source":         "pollinations_ai",
                "kb_preset":      EMOTION_KB_PRESET.get(emotion, "slow_zoom_in"),
                "scene_prompts":  scene_prompts,
                "query_used":     scene_prompts[0] if scene_prompts else "",
            }

        # Fallback: Pexels images
        logger.warning("Pollinations insufficient — falling back to Pexels images")
        return self._run_pexels_images(
            script, video_id, output_dir, niche, emotion,
            num_scenes, width, height
        )

    # ------------------------------------------------------------------
    # Strategy B: Stock video clips (reddit_story, finance)
    # ------------------------------------------------------------------
    def _run_stock_video(self, script, video_id, output_dir,
                          niche, emotion, num_scenes) -> dict:
        # Build a single general query for stock footage
        topic   = script.get("topic_brief", {}).get("topic", script.get("title", ""))
        keywords = " ".join(topic.split()[:3])
        query    = f"{keywords} {emotion}".strip()[:80]

        clip_paths = [
            f"{output_dir}/{video_id}_clip_{i:02d}.mp4"
            for i in range(num_scenes)
        ]

        result = self.video_fetcher.fetch_clips(
            query=query,
            output_paths=clip_paths,
            niche=niche,
            emotion=emotion,
            min_duration=4,
            max_duration=15,
        )

        successful_clips = [
            p for p in result.get("clip_paths", [])
            if p and os.path.exists(p) and os.path.getsize(p) > 50_000
        ]

        if len(successful_clips) >= 2:
            logger.success(f"Stock video: {len(successful_clips)}/{num_scenes} clips fetched")
            return {
                "success":     True,
                "image_paths": successful_clips,
                "is_video":    True,
                "source":      "pexels_stock",
                "kb_preset":   EMOTION_KB_PRESET.get(emotion, "slow_zoom_in"),
                "query_used":  query,
            }

        # Fallback to Pexels images if clips fail
        logger.warning("Stock clips failed — falling back to Pexels still images")
        return self._run_pexels_images(
            script, video_id, output_dir, niche, emotion,
            num_scenes,
            self.image_config.get("width",  1080),
            self.image_config.get("height", 1920),
        )

    # ------------------------------------------------------------------
    # Pexels still images (universal fallback)
    # ------------------------------------------------------------------
    def _run_pexels_images(self, script, video_id, output_dir,
                            niche, emotion, num_scenes, width, height) -> dict:
        scene_prompts = self._build_scene_prompts(script, niche, num_scenes)
        image_paths   = [
            f"{output_dir}/{video_id}_pexels_{i:02d}.jpg"
            for i in range(num_scenes)
        ]

        # Use the first scene prompt as the Pexels query
        primary_query = scene_prompts[0] if scene_prompts else f"{niche} {emotion}"

        img_result = self.image_server.call(
            "fetch_images",
            query=primary_query[:80],
            output_paths=image_paths,
            width=width,
            height=height,
        )

        fetched = [
            img["path"]
            for img in img_result.get("images", [])
            if img.get("success") and os.path.exists(img.get("path", ""))
        ]

        if fetched:
            logger.info(f"Pexels fallback: {len(fetched)} images")
            return {
                "success":     True,
                "image_paths": fetched,
                "is_video":    False,
                "source":      "pexels_images",
                "kb_preset":   EMOTION_KB_PRESET.get(emotion, "slow_zoom_in"),
                "query_used":  primary_query,
            }

        logger.error("All visual sources failed")
        return {
            "success":     False,
            "image_paths": [],
            "is_video":    False,
            "error":       "All visual sources failed",
        }
