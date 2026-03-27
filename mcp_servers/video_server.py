"""
mcp_servers/video_server.py
FFmpeg-based video composition: images + audio + captions → MP4.
"""
import os
import subprocess
from pathlib import Path
from loguru import logger


class VideoMCPServer:
    def __init__(self):
        self.tools = {
            "compose_video": self._compose_video,
            "check_ffmpeg": self._check_ffmpeg,
        }

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _check_ffmpeg(self, **kwargs):
        try:
            result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
            return {"success": result.returncode == 0}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _compose_video(
        self,
        image_paths: list,
        audio_path: str,
        output_path: str,
        subtitle_path: str = None,
        music_path: str = None,
        music_volume: float = 0.12,
        fps: int = 30,
        width: int = 1080,
        height: int = 1920,
        **kwargs,
    ):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if not image_paths:
            return {"success": False, "error": "No images provided"}
        if not audio_path or not os.path.exists(audio_path):
            return {"success": False, "error": f"Audio file not found: {audio_path}"}

        # Filter existing images
        valid_images = [p for p in image_paths if os.path.exists(p)]
        if not valid_images:
            return {"success": False, "error": "No valid image files found"}

        try:
            # Step 1: Get audio duration
            duration = self._get_audio_duration(audio_path)
            if duration <= 0:
                duration = 60.0

            # Step 2: Build image slideshow
            img_duration = duration / len(valid_images)
            slideshow_path = output_path.replace(".mp4", "_slideshow.mp4")

            # Create image list file for FFmpeg concat
            list_path = output_path.replace(".mp4", "_imglist.txt")
            with open(list_path, "w") as f:
                for img in valid_images:
                    f.write(f"file '{os.path.abspath(img)}'\n")
                    f.write(f"duration {img_duration:.3f}\n")
                # Add last image again (FFmpeg concat requirement)
                f.write(f"file '{os.path.abspath(valid_images[-1])}'\n")

            # Step 3: Create slideshow from images
            slideshow_cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1",
                "-r", str(fps),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-t", str(duration),
                slideshow_path
            ]
            self._run_cmd(slideshow_cmd, "image slideshow")

            # Step 4: Mix audio (voice + optional music)
            if music_path and os.path.exists(music_path):
                mixed_audio = output_path.replace(".mp4", "_mixed_audio.mp3")
                mix_cmd = [
                    "ffmpeg", "-y",
                    "-i", audio_path,
                    "-i", music_path,
                    "-filter_complex",
                    f"[0:a]volume=1.0[voice];[1:a]volume={music_volume}[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2[out]",
                    "-map", "[out]",
                    "-t", str(duration),
                    "-c:a", "aac", "-b:a", "192k",
                    mixed_audio
                ]
                self._run_cmd(mix_cmd, "audio mixing")
                final_audio = mixed_audio
            else:
                final_audio = audio_path

            # Step 5: Combine video + audio (with optional subtitles)
            if subtitle_path and os.path.exists(subtitle_path):
                sub_ext = Path(subtitle_path).suffix.lower()
                if sub_ext == ".ass":
                    vf_filter = f"scale={width}:{height},ass='{subtitle_path}'"
                else:
                    vf_filter = f"scale={width}:{height},subtitles='{subtitle_path}':force_style='FontSize=72,Bold=1,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=4,Alignment=2'"

                compose_cmd = [
                    "ffmpeg", "-y",
                    "-i", slideshow_path,
                    "-i", final_audio,
                    "-vf", vf_filter,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    output_path
                ]
            else:
                compose_cmd = [
                    "ffmpeg", "-y",
                    "-i", slideshow_path,
                    "-i", final_audio,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    output_path
                ]

            self._run_cmd(compose_cmd, "video composition")

            # Cleanup temp files
            for tmp in [slideshow_path, list_path]:
                if os.path.exists(tmp):
                    os.remove(tmp)
            if music_path and os.path.exists(output_path.replace(".mp4", "_mixed_audio.mp3")):
                os.remove(output_path.replace(".mp4", "_mixed_audio.mp3"))

            file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if file_size == 0:
                return {"success": False, "error": "Output video is empty"}

            logger.success(f"Video composed: {output_path} ({file_size / 1024 / 1024:.1f} MB)")
            return {
                "success": True,
                "output_path": output_path,
                "file_size_bytes": file_size,
                "duration_seconds": duration,
            }

        except Exception as e:
            logger.error(f"Video composition failed: {e}")
            return {"success": False, "error": str(e)}

    def _get_audio_duration(self, audio_path: str) -> float:
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return float(result.stdout.strip())
        except Exception:
            return 60.0

    def _run_cmd(self, cmd: list, step_name: str):
        logger.debug(f"FFmpeg {step_name}: {' '.join(cmd[:6])}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error(f"FFmpeg {step_name} failed:\n{result.stderr[-1000:]}")
            raise RuntimeError(f"FFmpeg {step_name} failed: {result.stderr[-500:]}")
        return result
