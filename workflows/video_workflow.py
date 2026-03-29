"""
workflows/video_workflow.py

UPGRADED v2: Wires all content-richness improvements into the pipeline.

New steps vs v1:
  Step 7.5 — FactOverlayerAgent: Burns bold on-screen fact cards at 25%/50%/72%
             of video duration. This is the "dual stimulus" technique — viewer
             reads AND listens simultaneously, doubling engagement.

Updated steps vs v1:
  Step 1  — ScriptWriterAgent now runs a 4-pass chain (added Pass 4: Loop Engineer)
  Step 3  — VisualDirectorAgent now targets ~18 scenes (rapid cutting, was ~6)
  Step 5  — CaptionMakerAgent now outputs center-screen 72px captions (was bottom 54px)
  Step 7  — VideoComposerAgent now forwards hook_text for opening hook card generation
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
    overlays_added:    int   = 0   # NEW: tracks fact overlay count
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
        from agents.fact_overlayer   import FactOverlayerAgent   # NEW
        from agents.quality_reviewer import QualityReviewerAgent
        from agents.social_publisher import SocialPublisherAgent

        self.script_writer    = ScriptWriterAgent(self.config)
        self.voice_producer   = VoiceProducerAgent(self.config)
        self.visual_director  = VisualDirectorAgent(self.config)
        self.music_director   = MusicDirectorAgent(self.config)
        self.caption_maker    = CaptionMakerAgent(self.config)
        self.video_composer   = VideoComposerAgent(self.config)
        self.fact_overlayer   = FactOverlayerAgent(self.config)  # NEW
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

            # ── Step 3: Fetch Visuals (rapid cut: ~18 scenes) ─────────────────
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

            # ── Step 5: Process Captions (center-screen 72px word highlight) ──
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

            # ── Step 7: Compose Video (with opening hook card) ─────────────────
            state.status = "composing"
            logger.info(
                "Step 7/9: Composing video "
                "(hook card + rapid cuts + emotion KB + SFX + dynamic duck)..."
            )
            # Re-attach extracted so video_composer can pass hook_text properly
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

            # ── Step 7.5: Burn Fact Overlays (NEW) ────────────────────────────
            state.status = "overlaying"
            logger.info(
                "Step 7.5/9: Burning fact overlays "
                "(dual stimulus: bold cards at 25%/50%/72%)..."
            )
            # Put extracted back for fact overlayer to use
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
            # Build a compose_result dict with updated path for quality reviewer
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
                state.publish_result = self.social_publisher.run(
                    state.final_video_path, state.script, video_index
                )

            logger.success(
                f"=== Video pipeline complete: {video_id} ===\n"
                f"  Score:        {state.quality_score}\n"
                f"  Voice:        {state.voice_result.get('backend')}\n"
                f"  Visuals:      {state.visual_source} ({len(state.visual_paths)} scenes)\n"
                f"  Captions:     {state.caption_result.get('caption_style')} center-screen\n"
                f"  Fact cards:   {state.overlays_added}\n"
                f"  Hook card:    yes\n"
                f"  Loop CTA:     {state.script.get('cta', '')[:50]}"
            )
            return self._build_result(state, "success")

        except Exception as e:
            logger.error(f"Video pipeline failed for {video_id}: {e}")
            state.status        = "failed"
            state.error_message = str(e)
            return self._build_result(state, "failed")

    # ------------------------------------------------------------------
    # Thumbnail generation (unchanged from v1)
    # ------------------------------------------------------------------
    def _generate_thumbnail(self, script: dict, video_id: str, output_dir: str) -> Optional[str]:
        try:
            import requests
            import urllib.parse

            hook    = script.get("hook", "")
            title   = script.get("title", "")
            niche   = os.environ.get("NICHE", "motivation")

            thumbnail_styles = {
                "motivation":   "dramatic lighting, golden hour, inspirational, person achieving goal",
                "horror":       "dark atmospheric, horror movie poster style, eerie shadows",
                "reddit_story": "realistic dramatic moment, candid shocked expression",
                "brainrot":     "colorful neon chaos, surreal internet aesthetic",
                "finance":      "clean professional, money concept, charts, business",
            }
            style = thumbnail_styles.get(niche, "cinematic, dramatic lighting")

            hook_words = " ".join(hook.split()[:6]) if hook else title[:40]
            prompt     = f"{hook_words}, {style}, thumbnail composition, high contrast, 4K"

            encoded = urllib.parse.quote(prompt)
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width=1280&height=720&nologo=true&enhance=true&seed=999"
            )

            resp = requests.get(url, timeout=60)
            resp.raise_for_status()

            thumbnail_path = f"{output_dir}/{video_id}_thumbnail.jpg"
            with open(thumbnail_path, "wb") as f:
                f.write(resp.content)

            size = os.path.getsize(thumbnail_path)
            if size > 5_000:
                logger.success(f"Thumbnail generated: {thumbnail_path} ({size//1024} KB)")
                return thumbnail_path
            else:
                os.remove(thumbnail_path)
                logger.warning("Thumbnail too small, skipping")
                return None
        except Exception as e:
            logger.warning(f"Thumbnail generation failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Result builder (updated with new fields)
    # ------------------------------------------------------------------
    def _build_result(self, state: VideoState, outcome: str) -> dict:
        return {
            "video_id":         state.video_id,
            "outcome":          outcome,
            "status":           state.status,
            "final_video_path": state.final_video_path,
            "thumbnail_path":   state.thumbnail_path,
            "quality_score":    state.quality_score,
            "quality_issues":   state.quality_result.get("issues", []),
            "publish_result":   state.publish_result,
            "error":            state.error_message,
            "title":            state.script.get("title", ""),
            "topic":            state.topic_brief.get("topic", ""),
            "voice_backend":    state.voice_result.get("backend", "unknown"),
            "visual_source":    state.visual_source,
            "visual_count":     len(state.visual_paths),
            "caption_style":    state.caption_result.get("caption_style", "unknown"),
            "caption_position": state.caption_result.get("position", "unknown"),
            "kb_preset":        state.kb_preset,
            "overlays_added":   state.overlays_added,          # NEW
            "hook":             state.script.get("hook", ""),  # NEW
            "cta":              state.script.get("cta", ""),   # NEW (loop-engineered)
        }
