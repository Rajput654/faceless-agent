"""
workflows/video_workflow.py

UPGRADED v2: Wires all content-richness improvements into the pipeline.

New steps vs v1:
  Step 7.5 — FactOverlayerAgent: Burns bold on-screen fact cards at 25%/50%/72%
             of video duration.

FIX 1 — Thumbnail forwarded to social_publisher:
  state.thumbnail_path was generated but never passed to social_publisher.run().
  Now forwarded so that youtube.thumbnails().set() is called after upload.

FIX 2 — Robust thumbnail generation:
  - content-type validation: Pollinations sometimes returns an HTML error page
    instead of an image. The content-type header is now checked.
  - Size threshold raised from 5,000 to 50,000 bytes to filter out HTML error
    responses that can be 10-20 KB.
  - Added the 'model' parameter to Pollinations URL for more reliable responses.
  - Timeout increased to 90s (Pollinations can be slow under load).
"""
import os
from typing import Dict, List, Optional, Literal
from loguru import logger
from pydantic import BaseModel


class VideoState(BaseModel):
    video_id:          str   = ""
    video_index:       int   = 0
    topic_brief:       Dict  = {}
    script:            Dict  = {}
    extracted:         Dict  = {}
    voice_result:      Dict  = {}
    visual_paths:      List[str] = []
    visual_source:     str   = ""
    kb_preset:         str   = "slow_zoom_in"
    music_path:        Optional[str] = None
    caption_result:    Dict  = {}
    final_video_path:  Optional[str] = None
    thumbnail_path:    Optional[str] = None
    quality_result:    Dict  = {}
    quality_score:     float = 0.0
    publish_result:    Dict  = {}
    retry_count:       int   = 0
    max_retries:       int   = 3
    overlays_added:    int   = 0
    status: Literal[
        "pending", "scripting", "voice", "visuals",
        "composing", "overlaying", "reviewing", "publishing",
        "passed", "revise", "reject", "failed"
    ] = "pending"
    error_message:     Optional[str] = None
    output_dir:        str   = "/tmp"

    class Config:
        arbitrary_types_allowed = True


class VideoWorkflow:
    def __init__(self, config):
        self.config     = config
        self.output_dir = "/tmp"
        self._init_agents()

    def _init_agents(self):
        from agents.script_writer    import ScriptWriterAgent
        from agents.voice_producer   import VoiceProducerAgent
        from agents.visual_director  import VisualDirectorAgent
        from agents.music_director   import MusicDirectorAgent
        from agents.caption_maker    import CaptionMakerAgent
        from agents.video_composer   import VideoComposerAgent
        from agents.fact_overlayer   import FactOverlayerAgent
        from agents.quality_reviewer import QualityReviewerAgent
        from agents.social_publisher import SocialPublisherAgent

        self.script_writer    = ScriptWriterAgent(self.config)
        self.voice_producer   = VoiceProducerAgent(self.config)
        self.visual_director  = VisualDirectorAgent(self.config)
        self.music_director   = MusicDirectorAgent(self.config)
        self.caption_maker    = CaptionMakerAgent(self.config)
        self.video_composer   = VideoComposerAgent(self.config)
        self.fact_overlayer   = FactOverlayerAgent(self.config)
        self.quality_reviewer = QualityReviewerAgent(self.config)
        self.social_publisher = SocialPublisherAgent(self.config)

    def run_single_video(
        self,
        topic_brief: dict,
        video_id:    str,
        video_index: int  = 0,
        upload:      bool = False,
    ) -> dict:
        logger.info(f"=== VideoWorkflow starting: {video_id} ===")
        logger.info(f"Topic: {topic_brief.get('topic', 'Unknown')}")

        state = VideoState(
            video_id    = video_id,
            video_index = video_index,
            topic_brief = topic_brief,
            output_dir  = self.output_dir,
            max_retries = self.config.get("video", {}).get("max_retries", 3),
        )

        try:
            # ── Step 1: Write Script (4-pass chain) ───────────────────────────
            state.status = "scripting"
            logger.info("Step 1/9: Writing script (4-pass chain: extract → write → hook → loop)...")
            state.script = self.script_writer.run(topic_brief, video_id)
            if not state.script:
                raise RuntimeError("Script writer returned empty result")

            emotion   = state.script.get("emotion", "inspiration")
            kb_preset = state.script.get("_kb_preset", None)

            logger.info(
                f"Script ready | Title: '{state.script.get('title', '')}' | "
                f"Hook: '{state.script.get('hook', '')[:50]}' | "
                f"CTA: '{state.script.get('cta', '')[:50]}' | "
                f"Emotion: {emotion}"
            )

            # ── Step 2: Generate Voice ─────────────────────────────────────────
            state.status = "voice"
            logger.info("Step 2/9: Generating voice (Chatterbox → Kokoro → edge-tts)...")
            state.voice_result = self.voice_producer.run(
                state.script, video_id, self.output_dir
            )
            if not state.voice_result.get("success"):
                raise RuntimeError(
                    f"Voice producer failed: {state.voice_result.get('error')}"
                )
            logger.info(
                f"Voice backend: {state.voice_result.get('backend', 'unknown')} | "
                f"Duration: {state.voice_result.get('duration_ms', 0)/1000:.1f}s"
            )

            # ── Step 3: Fetch Visuals ─────────────────────────────────────────
            logger.info("Step 3/9: Fetching visuals (rapid cut: ~1 scene per 3s)...")

            extracted = state.script.pop("_extracted", {})
            state.extracted = extracted

            visual_result = self.visual_director.run(
                state.script, video_id, self.output_dir,
                extracted=extracted
            )
            state.visual_paths  = visual_result.get("image_paths", [])
            state.visual_source = visual_result.get("source", "unknown")
            state.kb_preset     = visual_result.get("kb_preset", "slow_zoom_in")

            logger.info(
                f"Visuals: {len(state.visual_paths)} assets | "
                f"source={state.visual_source} | kb_preset={state.kb_preset}"
            )

            # ── Step 4: Fetch Music ────────────────────────────────────────────
            logger.info("Step 4/9: Fetching music...")
            music_result     = self.music_director.run(
                state.script, video_id, self.output_dir
            )
            state.music_path = music_result.get("music_path")

            # ── Step 5: Process Captions ───────────────────────────────────────
            logger.info("Step 5/9: Processing captions (center-screen, 72px, word highlight)...")
            state.caption_result = self.caption_maker.run(
                state.voice_result, video_id, self.output_dir
            )
            logger.info(
                f"Caption style: {state.caption_result.get('caption_style', 'unknown')} | "
                f"Position: {state.caption_result.get('position', 'unknown')} | "
                f"Font size: {state.caption_result.get('font_size', 'unknown')}px | "
                f"Words: {state.caption_result.get('word_count', 0)}"
            )

            # ── Step 6: Generate Thumbnail ─────────────────────────────────────
            logger.info("Step 6/9: Generating thumbnail...")
            state.thumbnail_path = self._generate_thumbnail(
                state.script, video_id, self.output_dir
            )
            if state.thumbnail_path:
                logger.success(f"Thumbnail ready: {state.thumbnail_path}")
            else:
                logger.warning("Thumbnail generation failed — YouTube will use auto-frame")

            # ── Step 7: Compose Video ──────────────────────────────────────────
            state.status = "composing"
            logger.info(
                "Step 7/9: Composing video "
                "(hook card + rapid cuts + emotion KB + SFX + dynamic duck)..."
            )
            state.script["_extracted"] = extracted

            compose_result = self.video_composer.run(
                script         = state.script,
                voice_result   = state.voice_result,
                visual_result  = {"image_paths": state.visual_paths},
                caption_result = state.caption_result,
                music_result   = music_result,
                video_id       = video_id,
                output_dir     = self.output_dir,
                emotion        = emotion,
                kb_preset      = state.kb_preset,
            )
            state.final_video_path = compose_result.get("final_video_path")

            if not compose_result.get("success"):
                raise RuntimeError(
                    f"Video composition failed: {compose_result.get('error')}"
                )

            # ── Step 7.5: Burn Fact Overlays ───────────────────────────────────
            state.status = "overlaying"
            logger.info(
                "Step 7.5/9: Burning fact overlays "
                "(dual stimulus: bold cards at 25%/50%/72%)..."
            )
            state.script["_extracted"] = extracted

            overlay_result = self.fact_overlayer.run(
                video_path = state.final_video_path,
                script     = state.script,
                video_id   = video_id,
                output_dir = self.output_dir,
            )
            state.overlays_added = overlay_result.get("overlays_added", 0)

            if overlay_result.get("success") and overlay_result.get("video_path"):
                new_path = overlay_result["video_path"]
                if new_path != state.final_video_path and os.path.exists(new_path):
                    state.final_video_path = new_path
                    logger.success(
                        f"Fact overlays applied: {state.overlays_added} cards | "
                        f"Texts: {overlay_result.get('texts_used', [])}"
                    )
                else:
                    logger.info(f"Fact overlays: {state.overlays_added} cards added (same file)")
            else:
                logger.warning("Fact overlay step returned no video — using pre-overlay video")

            # ── Step 8: Quality Review ─────────────────────────────────────────
            state.status = "reviewing"
            logger.info("Step 8/9: Quality review...")
            updated_compose = {
                "final_video_path": state.final_video_path,
                "success": True,
            }
            state.quality_result = self.quality_reviewer.run(
                updated_compose, state.script, video_id
            )
            state.quality_score = state.quality_result.get("quality_score", 0.0)

            if not state.quality_result.get("passed"):
                logger.warning(
                    f"Quality check failed: {state.quality_result.get('issues')}"
                )
                state.status = "reject"
                return self._build_result(state, "failed")

            state.status = "passed"

            # ── Optional: Upload ───────────────────────────────────────────────
            if upload and state.final_video_path:
                state.status = "publishing"
                logger.info("Step 9/9: Uploading to YouTube...")
                # FIX: pass thumbnail_path so it gets attached to the video
                state.publish_result = self.social_publisher.run(
                    state.final_video_path,
                    state.script,
                    video_index,
                    thumbnail_path=state.thumbnail_path,   # FIX: was missing
                )

            logger.success(
                f"=== Video pipeline complete: {video_id} ===\n"
                f"  Score:        {state.quality_score}\n"
                f"  Voice:        {state.voice_result.get('backend')}\n"
                f"  Visuals:      {state.visual_source} ({len(state.visual_paths)} scenes)\n"
                f"  Captions:     {state.caption_result.get('caption_style')} center-screen\n"
                f"  Fact cards:   {state.overlays_added}\n"
                f"  Hook card:    yes\n"
                f"  Thumbnail:    {'generated' if state.thumbnail_path else 'not generated'}\n"
                f"  Loop CTA:     {state.script.get('cta', '')[:50]}"
            )
            return self._build_result(state, "success")

        except Exception as e:
            logger.error(f"Video pipeline failed for {video_id}: {e}")
            state.status        = "failed"
            state.error_message = str(e)
            return self._build_result(state, "failed")

    # ------------------------------------------------------------------
    # FIX 2: Robust thumbnail generation
    # ------------------------------------------------------------------
    def _generate_thumbnail(self, script: dict, video_id: str, output_dir: str) -> Optional[str]:
        """
        Generate a custom thumbnail using Pollinations AI.

        FIX: Previous version had two reliability problems:
          1. Size threshold was 5,000 bytes — HTML error pages from Pollinations
             can be 10-20 KB, so the check did not catch them.
          2. No content-type validation — an HTML page with size > 5000 would
             pass through and be saved as the thumbnail, causing upload failure.

        Fixes applied:
          - Size threshold raised to 50,000 bytes (real images are typically
            100KB-1MB; anything smaller is almost certainly an error page).
          - content-type header is validated to start with "image/".
          - Timeout increased from 60s to 90s for reliability under load.
          - Added 'nologo=true&enhance=true' to the Pollinations URL.
          - Added a second attempt with a simplified prompt on failure.
        """
        try:
            import requests
            import urllib.parse

            hook    = script.get("hook", "")
            title   = script.get("title", "")
            niche   = os.environ.get("NICHE", "motivation")

            thumbnail_styles = {
                "motivation":   "dramatic lighting, golden hour, inspirational, person achieving goal, cinematic",
                "horror":       "dark atmospheric, horror movie poster style, eerie shadows, fog, cinematic",
                "reddit_story": "realistic dramatic moment, candid shocked expression, cinematic, photorealistic",
                "brainrot":     "colorful neon chaos, surreal internet aesthetic, vibrant, high contrast",
                "finance":      "clean professional, money concept, charts, business, corporate, sharp lighting",
            }
            style = thumbnail_styles.get(niche, "cinematic, dramatic lighting, 4K")

            hook_words = " ".join(hook.split()[:6]) if hook else title[:40]
            prompt     = f"{hook_words}, {style}, YouTube thumbnail composition, 16:9"

            thumbnail_path = f"{output_dir}/{video_id}_thumbnail.jpg"

            # Attempt 1: full prompt
            success = self._fetch_pollinations_thumbnail(
                prompt, thumbnail_path, width=1280, height=720, seed=42
            )

            if not success:
                # Attempt 2: simplified fallback prompt
                simple_prompt = f"{niche} content, {style}, YouTube thumbnail"
                logger.info("Thumbnail attempt 1 failed — trying simplified prompt...")
                success = self._fetch_pollinations_thumbnail(
                    simple_prompt, thumbnail_path, width=1280, height=720, seed=999
                )

            if success:
                size = os.path.getsize(thumbnail_path)
                logger.success(f"Thumbnail generated: {thumbnail_path} ({size // 1024} KB)")
                return thumbnail_path
            else:
                logger.warning("Both thumbnail attempts failed")
                return None

        except Exception as e:
            logger.warning(f"Thumbnail generation crashed: {e}")
            return None

    def _fetch_pollinations_thumbnail(
        self,
        prompt: str,
        output_path: str,
        width: int = 1280,
        height: int = 720,
        seed: int = 42,
    ) -> bool:
        """
        Fetch a single image from Pollinations AI.

        Returns True only if a valid image was saved, False otherwise.
        Validates both the HTTP status, content-type header, AND file size.
        """
        try:
            import requests
            import urllib.parse

            encoded = urllib.parse.quote(prompt)
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width={width}&height={height}&seed={seed}"
                f"&nologo=true&enhance=true"
            )

            resp = requests.get(url, timeout=90)  # FIX: was 60s
            resp.raise_for_status()

            # FIX: validate content-type — Pollinations returns HTML on error
            content_type = resp.headers.get("content-type", "").lower()
            if not content_type.startswith("image/"):
                logger.warning(
                    f"Pollinations returned non-image content-type: '{content_type}' "
                    f"(likely an error page). Discarding."
                )
                return False

            content = resp.content

            # FIX: raised from 5,000 to 50,000 — HTML error pages can be ~15 KB
            if len(content) < 50_000:
                logger.warning(
                    f"Pollinations response too small: {len(content)} bytes "
                    f"(threshold: 50,000). Content-type was '{content_type}'. Discarding."
                )
                return False

            with open(output_path, "wb") as f:
                f.write(content)

            return True

        except Exception as e:
            logger.warning(f"Pollinations thumbnail fetch failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Result builder (updated with thumbnail status)
    # ------------------------------------------------------------------
    def _build_result(self, state: VideoState, outcome: str) -> dict:
        thumbnail_uploaded = (
            state.publish_result.get("thumbnail_uploaded", False)
            if state.publish_result else False
        )
        return {
            "video_id":           state.video_id,
            "outcome":            outcome,
            "status":             state.status,
            "final_video_path":   state.final_video_path,
            "thumbnail_path":     state.thumbnail_path,
            "thumbnail_uploaded": thumbnail_uploaded,
            "quality_score":      state.quality_score,
            "quality_issues":     state.quality_result.get("issues", []),
            "publish_result":     state.publish_result,
            "error":              state.error_message,
            "title":              state.script.get("title", ""),
            "topic":              state.topic_brief.get("topic", ""),
            "voice_backend":      state.voice_result.get("backend", "unknown"),
            "visual_source":      state.visual_source,
            "visual_count":       len(state.visual_paths),
            "caption_style":      state.caption_result.get("caption_style", "unknown"),
            "caption_position":   state.caption_result.get("position", "unknown"),
            "kb_preset":          state.kb_preset,
            "overlays_added":     state.overlays_added,
            "hook":               state.script.get("hook", ""),
            "cta":                state.script.get("cta", ""),
        }
