"""
agents/video_composer.py

FIXED v3 — Music blocking bugs resolved:

BUG FIX B6 — MUSIC FILE EXISTENCE NOT VALIDATED AT USE TIME:
  music_path could be received as a valid string that existed when
  MusicDirectorAgent returned it, but by the time VideoComposerAgent
  uses it (after several other pipeline steps), the file might have been
  moved, renamed, or deleted (especially in batch mode with parallel jobs
  sharing /tmp). Fix: validate existence immediately before passing to
  video_server, and log clearly if the file has gone missing.

ALSO: Added more detailed logging of music source/size so it's easy to
confirm in CI whether real music or silence is being used.
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

        # BUG FIX B6 (original fix from v2): Read music_path directly —
        # do NOT gate on success flag.
        music_path = music_result.get("music_path") if music_result else None

        # BUG FIX B6 (new): Validate file existence AT USE TIME, not just
        # at receipt time. In batch mode, /tmp files can be cleaned between steps.
        if music_path:
            if os.path.exists(music_path):
                music_source = music_result.get("source", "unknown")
                music_size   = os.path.getsize(music_path) // 1024
                if music_source in ("generated_ambient", "none"):
                    logger.warning(
                        f"Music: synthetic ambient track ({music_size} KB) — "
                        f"no CDN music was found. Video will have ambient background."
                    )
                else:
                    logger.info(
                        f"Music: {music_source} ({music_size} KB) → "
                        f"will mix at volume {self.music_config.get('volume_reduction', 0.12)}"
                    )
            else:
                # File disappeared between MusicDirector completing and now
                logger.warning(
                    f"Music file no longer exists at use time: {music_path} "
                    f"(may have been moved or deleted between pipeline steps). "
                    f"Video will be voice-only."
                )
                music_path = None
        else:
            logger.warning("Music: no track available — video will have voice only")

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
