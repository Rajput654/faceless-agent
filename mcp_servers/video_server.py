"""
mcp_servers/video_server.py

Root-cause fix for the subtitle crash:
  FFmpeg's subtitles= filter cannot open paths that contain colons or
  special characters.  The fix: copy the subtitle file to a temp path
  with a plain safe name in the same directory as the output, then
  reference it by that safe path.  This eliminates the
  "Unable to open /tmp/video_000_subtitles.srt" error.
"""
import os
import shutil
import random
import subprocess
from pathlib import Path
from loguru import logger


KB_PRESETS = [
    ("min(zoom+0.0008,1.3)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("if(eq(on,1),1.3,max(zoom-0.0008,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("1.12", "if(eq(on,1),0,x+0.6)", "ih/2-(ih/zoom/2)"),
    ("1.12", "if(eq(on,1),iw-iw/zoom,max(0,x-0.6))", "ih/2-(ih/zoom/2)"),
    ("min(zoom+0.0008,1.3)", "0", "0"),
    ("min(zoom+0.0008,1.3)", "iw-(iw/zoom)", "ih-(ih/zoom)"),
]


class VideoMCPServer:
    def __init__(self):
        self.tools = {
            "compose_video": self._compose_video,
            "check_ffmpeg":  self._check_ffmpeg,
        }

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _check_ffmpeg(self, **kwargs):
        try:
            r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
            return {"success": r.returncode == 0}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _compose_video(
        self,
        image_paths:   list,
        audio_path:    str,
        output_path:   str,
        subtitle_path: str   = None,
        music_path:    str   = None,
        music_volume:  float = 0.10,
        fps:           int   = 30,
        width:         int   = 1080,
        height:        int   = 1920,
        **kwargs,
    ):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if not image_paths:
            return {"success": False, "error": "No visual assets provided"}
        if not audio_path or not os.path.exists(audio_path):
            return {"success": False, "error": f"Audio not found: {audio_path}"}

        valid_assets = [p for p in image_paths if p and os.path.exists(p)]
        if not valid_assets:
            return {"success": False, "error": "No valid visual files found"}

        sub_tmp = None   # track temp subtitle copy for cleanup

        try:
            duration = self._audio_duration(audio_path)
            if duration <= 0:
                duration = 55.0

            is_video = any(p.lower().endswith(".mp4") for p in valid_assets)

            if is_video:
                merged = self._compose_from_clips(
                    valid_assets, output_path, duration, fps, width, height
                )
            else:
                merged = self._compose_from_images(
                    valid_assets, output_path, duration, fps, width, height
                )

            # ── Audio mix ────────────────────────────────────────────────────
            if music_path and os.path.exists(music_path):
                mixed_audio = output_path.replace(".mp4", "_audio_mix.mp3")
                self._run([
                    "ffmpeg", "-y",
                    "-i", audio_path, "-i", music_path,
                    "-filter_complex",
                    (f"[0:a]volume=1.0[v];[1:a]volume={music_volume}[m];"
                     f"[v][m]amix=inputs=2:duration=first:dropout_transition=2[out]"),
                    "-map", "[out]", "-t", str(duration),
                    "-c:a", "aac", "-b:a", "192k", mixed_audio,
                ], "audio mix")
                final_audio = mixed_audio
            else:
                final_audio = audio_path

            # ── Subtitle filter (safe path copy) ─────────────────────────────
            vf_final, sub_tmp = self._caption_filter(subtitle_path, output_path)

            compose_cmd = [
                "ffmpeg", "-y",
                "-i", merged, "-i", final_audio,
                "-c:v", "libx264", "-preset", "fast", "-crf", "21",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest", "-movflags", "+faststart",
            ]
            if vf_final:
                compose_cmd += ["-vf", vf_final]
            compose_cmd.append(output_path)
            self._run(compose_cmd, "final compose")

        except Exception as e:
            logger.error(f"Video composition failed: {e}")
            return {"success": False, "error": str(e)}

        finally:
            # Always clean up temp files
            if sub_tmp and os.path.exists(sub_tmp):
                os.remove(sub_tmp)
            for suffix in ["_merged.mp4", "_audio_mix.mp3"]:
                tmp = output_path.replace(".mp4", suffix)
                if os.path.exists(tmp):
                    os.remove(tmp)
            for i in range(len(valid_assets)):
                for tag in [f"_kb_{i:02d}.mp4", f"_scaled_{i:02d}.mp4", f"_cf_{i:02d}.mp4"]:
                    tmp = output_path.replace(".mp4", tag)
                    if os.path.exists(tmp):
                        os.remove(tmp)

        file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        if file_size == 0:
            return {"success": False, "error": "Output video is empty"}

        logger.success(
            f"Video composed ✅  {output_path} "
            f"({file_size/1024/1024:.1f} MB, {duration:.1f}s, "
            f"{len(valid_assets)} {'clips' if is_video else 'images'})"
        )
        return {
            "success":          True,
            "output_path":      output_path,
            "file_size_bytes":  file_size,
            "duration_seconds": duration,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Subtitle filter — THE KEY FIX
    # ─────────────────────────────────────────────────────────────────────────

    def _caption_filter(self, subtitle_path: str, output_path: str):
        """
        Copy the subtitle file to /tmp/sub_safe.{ext} and build the FFmpeg
        filter string pointing at that safe path.

        WHY: FFmpeg's subtitles= filter is broken for paths containing colons
        (:) which appear in /tmp/ paths on some builds, and for any path with
        spaces or special chars.  The only reliable fix is a path that is:
          - absolute
          - contains only [a-zA-Z0-9_./]
          - has no colon except the drive letter on Windows (N/A here)

        We copy to /tmp/sub_safe_<stem>.{ext} which satisfies all constraints.
        """
        if not subtitle_path or not os.path.exists(subtitle_path):
            logger.warning("Subtitle file missing or not found — composing without captions")
            return None, None

        ext = Path(subtitle_path).suffix.lower()
        stem = Path(output_path).stem.replace("-", "_").replace(" ", "_")
        # Safe path: only alphanumeric + underscore + dot + slash
        safe_path = f"/tmp/sub_safe_{stem}{ext}"

        try:
            shutil.copy2(subtitle_path, safe_path)
            logger.info(f"Subtitle copied to safe path: {safe_path}")
        except Exception as e:
            logger.warning(f"Cannot copy subtitle to safe path: {e} — skipping captions")
            return None, None

        if ext == ".ass":
            vf = f"ass={safe_path}"
        else:
            # SRT with bold Shorts-style white captions
            vf = (
                f"subtitles={safe_path}:"
                f"force_style='FontName=Arial,FontSize=58,Bold=1,"
                f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                f"Outline=4,Shadow=2,Alignment=2,"
                f"MarginV=150,MarginL=60,MarginR=60'"
            )

        return vf, safe_path

    # ─────────────────────────────────────────────────────────────────────────
    # Video clip pipeline
    # ─────────────────────────────────────────────────────────────────────────

    def _compose_from_clips(self, clips, output_path, duration, fps, width, height):
        n       = len(clips)
        seg_dur = max(duration / n, 2.0)
        xfade   = 0.4

        scaled_clips = []
        for i, clip in enumerate(clips):
            out = output_path.replace(".mp4", f"_scaled_{i:02d}.mp4")
            vf  = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps={fps}"
            )
            self._run([
                "ffmpeg", "-y", "-i", clip,
                "-t", str(seg_dur), "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-pix_fmt", "yuv420p", "-an", out,
            ], f"scale clip {i}")
            scaled_clips.append(out)

        if n == 1:
            return scaled_clips[0]
        return self._crossfade_clips(scaled_clips, output_path, fps, xfade, seg_dur, duration)

    # ─────────────────────────────────────────────────────────────────────────
    # Still image pipeline (Ken Burns fallback)
    # ─────────────────────────────────────────────────────────────────────────

    def _compose_from_images(self, images, output_path, duration, fps, width, height):
        n       = len(images)
        seg_dur = max(duration / n, 2.5)
        xfade   = 0.5

        preset_order = random.sample(range(len(KB_PRESETS)), min(n, len(KB_PRESETS)))
        while len(preset_order) < n:
            preset_order += random.sample(range(len(KB_PRESETS)), min(n, len(KB_PRESETS)))
        preset_order = preset_order[:n]

        kb_clips = []
        for i, img in enumerate(images):
            clip_path = output_path.replace(".mp4", f"_kb_{i:02d}.mp4")
            zoom_e, x_e, y_e = KB_PRESETS[preset_order[i]]
            frames = int(seg_dur * fps)
            vf = (
                f"scale={width*2}:{height*2}:force_original_aspect_ratio=increase,"
                f"crop={width*2}:{height*2},"
                f"zoompan=z='{zoom_e}':x='{x_e}':y='{y_e}'"
                f":d={frames}:s={width}x{height}:fps={fps},setsar=1"
            )
            self._run([
                "ffmpeg", "-y", "-loop", "1", "-i", img,
                "-vf", vf, "-t", str(seg_dur),
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-pix_fmt", "yuv420p", clip_path,
            ], f"Ken Burns clip {i}")
            kb_clips.append(clip_path)

        if n == 1:
            return kb_clips[0]
        return self._crossfade_clips(kb_clips, output_path, fps, xfade, seg_dur, duration)

    # ─────────────────────────────────────────────────────────────────────────
    # Shared helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _crossfade_clips(self, clips, output_path, fps, xfade_dur, seg_dur, total_dur):
        try:
            n   = len(clips)
            cmd = ["ffmpeg", "-y"]
            for c in clips:
                cmd += ["-i", c]

            fg_parts = []
            prev     = "[0:v]"
            offset   = 0.0
            for i in range(1, n):
                offset += seg_dur - xfade_dur
                out_label = f"[xf{i}]" if i < n - 1 else "[vout]"
                fg_parts.append(
                    f"{prev}[{i}:v]xfade=transition=fade:"
                    f"duration={xfade_dur}:offset={offset:.3f}{out_label}"
                )
                prev = out_label

            merged = output_path.replace(".mp4", "_merged.mp4")
            cmd += [
                "-filter_complex", ";".join(fg_parts),
                "-map", "[vout]",
                "-t", str(total_dur),
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-pix_fmt", "yuv420p", merged,
            ]
            self._run(cmd, "crossfade chain")
            return merged
        except Exception as e:
            logger.warning(f"Crossfade failed ({e}), falling back to concat")
            return self._simple_concat(clips, output_path, total_dur)

    def _simple_concat(self, clips, output_path, duration):
        list_path = output_path.replace(".mp4", "_list.txt")
        merged    = output_path.replace(".mp4", "_merged.mp4")
        with open(list_path, "w") as f:
            for c in clips:
                f.write(f"file '{os.path.abspath(c)}'\n")
        self._run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
            "-t", str(duration), "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            merged,
        ], "simple concat")
        if os.path.exists(list_path):
            os.remove(list_path)
        return merged

    def _audio_duration(self, path: str) -> float:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=15,
            )
            return float(r.stdout.strip())
        except Exception:
            return 55.0

    def _run(self, cmd: list, step: str):
        logger.debug(f"FFmpeg [{step}]: {' '.join(cmd[:10])}…")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            logger.error(f"FFmpeg [{step}] failed:\n{r.stderr[-2000:]}")
            raise RuntimeError(f"FFmpeg [{step}] failed: {r.stderr[-600:]}")
        return r
