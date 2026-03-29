"""
agents/video_composer.py

UPGRADED v2: Forwards emotion, kb_preset, AND hook_text to the VideoMCPServer.

Key changes from v1:
  - hook_text is now extracted from the script and forwarded to VideoMCPServer
    so the opening hook card is generated automatically.
  - emotion and kb_preset forwarding unchanged from v1.
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
        music_path    = music_result.get("music_path") if music_result.get("success") else None

        _emotion   = emotion  or script.get("emotion", "inspiration")
        _kb_preset = kb_preset or None

        # ── NEW: Extract hook text for opening card ────────────────────────
        # Use the hook field from the script (already sharpened by Pass 3)
        hook_text = script.get("hook", "")

        output_path = f"{output_dir}/{video_id}_final.mp4"

        caption_style = caption_result.get('caption_style', 'unknown')
        caption_pos   = caption_result.get('position', 'unknown')

        logger.info(
            f"Composing | emotion={_emotion} | kb_preset={_kb_preset} | "
            f"visuals={len(image_paths)} | "
            f"captions={caption_style} ({caption_pos}) | "
            f"music={'yes' if music_path else 'no'} | "
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
            hook_text     = hook_text,   # NEW
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
