"""
agents/video_composer.py

FIXED v2 — BUG FIX: music_path guard was too strict

BUG: The original code read:
    music_path = music_result.get("music_path") if music_result.get("success") else None

This meant if MusicDirectorAgent returned success=False (which it did
whenever all sources failed in the old code), music_path was forced to None
even if a silence file was available. This caused _dynamic_music_mix() to
be skipped entirely, producing videos with no background music track.

FIX: Read music_path directly from the result dict. The MusicDirectorAgent
now always returns success=True with either real music or a silence file path.
The only valid "no music" state is when music_path is explicitly None (which
only happens if even ffmpeg failed to generate silence). In that case we
correctly skip mixing rather than crashing.

Also added: music source logging so you can see in CI whether real music,
fallback CDN music, or silence was used.
"""
import os
from loguru import logger
from mcp_servers.video_server import VideoMCPServer


class VideoComposerAgent:
    def __init__(self, config):
        self.config       = config
        self.video_server = VideoMCPServer()
        self.video_config = config.get("video", {})
        self.music_config = config.get("music", {})

    def run(
        self,
        script:         dict,
        voice_result:   dict,
        visual_result:  dict,
        caption_result: dict,
        music_result:   dict,
        video_id:       str,
        output_dir:     str  = "/tmp",
        emotion:        str  = None,
        kb_preset:      str  = None,
        *args, **kwargs,
    ):
        logger.info(f"VideoComposerAgent composing video: {video_id}")

        audio_path    = voice_result.get("audio_path")
        image_paths   = visual_result.get("image_paths", [])
        subtitle_path = (
            caption_result.get("ass_path")
            or caption_result.get("srt_path")
        )

        # BUG FIX: Read music_path directly — do NOT gate on success flag.
        # MusicDirectorAgent now always returns success=True, and music_path
        # is None only when even the silence fallback failed (extremely rare).
        # Old code: music_path = music_result.get("music_path") if music_result.get("success") else None
        music_path = music_result.get("music_path") if music_result else None

        # Log what music source we're using so it's visible in CI
        if music_path and os.path.exists(music_path):
            music_source = music_result.get("source", "unknown")
            music_size = os.path.getsize(music_path) // 1024
            if music_source == "generated_silence":
                logger.warning(f"Music: using silence track ({music_size} KB) — no real music found")
            else:
                logger.info(f"Music: {music_source} ({music_size} KB) → will mix at volume {self.music_config.get('volume_reduction', 0.12)}")
        else:
            logger.warning("Music: no track available — video will have voice only")
            music_path = None  # ensure we don't pass a bad path to ffmpeg

        _emotion   = emotion  or script.get("emotion", "inspiration")
        _kb_preset = kb_preset or None
        hook_text  = script.get("hook", "")
        output_path = f"{output_dir}/{video_id}_final.mp4"

        caption_style = caption_result.get("caption_style", "unknown")
        caption_pos   = caption_result.get("position", "unknown")

        logger.info(
            f"Composing | emotion={_emotion} | kb_preset={_kb_preset} | "
            f"visuals={len(image_paths)} | "
            f"captions={caption_style} ({caption_pos}) | "
            f"music={'yes (' + (music_result.get('source','?') if music_result else 'none') + ')' if music_path else 'no'} | "
            f"hook_card={'yes' if hook_text else 'no'}"
        )

        result = self.video_server.call(
            "compose_video",
            image_paths   = image_paths,
            audio_path    = audio_path,
            output_path   = output_path,
            subtitle_path = subtitle_path,
            music_path    = music_path,
            music_volume  = self.music_config.get("volume_reduction", 0.12),
            fps           = self.video_config.get("fps", 30),
            width         = 1080,
            height        = 1920,
            emotion       = _emotion,
            kb_preset     = _kb_preset,
            hook_text     = hook_text,
        )

        if result.get("success"):
            logger.success(f"Final video ready: {output_path}")
            return {
                "success":          True,
                "final_video_path": output_path,
                "file_size_bytes":  result.get("file_size_bytes", 0),
                "duration_seconds": result.get("duration_seconds", 0),
            }
        else:
            logger.error(f"Video composition failed: {result.get('error')}")
            return {
                "success":          False,
                "final_video_path": None,
                "error":            result.get("error"),
            }
