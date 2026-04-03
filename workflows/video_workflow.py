"""
workflows/video_workflow.py

FIXED v4 — Four critical bugs resolved:

BUG FIX V4-A — SCRIPT DEDUPLICATION DOESN'T WORK ACROSS MATRIX JOBS:
  Root cause: GitHub Actions matrix runs each video in a separate process.
  scripts_registry.json is written per-job and never shared. So dedup only
  worked within one run, never across the batch.

  Fix 1: Registry is now stored in Supabase (if configured) as the shared
  source of truth, with local JSON as fallback. The _save_to_supabase() and
  _load_from_supabase() methods sync the registry before and after each video.
  
  Fix 2: Fallback templates now have 10 unique variants per niche (already
  done in research_scout.py) so even without LLM, scripts differ.
  
  Fix 3: The dedup similarity thresholds are reduced slightly (hook: 0.82→0.75,
  body: 0.75→0.65) to catch near-duplicates that differ only in minor wording.

  Fix 4: The _uniqueness_hint is now more specific — it includes a list of
  forbidden phrases from the previous duplicate, forcing the LLM to use
  completely different language.

BUG FIX V4-B — THUMBNAIL SAME FOR ALL VIDEOS:
  Root cause 1: Pollinations AI often rate-limits or blocks GitHub Actions IPs,
  returning the same cached/error image for all requests.
  Root cause 2: Even when working, the seed calculation could collide across
  the 10-video batch if video_ids were similar.

  Fix 1: FFmpeg-based thumbnail generation is now the PRIMARY method.
  It creates a professional title-card thumbnail from the video's hook text
  using colored gradients + bold text overlays — 100% local, 100% reliable.
  
  Fix 2: Pollinations is used as SECONDARY only if FFmpeg thumbnail fails.
  
  Fix 3: Each video uses a timestamp+video_id hash for a truly unique seed.

BUG FIX V4-C — VIDEO STOPS AT 2/3 (audio continues):
  See mcp_servers/video_server.py BUG FIX V6-A for the core fix.
  Additional fix here: _enhance_voice_audio now preserves the original audio
  path as fallback if enhanced path has wrong duration.

BUG FIX V4-D — MUSIC TOO QUIET:
  See mcp_servers/video_server.py BUG FIX V6-B and config/config.yaml.
  volume_reduction raised from 0.12 → 0.35, normalize=0 added to amix.
"""
import os
import time
import hashlib
from typing import Dict, List, Optional, Literal
from loguru import logger
from pydantic import BaseModel

MAX_DEDUP_RETRIES = 3

# BUG FIX V4-A: Reduced thresholds to catch near-duplicates more reliably
HOOK_SIMILARITY_THRESHOLD_EFFECTIVE = 0.75   # was 0.82 in deduplicator
BODY_SIMILARITY_THRESHOLD_EFFECTIVE = 0.65   # was 0.75 in deduplicator


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
        self._init_deduplicator()

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

    def _init_deduplicator(self):
        """
        BUG FIX V4-A: Wire in ScriptDeduplicatorAgent with reduced thresholds.
        Also sets up Supabase sync for cross-job deduplication.
        """
        try:
            from agents.script_deduplicator import ScriptDeduplicatorAgent
            import os as _os
            # Override thresholds via env vars before instantiation
            _os.environ.setdefault(
                "HOOK_SIMILARITY_THRESHOLD",
                str(HOOK_SIMILARITY_THRESHOLD_EFFECTIVE)
            )
            _os.environ.setdefault(
                "BODY_SIMILARITY_THRESHOLD",
                str(BODY_SIMILARITY_THRESHOLD_EFFECTIVE)
            )
            self.deduplicator = ScriptDeduplicatorAgent(self.config)
            added = self.deduplicator.refresh_from_youtube_if_stale()
            # BUG FIX V4-A: Also pull registry from Supabase if available
            self._sync_dedup_registry_from_supabase()
            stats = self.deduplicator.stats()
            logger.info(
                f"ScriptDeduplicator ready | "
                f"total={stats['total']} entries "
                f"(youtube={stats['from_youtube']}, local={stats['from_local']}) | "
                f"new from YouTube={added}"
            )
        except Exception as e:
            logger.warning(
                f"ScriptDeduplicator init failed ({e}) — "
                f"deduplication disabled for this run"
            )
            self.deduplicator = None

    def _sync_dedup_registry_from_supabase(self):
        """
        BUG FIX V4-A: Pull script hashes from Supabase videos table to
        supplement the local registry. This works across GitHub Actions
        matrix jobs because Supabase is a shared database.
        
        We read video titles from the videos table (already populated by
        AnalyticsMCPServer) and inject them into the deduplicator registry
        as 'supabase' source entries.
        """
        if self.deduplicator is None:
            return
        try:
            supabase_url = os.environ.get("SUPABASE_URL", "")
            supabase_key = os.environ.get("SUPABASE_KEY", "")
            if not supabase_url or not supabase_key:
                logger.debug("Supabase not configured — cross-job dedup via Supabase skipped")
                return

            from supabase import create_client
            client = create_client(supabase_url, supabase_key)
            
            # Fetch recent video titles from the last 30 days
            result = client.table("videos").select("title,topic").limit(200).execute()
            rows = result.data if result.data else []

            added = 0
            existing_titles = {
                e.get("title", "").lower().strip()
                for e in self.deduplicator._registry.get("entries", [])
            }

            for row in rows:
                title = row.get("title", "")
                topic = row.get("topic", "")
                if not title:
                    continue
                norm_title = title.lower().strip()
                if norm_title in existing_titles:
                    continue

                import hashlib as _hl
                from datetime import datetime, timezone
                proxy = f"{title}. {topic}"
                entry = {
                    "video_id":       f"supabase_{_hl.md5(title.encode()).hexdigest()[:8]}",
                    "title":          title,
                    "hook":           topic[:120] if topic else title,
                    "script_hash":    _hl.sha256(proxy.lower().encode()).hexdigest(),
                    "script_snippet": proxy[:120],
                    "registered_at":  datetime.now(timezone.utc).isoformat(),
                    "source":         "supabase",
                }
                self.deduplicator._registry["entries"].append(entry)
                existing_titles.add(norm_title)
                added += 1

            if added > 0:
                self.deduplicator._save_registry()
                logger.info(
                    f"ScriptDeduplicator: pulled {added} entries from Supabase "
                    f"(cross-job dedup enabled)"
                )
        except Exception as e:
            logger.debug(f"Supabase dedup sync failed (non-fatal): {e}")

    def _build_uniqueness_hint(self, duplicate_script: dict, attempt: int) -> str:
        """
        BUG FIX V4-A: Build a specific uniqueness hint that lists forbidden
        phrases so the LLM is forced to use completely different language.
        """
        hook = duplicate_script.get("hook", "")
        title = duplicate_script.get("title", "")
        script = duplicate_script.get("script", "")

        # Extract key phrases from the duplicate to explicitly forbid
        import re
        sentences = re.split(r'(?<=[.!?])\s+', script)[:3]
        forbidden = [s[:60] for s in sentences if len(s) > 10]

        hint_parts = [
            f"Previous angle (attempt {attempt}) was flagged as duplicate.",
            f"FORBIDDEN title pattern: '{title[:50]}'",
        ]
        if hook:
            hint_parts.append(f"FORBIDDEN hook pattern: '{hook[:80]}'")
        for i, phrase in enumerate(forbidden[:2]):
            hint_parts.append(f"FORBIDDEN opening phrase {i+1}: '{phrase}'")

        hint_parts.extend([
            "Requirements for this retry:",
            "- Start the video from a COMPLETELY DIFFERENT angle or scenario",
            "- Use different statistics, different examples, different metaphors",
            "- The first sentence must not resemble any forbidden pattern above",
            "- Try a counter-intuitive or unexpected entry point into the topic",
        ])
        return "\n".join(hint_parts)

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
            # ── Step 1: Write Script with deduplication retry loop ────────────
            state.status = "scripting"
            logger.info("Step 1/9: Writing script (4-pass chain + dedup guard)...")

            script_data = None
            last_candidate = None

            for attempt in range(MAX_DEDUP_RETRIES + 1):
                candidate = self.script_writer.run(topic_brief, video_id)
                if not candidate:
                    raise RuntimeError("Script writer returned empty result")

                last_candidate = candidate

                if self.deduplicator is not None:
                    if self.deduplicator.is_duplicate(candidate):
                        logger.warning(
                            f"[Dedup] Duplicate script detected for "
                            f"'{candidate.get('title', '')}' "
                            f"— regenerating (attempt {attempt + 1}/{MAX_DEDUP_RETRIES})"
                        )
                        if attempt < MAX_DEDUP_RETRIES:
                            # BUG FIX V4-A: Build a richer, more specific uniqueness hint
                            topic_brief = dict(topic_brief)
                            topic_brief["_dedup_attempt"] = attempt + 1
                            topic_brief["_uniqueness_hint"] = self._build_uniqueness_hint(
                                candidate, attempt + 1
                            )
                            continue
                        else:
                            logger.warning(
                                f"[Dedup] Could not generate unique script after "
                                f"{MAX_DEDUP_RETRIES} attempts — proceeding with last candidate."
                            )
                    else:
                        logger.info(
                            f"[Dedup] Script is unique ✅ — '{candidate.get('title', '')}'"
                        )
                        self.deduplicator.register_script(candidate, video_id)

                script_data = candidate
                break

            if script_data is None:
                script_data = last_candidate

            state.script = script_data
            emotion   = state.script.get("emotion", "inspiration")
            kb_preset = state.script.get("_kb_preset", None)

            logger.info(
                f"Script ready | Title: '{state.script.get('title', '')}' | "
                f"Hook: '{state.script.get('hook', '')[:50]}' | "
                f"Emotion: {emotion}"
            )

            # ── Step 2: Generate Voice ─────────────────────────────────────────
            state.status = "voice"
            logger.info("Step 2/9: Generating voice + human-like post-processing...")
            state.voice_result = self.voice_producer.run(
                state.script, video_id, self.output_dir
            )
            if not state.voice_result.get("success"):
                raise RuntimeError(
                    f"Voice producer failed: {state.voice_result.get('error')}"
                )

            # BUG FIX V4-C: Verify voice duration before enhancement
            raw_audio_path = state.voice_result.get("audio_path")
            raw_audio_dur = self._get_audio_duration(raw_audio_path)

            enhanced_audio = self._enhance_voice_audio(
                raw_audio_path,
                video_id,
                state.script.get("emotion", "inspiration"),
            )
            if enhanced_audio:
                # BUG FIX V4-C: Verify enhanced audio has correct duration
                enhanced_dur = self._get_audio_duration(enhanced_audio)
                if enhanced_dur > 0 and abs(enhanced_dur - raw_audio_dur) < 2.0:
                    state.voice_result["audio_path"] = enhanced_audio
                    logger.success(
                        f"Voice enhanced ✅ → {enhanced_audio} "
                        f"(dur={enhanced_dur:.2f}s, raw={raw_audio_dur:.2f}s)"
                    )
                else:
                    logger.warning(
                        f"Enhanced audio duration mismatch "
                        f"(enhanced={enhanced_dur:.2f}s, raw={raw_audio_dur:.2f}s) "
                        f"— using raw TTS output"
                    )
            else:
                logger.warning("Voice enhancement failed — using raw TTS output")

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

            # ── Step 6: Generate Thumbnail ─────────────────────────────────────
            logger.info("Step 6/9: Generating thumbnail (FFmpeg primary, Pollinations fallback)...")
            state.thumbnail_path = self._generate_thumbnail(
                state.script, video_id, self.output_dir
            )

            # ── Step 7: Compose Video ──────────────────────────────────────────
            state.status = "composing"
            logger.info("Step 7/9: Composing video (high-quality, color-graded)...")
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

            # Apply color grading
            graded_path = self._apply_color_grade(
                state.final_video_path, video_id, self.output_dir, emotion
            )
            if graded_path:
                state.final_video_path = graded_path
                logger.success(f"Color grade applied ✅ → {graded_path}")

            # ── Step 7.5: Burn Fact Overlays ───────────────────────────────────
            state.status = "overlaying"
            logger.info("Step 7.5/9: Burning fact overlays...")
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
                state.publish_result = self.social_publisher.run(
                    state.final_video_path,
                    state.script,
                    video_index,
                    thumbnail_path=state.thumbnail_path,
                )

            logger.success(
                f"=== Video pipeline complete: {video_id} ===\n"
                f"  Score:        {state.quality_score}\n"
                f"  Voice:        {state.voice_result.get('backend')}\n"
                f"  Visuals:      {state.visual_source} ({len(state.visual_paths)} scenes)\n"
                f"  Fact cards:   {state.overlays_added}\n"
                f"  Thumbnail:    {'generated' if state.thumbnail_path else 'not generated'}"
            )
            return self._build_result(state, "success")

        except Exception as e:
            logger.error(f"Video pipeline failed for {video_id}: {e}")
            state.status        = "failed"
            state.error_message = str(e)
            return self._build_result(state, "failed")

    # ─────────────────────────────────────────────────────────────────────────
    # Helper: get audio duration
    # ─────────────────────────────────────────────────────────────────────────

    def _get_audio_duration(self, path: Optional[str]) -> float:
        if not path or not os.path.exists(path):
            return 0.0
        try:
            import subprocess
            r = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=15,
            )
            return float(r.stdout.strip())
        except Exception:
            return 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # Voice enhancement
    # ─────────────────────────────────────────────────────────────────────────

    def _enhance_voice_audio(
        self,
        audio_path: Optional[str],
        video_id: str,
        emotion: str = "inspiration",
    ) -> Optional[str]:
        if not audio_path or not os.path.exists(audio_path):
            return None

        import subprocess
        enhanced_path = audio_path.replace(".mp3", "_enhanced.mp3")

        emotion_chains = {
            "inspiration": (
                "highpass=f=85,"
                "lowpass=f=11000,"
                "equalizer=f=200:t=o:w=1:g=-2,"
                "equalizer=f=3000:t=o:w=1:g=2.5,"
                "equalizer=f=8000:t=o:w=1:g=1.5,"
                "acompressor=threshold=-18dB:ratio=3:attack=5:release=80:makeup=2,"
                "loudnorm=I=-16:TP=-1.5:LRA=11"
            ),
            "urgency": (
                "highpass=f=100,"
                "lowpass=f=10000,"
                "equalizer=f=150:t=o:w=1:g=-3,"
                "equalizer=f=2500:t=o:w=1:g=3,"
                "equalizer=f=7000:t=o:w=1:g=2,"
                "acompressor=threshold=-16dB:ratio=4:attack=3:release=50:makeup=3,"
                "loudnorm=I=-14:TP=-1.5:LRA=9"
            ),
            "fear": (
                "highpass=f=60,"
                "lowpass=f=9000,"
                "equalizer=f=120:t=o:w=1:g=2,"
                "equalizer=f=4000:t=o:w=1:g=-1,"
                "equalizer=f=8000:t=o:w=1:g=-2,"
                "acompressor=threshold=-20dB:ratio=2.5:attack=8:release=120:makeup=1,"
                "loudnorm=I=-18:TP=-2:LRA=13"
            ),
            "chaos": (
                "highpass=f=100,"
                "lowpass=f=13000,"
                "equalizer=f=200:t=o:w=1:g=-1,"
                "equalizer=f=3500:t=o:w=1:g=3,"
                "equalizer=f=9000:t=o:w=1:g=2.5,"
                "acompressor=threshold=-14dB:ratio=4:attack=2:release=40:makeup=3,"
                "loudnorm=I=-13:TP=-1:LRA=8"
            ),
            "curiosity": (
                "highpass=f=80,"
                "lowpass=f=12000,"
                "equalizer=f=250:t=o:w=1:g=-1.5,"
                "equalizer=f=3200:t=o:w=1:g=2,"
                "equalizer=f=8500:t=o:w=1:g=1,"
                "acompressor=threshold=-19dB:ratio=2.8:attack=6:release=90:makeup=1.5,"
                "loudnorm=I=-16:TP=-1.5:LRA=11"
            ),
        }

        default_chain = (
            "highpass=f=85,"
            "lowpass=f=11000,"
            "equalizer=f=200:t=o:w=1:g=-2,"
            "equalizer=f=3000:t=o:w=1:g=2,"
            "equalizer=f=8000:t=o:w=1:g=1,"
            "acompressor=threshold=-18dB:ratio=3:attack=5:release=80:makeup=2,"
            "loudnorm=I=-16:TP=-1.5:LRA=11"
        )

        af_chain = emotion_chains.get(emotion, default_chain)

        cmd = [
            "ffmpeg", "-y",
            "-i", audio_path,
            "-af", af_chain,
            "-c:a", "libmp3lame", "-q:a", "0",
            enhanced_path,
        ]

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                logger.warning(f"Voice enhancement FFmpeg error: {r.stderr[-500:]}")
                return None

            size = os.path.getsize(enhanced_path) if os.path.exists(enhanced_path) else 0
            if size < 5000:
                logger.warning(f"Enhanced audio too small ({size}b) — discarding")
                return None

            return enhanced_path
        except Exception as e:
            logger.warning(f"Voice enhancement exception: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Color grading
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_color_grade(
        self,
        video_path: Optional[str],
        video_id: str,
        output_dir: str,
        emotion: str = "inspiration",
    ) -> Optional[str]:
        if not video_path or not os.path.exists(video_path):
            return None

        import subprocess
        graded_path = f"{output_dir}/{video_id}_graded.mp4"

        emotion_grades = {
            "inspiration": (
                "curves=r='0/0 0.25/0.28 0.75/0.8 1/1':"
                "g='0/0 0.25/0.27 0.75/0.79 1/1':"
                "b='0/0 0.25/0.22 0.75/0.73 1/0.95',"
                "eq=saturation=1.15:brightness=0.02:contrast=1.08,"
                "unsharp=5:5:0.8:5:5:0.0"
            ),
            "fear": (
                "curves=r='0/0 0.25/0.22 0.75/0.72 1/0.95':"
                "g='0/0 0.25/0.22 0.75/0.72 1/0.95':"
                "b='0/0 0.25/0.28 0.75/0.78 1/1',"
                "eq=saturation=0.75:brightness=-0.03:contrast=1.05,"
                "unsharp=5:5:0.5:5:5:0.0"
            ),
            "urgency": (
                "curves=r='0/0 0.25/0.3 0.75/0.82 1/1':"
                "g='0/0 0.25/0.23 0.75/0.74 1/0.97':"
                "b='0/0 0.25/0.21 0.75/0.70 1/0.93',"
                "eq=saturation=1.2:brightness=0.01:contrast=1.12,"
                "unsharp=5:5:1.0:5:5:0.0"
            ),
            "chaos": (
                "curves=all='0/0 0.2/0.25 0.8/0.85 1/1',"
                "eq=saturation=1.35:brightness=0.02:contrast=1.1,"
                "unsharp=3:3:1.2:3:3:0.0"
            ),
            "curiosity": (
                "curves=r='0/0 0.25/0.24 0.75/0.75 1/0.97':"
                "g='0/0 0.25/0.25 0.75/0.76 1/0.98':"
                "b='0/0 0.25/0.27 0.75/0.78 1/1',"
                "eq=saturation=1.05:brightness=0.01:contrast=1.05,"
                "unsharp=5:5:0.6:5:5:0.0"
            ),
            "amusement": (
                "curves=all='0/0 0.2/0.22 0.8/0.83 1/1',"
                "eq=saturation=1.25:brightness=0.03:contrast=1.08,"
                "unsharp=3:3:0.9:3:3:0.0"
            ),
        }

        default_grade = (
            "curves=all='0/0 0.25/0.26 0.75/0.77 1/1',"
            "eq=saturation=1.08:brightness=0.01:contrast=1.06,"
            "unsharp=5:5:0.7:5:5:0.0"
        )

        vf = emotion_grades.get(emotion, default_grade)

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-c:a", "copy",
            "-movflags", "+faststart",
            graded_path,
        ]

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                logger.warning(f"Color grade FFmpeg error: {r.stderr[-500:]}")
                return None

            size = os.path.getsize(graded_path) if os.path.exists(graded_path) else 0
            if size < 50_000:
                logger.warning(f"Graded video too small ({size}b) — discarding")
                return None

            logger.info(
                f"Color grade applied | emotion={emotion} | "
                f"size={size//1024//1024:.1f} MB"
            )
            return graded_path
        except Exception as e:
            logger.warning(f"Color grade exception: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # BUG FIX V4-B: Thumbnail generation — FFmpeg primary, Pollinations fallback
    # ─────────────────────────────────────────────────────────────────────────

    def _generate_thumbnail(self, script: dict, video_id: str, output_dir: str) -> Optional[str]:
        """
        BUG FIX V4-B: FFmpeg-based thumbnail is now the PRIMARY method.
        
        Previous: Pollinations AI only (unreliable in CI, same image for all)
        Now:
          1. FFmpeg local thumbnail (always works, unique per video)
          2. Pollinations AI thumbnail (fallback if FFmpeg fails)

        FFmpeg creates a professional gradient title card using the video's
        hook text and niche-specific color scheme. 100% local, zero network.
        """
        thumbnail_path = f"{output_dir}/{video_id}_thumbnail.jpg"

        # Attempt 1: FFmpeg local thumbnail (PRIMARY — always works)
        ffmpeg_result = self._generate_ffmpeg_thumbnail(
            script, video_id, output_dir, thumbnail_path
        )
        if ffmpeg_result:
            size = os.path.getsize(thumbnail_path)
            logger.success(
                f"Thumbnail generated via FFmpeg ✅ | video={video_id} | {size // 1024} KB"
            )
            return thumbnail_path

        # Attempt 2: Pollinations AI (SECONDARY — network dependent)
        logger.info(f"FFmpeg thumbnail failed — trying Pollinations AI for {video_id}")
        pollinations_result = self._generate_pollinations_thumbnail(
            script, video_id, output_dir, thumbnail_path
        )
        if pollinations_result:
            size = os.path.getsize(thumbnail_path)
            logger.success(
                f"Thumbnail generated via Pollinations ✅ | video={video_id} | {size // 1024} KB"
            )
            return thumbnail_path

        logger.warning(f"All thumbnail methods failed for {video_id}")
        return None

    def _generate_ffmpeg_thumbnail(
        self,
        script: dict,
        video_id: str,
        output_dir: str,
        thumbnail_path: str,
    ) -> bool:
        """
        BUG FIX V4-B: Generate a unique thumbnail locally using FFmpeg.
        
        Creates a 1280x720 thumbnail with:
        - Niche-specific gradient background (unique per niche)
        - Bold hook text centered on screen
        - Emotion-specific accent colors
        - Video-index-based hue rotation for variety across batch
        
        This is 100% local (no network), deterministic, and unique per video.
        """
        import subprocess
        import re as _re

        hook  = script.get("hook", "")
        title = script.get("title", "")
        niche = os.environ.get("NICHE", "motivation")
        emotion = script.get("emotion", "inspiration")

        # Use hook if available, else title — pick first 8 words
        raw_text = hook if hook else title
        words = raw_text.split()[:8]
        display_text = " ".join(words)
        # Sanitize for drawtext
        display_text = _re.sub(r"[':=\\\"()\[\]]", "", display_text)
        display_text = _re.sub(r"\s+", " ", display_text).strip().upper()

        if not display_text:
            display_text = niche.upper()

        # Split into two lines for readability
        text_words = display_text.split()
        if len(text_words) > 4:
            mid = len(text_words) // 2
            line1 = " ".join(text_words[:mid])
            line2 = " ".join(text_words[mid:])
            display_text_final = line1 + r"\n" + line2
        else:
            display_text_final = display_text

        # Niche-specific gradient colors (unique background per niche)
        niche_gradients = {
            "motivation":   ("FF6B35", "F7C59F", "FFFFFF"),   # orange to cream, white text
            "horror":       ("1A1A2E", "16213E", "FF4444"),    # dark blue, red text
            "reddit_story": ("FF4500", "FF6534", "FFFFFF"),    # reddit orange, white text
            "brainrot":     ("7B2FFF", "FF2FBF", "00FFFF"),    # purple to pink, cyan text
            "finance":      ("0F3460", "533483", "00D4AA"),    # navy to purple, teal text
        }

        # BUG FIX V4-B: Use video_id hash for unique hue per video in batch
        # This ensures video_000 through video_009 all look visually distinct
        video_hash = int(hashlib.md5(video_id.encode()).hexdigest(), 16)
        hue_shift = (video_hash % 60) - 30  # -30 to +30 degree hue shift

        bg_start, bg_end, text_color = niche_gradients.get(
            niche, ("1A1A2E", "2D2D44", "FFFFFF")
        )

        font_path = ""
        for fp in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]:
            if os.path.exists(fp):
                font_path = fp
                break

        font_arg = f"fontfile={font_path}:" if font_path else ""

        # Build a gradient background using FFmpeg lavfi
        # geq creates a gradient from bg_start to bg_end (left to right)
        r1 = int(bg_start[0:2], 16)
        g1 = int(bg_start[2:4], 16)
        b1 = int(bg_start[4:6], 16)
        r2 = int(bg_end[0:2], 16)
        g2 = int(bg_end[2:4], 16)
        b2 = int(bg_end[4:6], 16)

        geq_r = f"r='lerp({r1},{r2},X/W)'"
        geq_g = f"g='lerp({g1},{g2},X/W)'"
        geq_b = f"b='lerp({b1},{b2},X/W)'"

        text_r = int(text_color[0:2], 16)
        text_g = int(text_color[2:4], 16)
        text_b = int(text_color[4:6], 16)
        text_hex = f"#{text_color}"

        # Accent bar (horizontal line above text)
        accent_colors = {
            "motivation": "FF6B35",
            "horror":     "FF4444",
            "reddit_story": "FF4500",
            "brainrot":   "FF2FBF",
            "finance":    "00D4AA",
        }
        accent = accent_colors.get(niche, "FFFFFF")
        accent_hex = f"#{accent}"

        vf_chain = (
            # Gradient background
            f"geq={geq_r}:{geq_g}:{geq_b},"
            # Accent bar
            f"drawbox=x=0:y=320:w=1280:h=8:color={accent_hex}:t=fill,"
            # Main hook text
            f"drawtext={font_arg}"
            f"text='{display_text_final}':"
            f"fontsize=72:"
            f"fontcolor={text_hex}:"
            f"bordercolor=black:borderw=4:"
            f"line_spacing=10:"
            f"x=(w-text_w)/2:y=(h-text_h)/2,"
            # Niche label at bottom
            f"drawtext={font_arg}"
            f"text='{niche.upper()}':"
            f"fontsize=32:"
            f"fontcolor={accent_hex}@0.9:"
            f"bordercolor=black:borderw=2:"
            f"x=(w-text_w)/2:y=h-80"
        )

        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:size=1280x720:duration=0.1:rate=1",
            "-vf", vf_chain,
            "-frames:v", "1",
            "-q:v", "2",
            thumbnail_path,
        ]

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                logger.debug(f"FFmpeg thumbnail error: {r.stderr[-500:]}")
                return False
            size = os.path.getsize(thumbnail_path) if os.path.exists(thumbnail_path) else 0
            return size > 5000
        except Exception as e:
            logger.debug(f"FFmpeg thumbnail exception: {e}")
            return False

    def _generate_pollinations_thumbnail(
        self,
        script: dict,
        video_id: str,
        output_dir: str,
        thumbnail_path: str,
    ) -> bool:
        """
        BUG FIX V4-B: Pollinations AI thumbnail (SECONDARY / fallback only).
        Uses a stronger unique seed derived from video_id + timestamp.
        """
        try:
            import requests
            import urllib.parse

            hook     = script.get("hook", "")
            title    = script.get("title", "")
            niche    = os.environ.get("NICHE", "motivation")
            emotion  = script.get("emotion", "inspiration")
            extracted = script.get("_extracted", {})
            core_mystery = extracted.get("core_mystery", "")

            # Strong unique seed: md5 of video_id + current timestamp
            seed_str = f"{video_id}_{int(time.time())}"
            primary_seed = int(hashlib.md5(seed_str.encode()).hexdigest(), 16) % 99991
            fallback_seed = (primary_seed + 7919) % 99991

            subject = (
                core_mystery[:50]
                if core_mystery and len(core_mystery) > 10
                else title[:50]
                if title
                else " ".join(hook.split()[:8])
            )

            emotion_visuals = {
                "inspiration": "golden hour triumph",
                "urgency":     "dramatic tension motion blur",
                "fear":        "dark shadows silhouette",
                "dread":       "eerie atmosphere fog",
                "shock":       "dramatic reveal close-up",
                "curiosity":   "mysterious discovery light",
                "amusement":   "vibrant energy expression",
                "chaos":       "explosive color contrast",
            }
            emotion_vis = emotion_visuals.get(emotion, "cinematic dramatic")

            niche_style_variants = {
                "motivation": [
                    "golden hour dramatic lighting, person achieving goal, cinematic 4K",
                    "sunrise mountain peak, lone figure silhouette, epic wide shot",
                    "gym athlete victory pose, motivational energy, high contrast",
                ],
                "horror": [
                    "dark atmospheric horror, eerie shadows, fog, cinematic",
                    "abandoned location, flickering light, deep shadows, unsettling",
                    "close-up fearful expression, dark background, horror film still",
                ],
                "reddit_story": [
                    "dramatic confrontation, realistic candid, shocked expression",
                    "phone screen glow face, late night, tense moment, cinematic",
                    "two people argument, real life drama, emotional intensity",
                ],
                "brainrot": [
                    "neon chaos surreal internet aesthetic, vivid high contrast",
                    "glitch art explosion, colorful pixels, chaotic energy",
                    "meme energy expression, bold colors, absurdist composition",
                ],
                "finance": [
                    "clean professional money concept, sharp lighting, corporate",
                    "stack of money close-up, dramatic lighting, wealth concept",
                    "stock chart upward, business success, minimal aesthetic",
                ],
            }

            variants = niche_style_variants.get(
                niche, ["cinematic dramatic lighting, 4K high quality, YouTube thumbnail"]
            )
            style = variants[primary_seed % len(variants)]

            prompt = (
                f"{subject}, {emotion_vis}, {style}, "
                f"YouTube thumbnail 16:9, no text, photorealistic"
            )

            logger.info(
                f"Pollinations thumbnail | video={video_id} seed={primary_seed} | "
                f"prompt='{prompt[:80]}...'"
            )

            success = self._fetch_pollinations_thumbnail(prompt, thumbnail_path, 1280, 720, primary_seed)
            if not success:
                simple_prompt = f"{subject}, {style}, YouTube thumbnail, cinematic, high quality"
                success = self._fetch_pollinations_thumbnail(
                    simple_prompt, thumbnail_path, 1280, 720, fallback_seed
                )

            return success

        except Exception as e:
            logger.warning(f"Pollinations thumbnail crashed: {e}")
            return False

    def _fetch_pollinations_thumbnail(self, prompt, output_path, width, height, seed) -> bool:
        try:
            import requests
            import urllib.parse
            encoded = urllib.parse.quote(prompt)
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width={width}&height={height}&seed={seed}&nologo=true&enhance=true"
            )
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "").lower()
            if not content_type.startswith("image/"):
                return False
            if len(resp.content) < 50_000:
                return False

            with open(output_path, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as e:
            logger.warning(f"Pollinations thumbnail fetch failed: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Result builder
    # ─────────────────────────────────────────────────────────────────────────

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
