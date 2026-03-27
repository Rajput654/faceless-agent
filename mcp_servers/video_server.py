"""
mcp_servers/video_server.py

FFmpeg-based video composition with:
  • Ken Burns effect  — slow zoom-in or pan on every image (no static frames)
  • Crossfade transitions between images
  • Bold ASS/SRT captions burned in
  • Voice + music mixing with ducking
  • 1080×1920 portrait output (YouTube Shorts / TikTok / Reels)
"""
import os
import random
import subprocess
from pathlib import Path
from loguru import logger


# ─────────────────────────────────────────────────────────────────────────────
# Ken Burns presets  (zoompan filter strings, filled in at runtime)
# Each entry is (zoom_expr, x_expr, y_expr) for ffmpeg zoompan
# ─────────────────────────────────────────────────────────────────────────────
KB_PRESETS = [
    # Slow zoom in from centre
    ("min(zoom+0.0008,1.3)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    # Slow zoom out (start zoomed, pull back)
    ("if(eq(on,1),1.3,max(zoom-0.0008,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    # Pan left to right
    ("1.12", "if(eq(on,1),0,x+0.6)", "ih/2-(ih/zoom/2)"),
    # Pan right to left
    ("1.12", "if(eq(on,1),iw-iw/zoom,max(0,x-0.6))", "ih/2-(ih/zoom/2)"),
    # Zoom in top-left
    ("min(zoom+0.0008,1.3)", "0", "0"),
    # Zoom in bottom-right
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

    # ── public tools ──────────────────────────────────────────────────────────

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
        subtitle_path: str  = None,
        music_path:    str  = None,
        music_volume:  float = 0.10,
        fps:           int  = 30,
        width:         int  = 1080,
        height:        int  = 1920,
        **kwargs,
    ):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if not image_paths:
            return {"success": False, "error": "No images provided"}
        if not audio_path or not os.path.exists(audio_path):
            return {"success": False, "error": f"Audio not found: {audio_path}"}

        valid_imgs = [p for p in image_paths if os.path.exists(p)]
        if not valid_imgs:
            return {"success": False, "error": "No valid image files"}

        try:
            duration     = self._audio_duration(audio_path)
            if duration <= 0:
                duration = 55.0

            xfade_dur    = 0.5                          # seconds of crossfade
            n            = len(valid_imgs)
            seg_dur      = duration / n                 # each image's screen time
            # Each segment must be long enough to overlap on both sides
            seg_dur      = max(seg_dur, xfade_dur * 2 + 0.5)
            total_raw    = seg_dur * n                  # total before crossfade trimming

            # ── Step 1: Ken Burns per-image clips ─────────────────────────────
            kb_clips = []
            preset_order = random.sample(range(len(KB_PRESETS)), min(n, len(KB_PRESETS)))
            while len(preset_order) < n:
                preset_order += random.sample(range(len(KB_PRESETS)), min(n, len(KB_PRESETS)))
            preset_order = preset_order[:n]

            for i, img in enumerate(valid_imgs):
                clip_path = output_path.replace(".mp4", f"_kb_{i:02d}.mp4")
                zoom_e, x_e, y_e = KB_PRESETS[preset_order[i]]
                frames = int(seg_dur * fps)

                # zoompan produces frames at 25fps by default; we force our fps
                # scale → zoompan → scale to exact resolution
                vf = (
                    f"scale={width*2}:{height*2}:force_original_aspect_ratio=increase,"
                    f"crop={width*2}:{height*2},"
                    f"zoompan=z='{zoom_e}':x='{x_e}':y='{y_e}'"
                    f":d={frames}:s={width}x{height}:fps={fps},"
                    f"setsar=1"
                )
                cmd = [
                    "ffmpeg", "-y",
                    "-loop", "1", "-i", img,
                    "-vf", vf,
                    "-t", str(seg_dur),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                    "-pix_fmt", "yuv420p",
                    clip_path,
                ]
                self._run(cmd, f"Ken Burns clip {i}")
                kb_clips.append(clip_path)

            # ── Step 2: crossfade clips together ──────────────────────────────
            if n == 1:
                merged = kb_clips[0]
            else:
                merged = self._crossfade_clips(
                    kb_clips, output_path, fps, xfade_dur, seg_dur, duration, width, height
                )

            # ── Step 3: mix audio (voice + optional music) ────────────────────
            if music_path and os.path.exists(music_path):
                mixed_audio = output_path.replace(".mp4", "_audio_mix.mp3")
                self._run([
                    "ffmpeg", "-y",
                    "-i", audio_path,
                    "-i", music_path,
                    "-filter_complex",
                    f"[0:a]volume=1.0[v];[1:a]volume={music_volume}[m];"
                    f"[v][m]amix=inputs=2:duration=first:dropout_transition=2[out]",
                    "-map", "[out]",
                    "-t", str(duration),
                    "-c:a", "aac", "-b:a", "192k",
                    mixed_audio,
                ], "audio mix")
                final_audio = mixed_audio
            else:
                final_audio = audio_path

            # ── Step 4: combine video + audio + captions ──────────────────────
            if subtitle_path and os.path.exists(subtitle_path):
                ext = Path(subtitle_path).suffix.lower()
                if ext == ".ass":
                    sub_filter = f"ass='{subtitle_path}'"
                else:
                    # Bold white text, black outline, bottom-centre
                    sub_filter = (
                        f"subtitles='{subtitle_path}':"
                        f"force_style='FontName=Arial,FontSize=68,Bold=1,"
                        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                        f"Outline=4,Shadow=2,Alignment=2,"
                        f"MarginV=120'"
                    )
                vf_final = sub_filter
            else:
                vf_final = None

            compose_cmd = [
                "ffmpeg", "-y",
                "-i", merged,
                "-i", final_audio,
                "-c:v", "libx264", "-preset", "fast", "-crf", "21",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
            ]
            if vf_final:
                compose_cmd += ["-vf", vf_final]
            compose_cmd.append(output_path)
            self._run(compose_cmd, "final compose")

            # ── Cleanup temp files ────────────────────────────────────────────
            for p in kb_clips:
                if os.path.exists(p):
                    os.remove(p)
            for suffix in ["_audio_mix.mp3"]:
                tmp = output_path.replace(".mp4", suffix)
                if os.path.exists(tmp):
                    os.remove(tmp)
            # Remove intermediate crossfade files
            for i in range(n):
                for tag in [f"_cf_{i:02d}.mp4"]:
                    tmp = output_path.replace(".mp4", tag)
                    if os.path.exists(tmp):
                        os.remove(tmp)

            file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if file_size == 0:
                return {"success": False, "error": "Output video is empty"}

            logger.success(
                f"Video composed ✅  {output_path} "
                f"({file_size/1024/1024:.1f} MB, {duration:.1f}s, {n} scenes)"
            )
            return {
                "success":          True,
                "output_path":      output_path,
                "file_size_bytes":  file_size,
                "duration_seconds": duration,
            }

        except Exception as e:
            logger.error(f"Video composition failed: {e}")
            return {"success": False, "error": str(e)}

    # ── Ken Burns crossfade chain ─────────────────────────────────────────────

    def _crossfade_clips(
        self, clips, output_path, fps, xfade_dur, seg_dur, total_audio_dur, width, height
    ):
        """
        Chain N clips with xfade transitions using a single filtergraph.
        Falls back to simple concat if anything goes wrong.
        """
        try:
            n = len(clips)
            # Build inputs
            cmd = ["ffmpeg", "-y"]
            for c in clips:
                cmd += ["-i", c]

            # Build filtergraph:
            # Each clip feeds into the next xfade; offset accumulates
            fg_parts = []
            # Label first input
            prev = "[0:v]"
            accumulated_offset = 0.0

            for i in range(1, n):
                accumulated_offset += seg_dur - xfade_dur
                out_label = f"[xf{i}]" if i < n - 1 else "[vout]"
                fg_parts.append(
                    f"{prev}[{i}:v]xfade=transition=fade:"
                    f"duration={xfade_dur}:offset={accumulated_offset:.3f}{out_label}"
                )
                prev = out_label

            merged = output_path.replace(".mp4", "_merged.mp4")
            cmd += [
                "-filter_complex", ";".join(fg_parts),
                "-map", "[vout]",
                "-t", str(total_audio_dur),
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-pix_fmt", "yuv420p",
                merged,
            ]
            self._run(cmd, "crossfade chain")
            return merged

        except Exception as e:
            logger.warning(f"Crossfade failed ({e}), falling back to simple concat")
            return self._simple_concat(clips, output_path, total_audio_dur)

    def _simple_concat(self, clips, output_path, duration):
        """Fallback: plain concat without transitions."""
        list_path = output_path.replace(".mp4", "_list.txt")
        merged    = output_path.replace(".mp4", "_merged.mp4")
        with open(list_path, "w") as f:
            for c in clips:
                f.write(f"file '{os.path.abspath(c)}'\n")
        self._run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            merged,
        ], "simple concat")
        if os.path.exists(list_path):
            os.remove(list_path)
        return merged

    # ── utilities ─────────────────────────────────────────────────────────────

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
        logger.debug(f"FFmpeg [{step}]: {' '.join(cmd[:8])}…")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            logger.error(f"FFmpeg [{step}] failed:\n{r.stderr[-1500:]}")
            raise RuntimeError(f"FFmpeg [{step}] failed: {r.stderr[-600:]}")
        return r
