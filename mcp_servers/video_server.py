"""
mcp_servers/video_server.py

UPGRADED with three major improvements:

1. EMOTION-AWARE KEN BURNS
   Instead of random zoom presets, the motion is chosen based on the
   emotion field passed from the script:
     horror/curiosity/shock → slow_zoom_in  (creeping dread)
     inspiration/urgency    → zoom_out      (expanding possibility)
     chaos/amusement        → fast_pan      (chaotic energy)

2. DYNAMIC MUSIC DUCKING
   Music volume follows an envelope:
     0s–3s   : duck to near-zero (let the hook land clean)
     3s–end-5s: bring up to 12% (body section)
     end-5s–end: fade to zero   (CTA silence = more powerful)
   Implemented via FFmpeg volume filter with keyframe timestamps.

3. SOUND EFFECTS LAYER
   Loads royalty-free SFX from assets/sfx/ and overlays them:
     - whoosh.mp3  at each scene cut point
     - rumble.mp3  looped under horror scripts
     - riser.mp3   4 seconds before the end (builds to CTA)
   If assets/sfx/ doesn't exist the pipeline skips SFX silently.

Original subtitle safe-path fix is preserved unchanged.
"""
import os
import shutil
import random
import subprocess
from pathlib import Path
from loguru import logger


# ─────────────────────────────────────────────────────────────────────────────
# Ken Burns preset definitions
# Each tuple: (zoom_expr, x_expr, y_expr)
# ─────────────────────────────────────────────────────────────────────────────

KB_PRESETS_BY_EMOTION = {
    # Slow zoom-in — creeping dread, curiosity
    "slow_zoom_in": [
        ("min(zoom+0.0006,1.3)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        ("min(zoom+0.0006,1.25)", "iw/2-(iw/zoom/2)", "ih*0.45-(ih/zoom/2)"),
    ],
    # Zoom-out — expanding possibility, inspiration
    "zoom_out": [
        ("if(eq(on,1),1.3,max(zoom-0.0008,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        ("if(eq(on,1),1.25,max(zoom-0.0006,1.0))", "iw/2-(iw/zoom/2)", "ih*0.4-(ih/zoom/2)"),
    ],
    # Fast pan — chaos, brainrot energy
    "fast_pan": [
        ("1.15", "if(eq(on,1),0,min(x+1.5,iw-iw/zoom))", "ih/2-(ih/zoom/2)"),
        ("1.15", "if(eq(on,1),iw-iw/zoom,max(x-1.5,0))", "ih/2-(ih/zoom/2)"),
        ("1.12", "iw/2-(iw/zoom/2)", "if(eq(on,1),0,min(y+1.2,ih-ih/zoom))"),
    ],
    # Subtle zoom — finance calm authority
    "subtle_zoom": [
        ("min(zoom+0.0003,1.12)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        ("if(eq(on,1),1.12,max(zoom-0.0003,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ],
}

# Fallback random presets (original behaviour)
KB_PRESETS_RANDOM = [
    ("min(zoom+0.0008,1.3)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("if(eq(on,1),1.3,max(zoom-0.0008,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("1.12", "if(eq(on,1),0,x+0.6)", "ih/2-(ih/zoom/2)"),
    ("1.12", "if(eq(on,1),iw-iw/zoom,max(0,x-0.6))", "ih/2-(ih/zoom/2)"),
    ("min(zoom+0.0008,1.3)", "0", "0"),
    ("min(zoom+0.0008,1.3)", "iw-(iw/zoom)", "ih-(ih/zoom)"),
]

# Emotion → KB preset name
EMOTION_TO_KB = {
    "fear":        "slow_zoom_in",
    "horror":      "slow_zoom_in",
    "curiosity":   "slow_zoom_in",
    "shock":       "slow_zoom_in",
    "inspiration": "zoom_out",
    "urgency":     "zoom_out",
    "chaos":       "fast_pan",
    "amusement":   "fast_pan",
    "default":     "slow_zoom_in",
}

# SFX asset paths (relative to project root)
SFX_WHOOSH  = "assets/sfx/whoosh.mp3"
SFX_RUMBLE  = "assets/sfx/rumble.mp3"
SFX_RISER   = "assets/sfx/riser.mp3"


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

    # ─────────────────────────────────────────────────────────────────────────
    # Main compose entry point
    # ─────────────────────────────────────────────────────────────────────────

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
        emotion:       str   = "inspiration",   # NEW: emotion-aware motion
        kb_preset:     str   = None,            # NEW: override from visual_director
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

        sub_tmp = None

        try:
            duration = self._audio_duration(audio_path)
            if duration <= 0:
                duration = 55.0

            is_video = any(p.lower().endswith(".mp4") for p in valid_assets)

            # Resolve KB preset: caller > emotion map > default
            _kb_preset = kb_preset or EMOTION_TO_KB.get(emotion, "slow_zoom_in")

            if is_video:
                merged = self._compose_from_clips(
                    valid_assets, output_path, duration, fps, width, height
                )
            else:
                merged = self._compose_from_images(
                    valid_assets, output_path, duration, fps, width, height,
                    kb_preset=_kb_preset
                )

            # ── UPGRADE 1: Dynamic music ducking ─────────────────────────────
            if music_path and os.path.exists(music_path):
                final_audio = self._dynamic_music_mix(
                    audio_path, music_path, output_path, duration, music_volume
                )
            else:
                final_audio = audio_path

            # ── UPGRADE 2: SFX layer ─────────────────────────────────────────
            sfx_audio = self._apply_sfx_layer(
                final_audio, output_path, duration, emotion,
                num_scenes=len(valid_assets)
            )
            if sfx_audio and os.path.exists(sfx_audio):
                final_audio = sfx_audio

            # ── Subtitle filter (safe path copy — unchanged) ──────────────────
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
            if sub_tmp and os.path.exists(sub_tmp):
                os.remove(sub_tmp)
            for suffix in ["_merged.mp4", "_audio_mix.mp3", "_sfx_mix.mp3"]:
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
            f"kb={_kb_preset}, emotion={emotion})"
        )
        return {
            "success":          True,
            "output_path":      output_path,
            "file_size_bytes":  file_size,
            "duration_seconds": duration,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # UPGRADE 1: Dynamic music ducking
    # ─────────────────────────────────────────────────────────────────────────

    def _dynamic_music_mix(
        self,
        voice_path:   str,
        music_path:   str,
        output_path:  str,
        duration:     float,
        base_volume:  float = 0.10,
    ) -> str:
        """
        Mix voice + music with an envelope on music volume:
          t=0     : music at 0.0  (hook lands clean)
          t=3     : music ramps to base_volume
          t=dur-5 : music stays at base_volume
          t=dur-2 : music fades to 0.0  (CTA silence)

        Uses FFmpeg's volume filter with 'enable' expression for keyframing.
        Falls back to simple flat mix if expression fails.
        """
        mixed_path = output_path.replace(".mp4", "_audio_mix.mp3")

        # Clamp times to valid range
        ramp_in_end  = min(3.0, duration * 0.08)
        ramp_out_start = max(ramp_in_end + 1, duration - 5.0)
        ramp_out_end   = max(ramp_out_start + 1, duration - 1.0)

        # FFmpeg volume envelope using 'volume' filter with if() expressions
        # t = current time in seconds
        volume_expr = (
            f"if(lt(t,{ramp_in_end:.1f}),"
            f"  {base_volume:.3f}*t/{ramp_in_end:.1f},"      # ramp in
            f"  if(lt(t,{ramp_out_start:.1f}),"
            f"    {base_volume:.3f},"                          # sustain
            f"    if(lt(t,{ramp_out_end:.1f}),"
            f"      {base_volume:.3f}*(1-(t-{ramp_out_start:.1f})"
            f"        /({ramp_out_end:.1f}-{ramp_out_start:.1f})),"
            f"      0"                                         # silence
            f"    )"
            f"  )"
            f")"
        )

        try:
            self._run([
                "ffmpeg", "-y",
                "-i", voice_path,
                "-i", music_path,
                "-filter_complex",
                (
                    f"[0:a]volume=1.0[voice];"
                    f"[1:a]volume='{volume_expr}'[music_ducked];"
                    f"[voice][music_ducked]amix=inputs=2:duration=first"
                    f":dropout_transition=2[out]"
                ),
                "-map", "[out]",
                "-t", str(duration),
                "-c:a", "aac", "-b:a", "192k",
                mixed_path,
            ], "dynamic music mix")
            logger.success(f"Dynamic music mix ✅ (hook silence + fade out)")
            return mixed_path
        except Exception as e:
            logger.warning(f"Dynamic music mix failed ({e}), trying simple flat mix")
            try:
                self._run([
                    "ffmpeg", "-y",
                    "-i", voice_path, "-i", music_path,
                    "-filter_complex",
                    (f"[0:a]volume=1.0[v];[1:a]volume={base_volume:.3f}[m];"
                     f"[v][m]amix=inputs=2:duration=first:dropout_transition=2[out]"),
                    "-map", "[out]", "-t", str(duration),
                    "-c:a", "aac", "-b:a", "192k", mixed_path,
                ], "simple music mix fallback")
                return mixed_path
            except Exception as e2:
                logger.warning(f"Simple music mix also failed: {e2}")
                return voice_path

    # ─────────────────────────────────────────────────────────────────────────
    # UPGRADE 2: SFX layer
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_sfx_layer(
        self,
        audio_path:  str,
        output_path: str,
        duration:    float,
        emotion:     str,
        num_scenes:  int,
    ) -> str:
        """
        Mix in royalty-free SFX from assets/sfx/:
          - whoosh.mp3: at each scene cut (spaced evenly)
          - rumble.mp3: looped under horror/fear content
          - riser.mp3:  4 seconds before end (builds to CTA)

        If assets/sfx/ is missing, returns None (silently skipped).
        """
        sfx_dir = Path("assets/sfx")
        if not sfx_dir.exists():
            return None

        whoosh_path = str(sfx_dir / "whoosh.mp3")
        rumble_path = str(sfx_dir / "rumble.mp3")
        riser_path  = str(sfx_dir / "riser.mp3")

        sfx_out = output_path.replace(".mp4", "_sfx_mix.mp3")

        # Build FFmpeg inputs and filter_complex dynamically
        inputs = ["-i", audio_path]
        filter_parts = ["[0:a]volume=1.0[base]"]
        mix_inputs   = ["[base]"]
        input_idx    = 1

        # ── Whoosh at each scene cut ──────────────────────────────────────────
        if os.path.exists(whoosh_path) and num_scenes > 1:
            scene_dur = duration / num_scenes
            # Add one whoosh input, then delay it to each cut point
            whoosh_delays = []
            for i in range(1, num_scenes):
                cut_ms = int(i * scene_dur * 1000)
                inputs += ["-i", whoosh_path]
                label = f"[whoosh{i}]"
                filter_parts.append(
                    f"[{input_idx}:a]volume=0.25,adelay={cut_ms}|{cut_ms}{label}"
                )
                mix_inputs.append(label)
                input_idx += 1

        # ── Rumble loop under horror ──────────────────────────────────────────
        if emotion in ("fear", "curiosity", "shock") and os.path.exists(rumble_path):
            inputs += ["-i", rumble_path]
            filter_parts.append(
                f"[{input_idx}:a]volume=0.08,aloop=loop=-1:size=2e+09,atrim=duration={duration}[rumble]"
            )
            mix_inputs.append("[rumble]")
            input_idx += 1

        # ── Riser 4 seconds before end ────────────────────────────────────────
        if os.path.exists(riser_path) and duration > 8:
            riser_start_ms = max(0, int((duration - 4.0) * 1000))
            inputs += ["-i", riser_path]
            filter_parts.append(
                f"[{input_idx}:a]volume=0.20,adelay={riser_start_ms}|{riser_start_ms}[riser]"
            )
            mix_inputs.append("[riser]")
            input_idx += 1

        # If no SFX were added, skip
        if len(mix_inputs) <= 1:
            return None

        # Final amix
        n_mix = len(mix_inputs)
        mix_labels = "".join(mix_inputs)
        filter_parts.append(
            f"{mix_labels}amix=inputs={n_mix}:duration=first:dropout_transition=1[sfxout]"
        )

        cmd = (
            ["ffmpeg", "-y"]
            + inputs
            + ["-filter_complex", ";".join(filter_parts)]
            + ["-map", "[sfxout]", "-t", str(duration)]
            + ["-c:a", "aac", "-b:a", "192k", sfx_out]
        )

        try:
            self._run(cmd, "SFX layer mix")
            logger.success(f"SFX layer ✅  ({n_mix - 1} effects added)")
            return sfx_out
        except Exception as e:
            logger.warning(f"SFX layer failed: {e} — continuing without SFX")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Subtitle filter — safe path copy (unchanged from original)
    # ─────────────────────────────────────────────────────────────────────────

    def _caption_filter(self, subtitle_path: str, output_path: str):
        if not subtitle_path or not os.path.exists(subtitle_path):
            logger.warning("Subtitle file missing — composing without captions")
            return None, None

        ext  = Path(subtitle_path).suffix.lower()
        stem = Path(output_path).stem.replace("-", "_").replace(" ", "_")
        safe_path = f"/tmp/sub_safe_{stem}{ext}"

        try:
            shutil.copy2(subtitle_path, safe_path)
        except Exception as e:
            logger.warning(f"Cannot copy subtitle to safe path: {e}")
            return None, None

        if ext == ".ass":
            vf = f"ass={safe_path}"
        else:
            vf = (
                f"subtitles={safe_path}:"
                f"force_style='FontName=Arial,FontSize=58,Bold=1,"
                f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                f"Outline=4,Shadow=2,Alignment=2,"
                f"MarginV=150,MarginL=60,MarginR=60'"
            )
        return vf, safe_path

    # ─────────────────────────────────────────────────────────────────────────
    # UPGRADE 1b: Emotion-aware Ken Burns for still images
    # ─────────────────────────────────────────────────────────────────────────

    def _compose_from_images(self, images, output_path, duration, fps, width, height,
                              kb_preset: str = "slow_zoom_in"):
        n       = len(images)
        seg_dur = max(duration / n, 2.5)
        xfade   = 0.5

        # Get preset list for this emotion
        preset_list = KB_PRESETS_BY_EMOTION.get(kb_preset, KB_PRESETS_RANDOM)

        # Cycle through presets (not random — deterministic for reproducibility)
        kb_clips = []
        for i, img in enumerate(images):
            clip_path = output_path.replace(".mp4", f"_kb_{i:02d}.mp4")
            zoom_e, x_e, y_e = preset_list[i % len(preset_list)]
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
            ], f"Ken Burns clip {i} [{kb_preset}]")
            kb_clips.append(clip_path)

        if n == 1:
            return kb_clips[0]
        return self._crossfade_clips(kb_clips, output_path, fps, xfade, seg_dur, duration)

    # ─────────────────────────────────────────────────────────────────────────
    # Video clip pipeline (unchanged)
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
    # Shared helpers (unchanged)
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
        logger.debug(f"FFmpeg [{step}]: {' '.join(cmd[:8])}…")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            logger.error(f"FFmpeg [{step}] failed:\n{r.stderr[-2000:]}")
            raise RuntimeError(f"FFmpeg [{step}] failed: {r.stderr[-600:]}")
        return r
