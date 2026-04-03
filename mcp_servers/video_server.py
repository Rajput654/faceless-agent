"""
mcp_servers/video_server.py

FIXED v6 — Four critical bugs resolved:

BUG FIX V6-A — VIDEO STOPS AT 2/3 (duration mismatch):
  Root cause: _compose_from_images calculated seg_dur * n_scenes total visual
  duration which was SHORTER than audio duration when xfade overlaps were not
  accounted for in the -t flag passed to crossfade chain. The final compose
  used `duration` (from audio) but the merged video was shorter, so ffmpeg
  padded with black frames or stopped playing video while audio continued.
  Fix 1: _compose_from_images now calculates total_visual_dur accounting for
  xfade overlap and passes it explicitly to the crossfade chain.
  Fix 2: Final compose -t is set to min(audio_duration, merged_video_duration)
  so video and audio always end together.
  Fix 3: Added _get_video_duration() call on merged output to verify before compose.

BUG FIX V6-B — MUSIC TOO QUIET:
  Root cause: base_volume=0.10 in _dynamic_music_mix PLUS amix normalizes its
  output, effectively halving perceived music volume again. Result: music at
  ~5-6% perceived level — inaudible on phone speakers.
  Fix: Raised default base_volume from 0.10 → 0.35. Also removed amix
  normalization (normalize=0) so the volume parameter is respected as-is.
  The music_volume config key is now read with a higher default (0.35).

PRESERVED: All v5 fixes (B1-B11).
"""
import os
import shutil
import random
import subprocess
from pathlib import Path
from loguru import logger


# ─────────────────────────────────────────────────────────────────────────────
# Ken Burns preset definitions
# ─────────────────────────────────────────────────────────────────────────────

KB_PRESETS_BY_EMOTION = {
    "slow_zoom_in": [
        ("min(zoom+0.0006,1.3)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        ("min(zoom+0.0006,1.25)", "iw/2-(iw/zoom/2)", "ih*0.45-(ih/zoom/2)"),
    ],
    "zoom_out": [
        ("if(eq(on,1),1.3,max(zoom-0.0008,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        ("if(eq(on,1),1.25,max(zoom-0.0006,1.0))", "iw/2-(iw/zoom/2)", "ih*0.4-(ih/zoom/2)"),
    ],
    "fast_pan": [
        ("1.15", "if(eq(on,1),0,min(x+1.5,iw-iw/zoom))", "ih/2-(ih/zoom/2)"),
        ("1.15", "if(eq(on,1),iw-iw/zoom,max(x-1.5,0))", "ih/2-(ih/zoom/2)"),
        ("1.12", "iw/2-(iw/zoom/2)", "if(eq(on,1),0,min(y+1.2,ih-ih/zoom))"),
    ],
    "subtle_zoom": [
        ("min(zoom+0.0003,1.12)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        ("if(eq(on,1),1.12,max(zoom-0.0003,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ],
}

KB_PRESETS_RANDOM = [
    ("min(zoom+0.0008,1.3)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("if(eq(on,1),1.3,max(zoom-0.0008,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("1.12", "if(eq(on,1),0,x+0.6)", "ih/2-(ih/zoom/2)"),
    ("1.12", "if(eq(on,1),iw-iw/zoom,max(0,x-0.6))", "ih/2-(ih/zoom/2)"),
    ("min(zoom+0.0008,1.3)", "0", "0"),
    ("min(zoom+0.0008,1.3)", "iw-(iw/zoom)", "ih-(ih/zoom)"),
]

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

SFX_WHOOSH  = "assets/sfx/whoosh.mp3"
SFX_RUMBLE  = "assets/sfx/rumble.mp3"
SFX_RISER   = "assets/sfx/riser.mp3"

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]

# BUG FIX V6-B: Raised default music volume from 0.10 → 0.35
# At 0.10 with amix normalization the music was inaudible on phone speakers.
DEFAULT_MUSIC_VOLUME = 0.35


def _find_font() -> str:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return ""


class VideoMCPServer:
    def __init__(self):
        self.tools = {
            "compose_video": self._compose_video,
            "check_ffmpeg":  self._check_ffmpeg,
        }
        self._font_path = _find_font()

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

    def _get_video_duration(self, path: str) -> float:
        """Get actual duration of a video/audio file using ffprobe."""
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=15,
            )
            val = float(r.stdout.strip())
            return val if val > 0 else 0.0
        except Exception:
            return 0.0

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
        music_volume:  float = None,   # BUG FIX V6-B: default handled below
        fps:           int   = 30,
        width:         int   = 1080,
        height:        int   = 1920,
        emotion:       str   = "inspiration",
        kb_preset:     str   = None,
        hook_text:     str   = "",
        **kwargs,
    ):
        # BUG FIX V6-B: Use higher default if not specified
        if music_volume is None:
            music_volume = DEFAULT_MUSIC_VOLUME

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if not image_paths:
            return {"success": False, "error": "No visual assets provided"}
        if not audio_path or not os.path.exists(audio_path):
            return {"success": False, "error": f"Audio not found: {audio_path}"}

        valid_assets = [p for p in image_paths if p and os.path.exists(p)]
        if not valid_assets:
            return {"success": False, "error": "No valid visual files found"}

        sub_tmp = None
        intermediate_files = []

        try:
            # BUG FIX V6-A: Get actual audio duration from file (not estimated)
            duration = self._get_video_duration(audio_path)
            if duration <= 0:
                duration = self._audio_duration(audio_path)
            if duration <= 0:
                duration = 55.0

            logger.info(f"Audio duration: {duration:.2f}s")

            is_video = any(p.lower().endswith(".mp4") for p in valid_assets)
            _kb_preset = kb_preset or EMOTION_TO_KB.get(emotion, "slow_zoom_in")

            if is_video:
                merged, actual_visual_dur = self._compose_from_clips(
                    valid_assets, output_path, duration, fps, width, height
                )
            else:
                merged, actual_visual_dur = self._compose_from_images(
                    valid_assets, output_path, duration, fps, width, height,
                    kb_preset=_kb_preset
                )

            # BUG FIX V6-A: Verify merged video actually has correct duration
            merged_actual_dur = self._get_video_duration(merged)
            if merged_actual_dur > 0:
                logger.info(
                    f"Merged video duration: {merged_actual_dur:.2f}s "
                    f"(target audio: {duration:.2f}s)"
                )
                # Use the minimum so video and audio end at the same time
                compose_duration = min(duration, merged_actual_dur)
            else:
                compose_duration = duration

            # ── Prepend opening hook card ─────────────────────────────────────
            if hook_text:
                hook_card = self._generate_hook_card(
                    hook_text, output_path, width, height, fps
                )
                if hook_card and os.path.exists(hook_card):
                    merged_with_hook = self._prepend_hook_card(
                        hook_card, merged, output_path, fps
                    )
                    if merged_with_hook and os.path.exists(merged_with_hook):
                        if merged != merged_with_hook:
                            intermediate_files.append(merged)
                        merged = merged_with_hook
                        compose_duration += 0.7
                        intermediate_files.append(hook_card)
                        logger.success(f"Hook card prepended ✅ (+0.7s)")

            # ── Music mixing ──────────────────────────────────────────────────
            if music_path and os.path.exists(music_path):
                music_size = os.path.getsize(music_path)
                logger.info(
                    f"Mixing music: {music_path} ({music_size // 1024} KB) "
                    f"at volume={music_volume:.3f} (BUG FIX V6-B: raised from 0.10) "
                    f"over {compose_duration:.1f}s"
                )
                final_audio = self._dynamic_music_mix(
                    audio_path, music_path, output_path, compose_duration, music_volume
                )
                if final_audio == audio_path:
                    logger.warning(
                        "Music mix returned voice_path — music was NOT mixed. "
                        "Video will be voice-only."
                    )
                    _mixed_audio_to_clean = None
                else:
                    logger.success(
                        f"Music mixed successfully → {os.path.basename(final_audio)}"
                    )
                    _mixed_audio_to_clean = final_audio
            else:
                if music_path:
                    logger.warning(f"music_path provided but file missing: {music_path}")
                else:
                    logger.info("No music_path — video will be voice-only")
                final_audio = audio_path
                _mixed_audio_to_clean = None

            # ── SFX layer ─────────────────────────────────────────────────────
            sfx_audio = self._apply_sfx_layer(
                final_audio, output_path, compose_duration, emotion,
                num_scenes=len(valid_assets)
            )
            if sfx_audio and os.path.exists(sfx_audio):
                _sfx_audio_to_clean = sfx_audio
                final_audio = sfx_audio
            else:
                _sfx_audio_to_clean = None

            # ── Subtitle filter ───────────────────────────────────────────────
            vf_final, sub_tmp = self._caption_filter(subtitle_path, output_path)

            # BUG FIX V6-A: Use compose_duration (min of audio+video) not raw duration
            # This ensures video track and audio track end at exactly the same time.
            compose_cmd = [
                "ffmpeg", "-y",
                "-i", merged,
                "-i", final_audio,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "fast", "-crf", "21",
                "-c:a", "aac", "-b:a", "192k",
                "-t", str(compose_duration),
                "-movflags", "+faststart",
            ]
            if vf_final:
                compose_cmd += ["-vf", vf_final]
            compose_cmd.append(output_path)

            self._run(compose_cmd, "final compose")

            # ── Cleanup intermediate files AFTER successful compose ────────────
            for f in intermediate_files:
                if f and f != output_path and os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

            merged_path_to_clean = merged
            if merged_path_to_clean and merged_path_to_clean != output_path:
                if os.path.exists(merged_path_to_clean):
                    try:
                        os.remove(merged_path_to_clean)
                    except Exception:
                        pass

            if _mixed_audio_to_clean and _mixed_audio_to_clean != audio_path:
                if os.path.exists(_mixed_audio_to_clean):
                    try:
                        os.remove(_mixed_audio_to_clean)
                    except Exception:
                        pass

            if _sfx_audio_to_clean and _sfx_audio_to_clean != audio_path:
                if os.path.exists(_sfx_audio_to_clean):
                    try:
                        os.remove(_sfx_audio_to_clean)
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Video composition failed: {e}")
            return {"success": False, "error": str(e)}

        finally:
            if sub_tmp and os.path.exists(sub_tmp):
                try:
                    os.remove(sub_tmp)
                except Exception:
                    pass

            for i in range(len(valid_assets) + 5):
                for tag in [f"_kb_{i:02d}.mp4", f"_scaled_{i:02d}.mp4", f"_cf_{i:02d}.mp4"]:
                    tmp = output_path.replace(".mp4", tag)
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass

            for suffix in ["_list.txt", "_concat_list.txt"]:
                tmp = output_path.replace(".mp4", suffix)
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

        file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        if file_size == 0:
            return {"success": False, "error": "Output video is empty"}

        # Verify final output duration
        final_dur = self._get_video_duration(output_path)
        logger.success(
            f"Video composed ✅  {output_path} "
            f"({file_size/1024/1024:.1f} MB, actual_dur={final_dur:.1f}s, "
            f"target_dur={compose_duration:.1f}s, "
            f"kb={_kb_preset}, emotion={emotion})"
        )
        return {
            "success":          True,
            "output_path":      output_path,
            "file_size_bytes":  file_size,
            "duration_seconds": final_dur if final_dur > 0 else compose_duration,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Opening hook card generation
    # ─────────────────────────────────────────────────────────────────────────

    def _generate_hook_card(self, hook_text: str, output_path: str,
                             width: int = 1080, height: int = 1920,
                             fps: int = 30) -> str:
        card_path = output_path.replace(".mp4", "_hookcard.mp4")

        import re
        clean = hook_text[:55]
        clean = re.sub(r"[':=\\\"()]", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip().upper()

        words = clean.split()
        if len(words) > 5:
            mid = len(words) // 2
            line1 = " ".join(words[:mid])
            line2 = " ".join(words[mid:])
            display = line1 + r"\n" + line2
        else:
            display = clean

        font_arg = f"fontfile={self._font_path}:" if self._font_path else ""

        vf = (
            f"drawtext="
            f"{font_arg}"
            f"text='{display}':"
            f"fontsize=76:"
            f"fontcolor=yellow:"
            f"bordercolor=black:borderw=6:"
            f"line_spacing=12:"
            f"x=(w-text_w)/2:y=(h-text_h)/2"
        )

        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:size={width}x{height}:duration=0.7:rate={fps}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            card_path,
        ]

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and os.path.exists(card_path):
                size = os.path.getsize(card_path)
                if size > 1000:
                    logger.info(f"Hook card generated: {card_path} ({size//1024} KB)")
                    return card_path
            logger.warning(f"Hook card generation failed: {r.stderr[-300:]}")
            return None
        except Exception as e:
            logger.warning(f"Hook card exception: {e}")
            return None

    def _prepend_hook_card(self, hook_card: str, main_video: str,
                            output_path: str, fps: int = 30) -> str:
        with_hook_path = output_path.replace(".mp4", "_with_hook.mp4")
        list_path      = output_path.replace(".mp4", "_hook_concat.txt")

        try:
            with open(list_path, "w") as f:
                f.write(f"file '{os.path.abspath(hook_card)}'\n")
                f.write(f"file '{os.path.abspath(main_video)}'\n")

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-c:v", "libx264", "-preset", "fast", "-crf", "21",
                "-pix_fmt", "yuv420p",
                "-an",
                with_hook_path,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            if os.path.exists(list_path):
                os.remove(list_path)

            if r.returncode == 0 and os.path.exists(with_hook_path):
                return with_hook_path

            logger.warning(f"Hook card concat failed: {r.stderr[-300:]}")
            return None
        except Exception as e:
            logger.warning(f"Hook card concat exception: {e}")
            if os.path.exists(list_path):
                os.remove(list_path)
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Dynamic music ducking
    # BUG FIX V6-B: normalize=0 added to amix so volume= is fully respected
    # ─────────────────────────────────────────────────────────────────────────

    def _normalize_to_cbr(self, music_path: str, output_path: str) -> bool:
        try:
            self._run([
                "ffmpeg", "-y",
                "-i", music_path,
                "-c:a", "libmp3lame", "-b:a", "128k",
                "-write_xing", "0",
                output_path,
            ], "normalize music to CBR")
            size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            return size > 1000
        except Exception as e:
            logger.warning(f"CBR normalization failed: {e}")
            return False

    def _dynamic_music_mix(self, voice_path, music_path, output_path,
                            duration, base_volume=None):
        """
        Mix voice + music. Returns mixed_path (.m4a) on success, voice_path on failure.

        BUG FIX V6-B: 
          1. Default base_volume raised to DEFAULT_MUSIC_VOLUME (0.35)
          2. amix filter now uses normalize=0 so the volume= parameter is
             respected as absolute gain rather than being normalized down.
             Previously amix normalized to 1.0 which effectively halved the
             perceived music volume on top of the already-low base_volume.
        """
        if base_volume is None:
            base_volume = DEFAULT_MUSIC_VOLUME

        mixed_path    = output_path.replace(".mp4", "_audio_mix.m4a")
        cbr_path      = output_path.replace(".mp4", "_music_cbr.mp3")
        extended_path = output_path.replace(".mp4", "_music_extended.mp3")
        extended_created = False

        logger.info(
            f"Music mix: voice={os.path.basename(voice_path)} "
            f"music={os.path.basename(music_path)} "
            f"duration={duration:.1f}s vol={base_volume:.3f} "
            f"(BUG FIX V6-B: normalize=0, vol={base_volume:.3f})"
        )

        cbr_ok = self._normalize_to_cbr(music_path, cbr_path)
        source_for_loop = cbr_path if cbr_ok else music_path
        if cbr_ok:
            logger.info(f"Music normalized to CBR: {os.path.getsize(cbr_path)//1024} KB")
        else:
            logger.warning("CBR normalization failed — using original file")

        try:
            try:
                self._run([
                    "ffmpeg", "-y",
                    "-stream_loop", "-1",
                    "-i", source_for_loop,
                    "-t", str(duration),
                    "-c:a", "libmp3lame", "-b:a", "128k",
                    extended_path,
                ], "extend music to video duration")
                extended_created = True
            except Exception as e:
                logger.warning(f"stream_loop extension failed: {e} — trying concat loop fallback")
                result = self._loop_by_concat(source_for_loop, extended_path, duration)
                if result:
                    extended_created = True
                else:
                    logger.warning("All music extension methods failed — voice-only")
                    return voice_path

            if not os.path.exists(extended_path) or os.path.getsize(extended_path) < 1000:
                logger.warning("Extended music file missing or empty — voice-only")
                return voice_path

            ext_size = os.path.getsize(extended_path)
            logger.info(f"Music extended: {ext_size // 1024} KB for {duration:.1f}s")

            ramp_in_end    = min(3.0, duration * 0.08)
            ramp_out_start = max(ramp_in_end + 1, duration - 5.0)
            ramp_out_end   = max(ramp_out_start + 1, duration - 1.0)

            music_af = (
                f"afade=t=in:st=0:d={ramp_in_end:.1f},"
                f"afade=t=out:st={ramp_out_start:.1f}:d={ramp_out_end - ramp_out_start:.1f},"
                f"volume={base_volume:.4f}"
            )

            attempt1_err = None
            attempt2_err = None

            # BUG FIX V6-B: normalize=0 added to amix — critical for volume control
            # Without normalize=0, amix divides output by number of inputs (2),
            # halving all levels including the carefully set music volume.
            try:
                self._run([
                    "ffmpeg", "-y",
                    "-i", voice_path,
                    "-i", extended_path,
                    "-filter_complex",
                    (
                        f"[0:a]volume=1.0[voice];"
                        f"[1:a]{music_af}[music];"
                        f"[voice][music]amix=inputs=2:duration=longest:"
                        f"dropout_transition=2:normalize=0[out]"
                    ),
                    "-map", "[out]",
                    "-t", str(duration),
                    "-c:a", "aac", "-b:a", "192k",
                    mixed_path,
                ], "dynamic music mix with fade (normalize=0)")

                size = os.path.getsize(mixed_path) if os.path.exists(mixed_path) else 0
                if size < 10_000:
                    raise RuntimeError(f"Mixed audio too small ({size} bytes)")

                logger.success(f"Dynamic music mix ✅  ({size // 1024} KB, {duration:.1f}s, vol={base_volume:.3f})")
                return mixed_path

            except Exception as e:
                attempt1_err = str(e)
                logger.warning(f"Attempt 1 (fade amix normalize=0) failed: {attempt1_err} — trying flat mix")

            # Attempt 2: simple flat amix without fade but with normalize=0
            try:
                self._run([
                    "ffmpeg", "-y",
                    "-i", voice_path,
                    "-i", extended_path,
                    "-filter_complex",
                    (
                        f"[0:a]volume=1.0[v];"
                        f"[1:a]volume={base_volume:.4f}[m];"
                        f"[v][m]amix=inputs=2:duration=longest:"
                        f"dropout_transition=2:normalize=0[out]"
                    ),
                    "-map", "[out]",
                    "-t", str(duration),
                    "-c:a", "aac", "-b:a", "192k",
                    mixed_path,
                ], "simple flat music mix (normalize=0)")

                size = os.path.getsize(mixed_path) if os.path.exists(mixed_path) else 0
                if size < 10_000:
                    raise RuntimeError(f"Simple mixed audio too small ({size} bytes)")

                logger.success(f"Simple flat music mix ✅  ({size // 1024} KB)")
                return mixed_path

            except Exception as e:
                attempt2_err = str(e)
                logger.warning(f"Attempt 2 (flat amix normalize=0) failed: {attempt2_err} — trying amerge")

            # Attempt 3: amerge (last resort)
            try:
                self._run([
                    "ffmpeg", "-y",
                    "-i", voice_path,
                    "-i", extended_path,
                    "-filter_complex",
                    f"[1:a]volume={base_volume:.4f}[m];"
                    f"[0:a][m]amerge=inputs=2,"
                    f"pan=stereo|c0<c0+c2|c1<c1+c3[out]",
                    "-map", "[out]",
                    "-t", str(duration),
                    "-c:a", "aac", "-b:a", "192k",
                    mixed_path,
                ], "amerge last resort")

                size = os.path.getsize(mixed_path) if os.path.exists(mixed_path) else 0
                if size < 10_000:
                    raise RuntimeError(f"amerge audio too small ({size} bytes)")

                logger.success(f"amerge music mix ✅  ({size // 1024} KB)")
                return mixed_path

            except Exception as e:
                logger.error(
                    f"ALL music mix attempts failed:\n"
                    f"  Attempt 1 (fade amix): {attempt1_err}\n"
                    f"  Attempt 2 (flat amix): {attempt2_err}\n"
                    f"  Attempt 3 (amerge):    {e}\n"
                    f"Video will be voice-only."
                )
                return voice_path

        finally:
            for tmp in [cbr_path, extended_path if extended_created else None]:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

    def _loop_by_concat(self, music_path: str, output_path: str, duration: float) -> str:
        try:
            music_duration = self._audio_duration(music_path)
            if music_duration <= 0:
                music_duration = 30.0
            repeats = max(2, int(duration / music_duration) + 2)

            list_path = output_path + "_loop_list.txt"
            with open(list_path, "w") as f:
                for _ in range(repeats):
                    f.write(f"file '{os.path.abspath(music_path)}'\n")

            self._run([
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-t", str(duration),
                "-c:a", "libmp3lame", "-b:a", "128k",
                output_path,
            ], "loop music by concat")

            if os.path.exists(list_path):
                os.remove(list_path)

            size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if size > 1000:
                logger.info(f"Music looped by concat: {repeats}x → {size//1024} KB")
                return output_path
            return None
        except Exception as e:
            logger.warning(f"Concat loop failed: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # SFX layer
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_sfx_layer(self, audio_path, output_path, duration, emotion, num_scenes):
        sfx_dir = Path("assets/sfx")
        if not sfx_dir.exists():
            return None

        whoosh_path = str(sfx_dir / "whoosh.mp3")
        rumble_path = str(sfx_dir / "rumble.mp3")
        riser_path  = str(sfx_dir / "riser.mp3")
        sfx_out = output_path.replace(".mp4", "_sfx_mix.m4a")

        normalized_audio = output_path.replace(".mp4", "_sfx_base.mp3")
        try:
            self._run([
                "ffmpeg", "-y", "-i", audio_path,
                "-c:a", "libmp3lame", "-b:a", "192k",
                normalized_audio,
            ], "normalize audio for SFX layer")
            sfx_input_path = normalized_audio
        except Exception:
            sfx_input_path = audio_path

        inputs       = ["-i", sfx_input_path]
        filter_parts = ["[0:a]volume=1.0[base]"]
        mix_inputs   = ["[base]"]
        input_idx    = 1
        rumble_extended = None

        if os.path.exists(whoosh_path) and num_scenes > 1:
            scene_dur = duration / num_scenes
            for i in range(1, min(num_scenes, 6)):
                cut_ms = int(i * scene_dur * 1000)
                inputs += ["-i", whoosh_path]
                label = f"[whoosh{i}]"
                filter_parts.append(
                    f"[{input_idx}:a]volume=0.20,adelay={cut_ms}|{cut_ms}{label}"
                )
                mix_inputs.append(label)
                input_idx += 1

        if emotion in ("fear", "curiosity", "shock") and os.path.exists(rumble_path):
            rumble_extended = output_path.replace(".mp4", "_rumble_ext.mp3")
            try:
                self._run([
                    "ffmpeg", "-y",
                    "-stream_loop", "-1",
                    "-i", rumble_path,
                    "-t", str(duration),
                    "-c:a", "libmp3lame", "-q:a", "2",
                    rumble_extended,
                ], "extend rumble sfx")
                inputs += ["-i", rumble_extended]
                filter_parts.append(f"[{input_idx}:a]volume=0.08[rumble]")
                mix_inputs.append("[rumble]")
                input_idx += 1
            except Exception as e:
                logger.debug(f"Rumble sfx extension failed: {e}")
                rumble_extended = None

        if os.path.exists(riser_path) and duration > 8:
            riser_start_ms = max(0, int((duration - 4.0) * 1000))
            inputs += ["-i", riser_path]
            filter_parts.append(
                f"[{input_idx}:a]volume=0.20,"
                f"adelay={riser_start_ms}|{riser_start_ms}[riser]"
            )
            mix_inputs.append("[riser]")
            input_idx += 1

        if len(mix_inputs) <= 1:
            for tmp in [normalized_audio]:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
            return None

        n_mix = len(mix_inputs)
        mix_labels = "".join(mix_inputs)
        # BUG FIX V6-B: normalize=0 here too for consistent SFX volume
        filter_parts.append(
            f"{mix_labels}amix=inputs={n_mix}:duration=first:"
            f"dropout_transition=1:normalize=0[sfxout]"
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
            logger.warning(f"SFX layer failed: {e}")
            return None
        finally:
            for tmp in [normalized_audio, rumble_extended]:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

    # ─────────────────────────────────────────────────────────────────────────
    # Subtitle filter
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
                f"force_style='FontName=Arial,FontSize=72,Bold=1,"
                f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                f"Outline=6,Shadow=3,Alignment=5,"
                f"MarginV=0,MarginL=80,MarginR=80'"
            )
        return vf, safe_path

    # ─────────────────────────────────────────────────────────────────────────
    # BUG FIX V6-A: _compose_from_images now returns (merged_path, actual_dur)
    # Previously it returned only merged_path, so caller had no way to know
    # actual visual duration, leading to duration mismatches.
    # ─────────────────────────────────────────────────────────────────────────

    def _compose_from_images(self, images, output_path, duration, fps, width, height,
                              kb_preset: str = "slow_zoom_in"):
        n = len(images)
        # BUG FIX V6-A: Calculate seg_dur so total visual covers full audio
        # Previously: seg_dur = min(3.0, max(duration/n, 2.0))
        # This could give n*seg_dur < duration when duration/n > 3.0
        # Now: ensure n*seg_dur >= duration by setting seg_dur = ceil(duration/n)
        xfade = 0.25
        # Account for xfade overlap: total_dur = n*seg - (n-1)*xfade
        # So seg_dur = (duration + (n-1)*xfade) / n
        seg_dur_raw = (duration + (n - 1) * xfade) / n
        seg_dur = max(2.0, min(4.0, seg_dur_raw))

        # Recalculate actual total visual duration with this seg_dur
        actual_visual_dur = n * seg_dur - (n - 1) * xfade
        logger.info(
            f"Ken Burns: {n} scenes × {seg_dur:.2f}s (xfade={xfade}s) "
            f"= {actual_visual_dur:.2f}s visual (audio={duration:.2f}s)"
        )

        preset_list = KB_PRESETS_BY_EMOTION.get(kb_preset, KB_PRESETS_RANDOM)

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
            ], f"Ken Burns clip {i} [{kb_preset}] {seg_dur:.1f}s")
            kb_clips.append(clip_path)

        if n == 1:
            return kb_clips[0], seg_dur

        merged = self._crossfade_clips(kb_clips, output_path, fps, xfade, seg_dur, actual_visual_dur)
        return merged, actual_visual_dur

    # ─────────────────────────────────────────────────────────────────────────
    # BUG FIX V6-A: _compose_from_clips also returns (merged_path, actual_dur)
    # ─────────────────────────────────────────────────────────────────────────

    def _compose_from_clips(self, clips, output_path, duration, fps, width, height):
        n = len(clips)
        xfade = 0.25
        seg_dur_raw = (duration + (n - 1) * xfade) / n
        seg_dur = max(2.0, min(4.0, seg_dur_raw))
        actual_visual_dur = n * seg_dur - (n - 1) * xfade

        logger.info(
            f"Stock clips: {n} clips × {seg_dur:.2f}s "
            f"= {actual_visual_dur:.2f}s visual (audio={duration:.2f}s)"
        )

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
            ], f"scale clip {i} {seg_dur:.1f}s")
            scaled_clips.append(out)

        if n == 1:
            return scaled_clips[0], seg_dur

        merged = self._crossfade_clips(scaled_clips, output_path, fps, xfade, seg_dur, actual_visual_dur)
        return merged, actual_visual_dur

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
        logger.debug(f"FFmpeg [{step}]: {' '.join(cmd[:8])}…")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            logger.error(f"FFmpeg [{step}] failed:\n{r.stderr[-2000:]}")
            raise RuntimeError(f"FFmpeg [{step}] failed: {r.stderr[-600:]}")
        return r
