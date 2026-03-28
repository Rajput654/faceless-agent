"""
agents/video_composer.py

UPGRADED: Forwards emotion and kb_preset from the script/visual pipeline
into the VideoMCPServer so emotion-aware Ken Burns and dynamic music
ducking are applied correctly.
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
        emotion:        str  = None,   # NEW: forwarded from VideoWorkflow
        kb_preset:      str  = None,   # NEW: forwarded from VisualDirectorAgent
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

        # Resolve emotion from script if not explicitly passed
        _emotion  = emotion  or script.get("emotion", "inspiration")
        _kb_preset = kb_preset or None  # video_server will resolve from emotion if None

        output_path = f"{output_dir}/{video_id}_final.mp4"

        logger.info(
            f"Composing | emotion={_emotion} | kb_preset={_kb_preset} | "
            f"visuals={len(image_paths)} | "
            f"captions={caption_result.get('caption_style', 'unknown')} | "
            f"music={'yes' if music_path else 'no'}"
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
            emotion       = _emotion,    # NEW
            kb_preset     = _kb_preset,  # NEW
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
