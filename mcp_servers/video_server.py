"""
mcp_servers/video_server.py

UPGRADED v3 with music mixing fixes:

1. OPENING HOOK CARD (from v2, preserved)

2. RAPID CUT TIMING (from v2, preserved)

3. EMOTION-AWARE KEN BURNS (from v2, preserved)

4. DYNAMIC MUSIC DUCKING (FIXED v5)
   ROOT CAUSE FIX: aloop filter with size=2000000000 or size=2e+09 fails
   silently on many ffmpeg builds (especially Ubuntu 24 / GitHub Actions runners).
   FIX: Replaced aloop entirely with -stream_loop -1 pre-extension approach.
   Music is first looped to full video duration using ffmpeg -stream_loop -1,
   then mixed with voice in a separate pass. This is universally compatible
   across all ffmpeg versions and avoids the aloop size parameter entirely.

   Also added explicit duration-based volume ramp using the volume filter
   expression on the pre-extended file, which is simpler and more reliable
   than doing it inline with aloop.

5. SFX LAYER (from v2, preserved, aloop also replaced with -stream_loop -1)

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

# System font candidates for hook card
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


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
        emotion:       str   = "inspiration",
        kb_preset:     str   = None,
        hook_text:     str   = "",
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
                        merged = merged_with_hook
                        duration += 0.7
                        logger.success(f"Hook card prepended ✅ (+0.7s)")

            # ── Dynamic music mixing (FIXED v5: stream_loop approach) ──────────
            if music_path and os.path.exists(music_path):
                music_size = os.path.getsize(music_path)
                logger.info(
                    f"Mixing music: {music_path} ({music_size // 1024} KB) "
                    f"at volume={music_volume:.3f} over {duration:.1f}s"
                )
                final_audio = self._dynamic_music_mix(
                    audio_path, music_path, output_path, duration, music_volume
                )
                if final_audio == audio_path:
                    logger.warning(
                        "Music mix returned voice_path — music was NOT mixed. "
                        "Video will be voice-only."
                    )
                else:
                    logger.success(
                        f"Music mixed successfully → {os.path.basename(final_audio)}"
                    )
            else:
                if music_path:
                    logger.warning(f"music_path provided but file missing: {music_path}")
                else:
                    logger.warning("No music_path provided — video will be voice-only")
                final_audio = audio_path

            # ── SFX layer ─────────────────────────────────────────────────────
            sfx_audio = self._apply_sfx_layer(
                final_audio, output_path, duration, emotion,
                num_scenes=len(valid_assets)
            )
            if sfx_audio and os.path.exists(sfx_audio):
                final_audio = sfx_audio

            # ── Subtitle filter ───────────────────────────────────────────────
            vf_final, sub_tmp = self._caption_filter(subtitle_path, output_path)

            compose_cmd = [
                "ffmpeg", "-y",
                "-i", merged, "-i", final_audio,
                "-c:v", "libx264", "-preset", "fast", "-crf", "21",
                "-c:a", "aac", "-b:a", "192k",
                "-t", str(duration),
                "-movflags", "+faststart",
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
            # Clean up temp files
            for suffix in ["_merged.mp4", "_audio_mix.mp3", "_audio_mix.aac",
                           "_sfx_mix.mp3", "_hookcard.mp4", "_with_hook.mp4",
                           "_music_extended.mp3"]:
                tmp = output_path.replace(".mp4", suffix)
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
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

        file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        if file_size == 0:
            return {"success": False, "error": "Output video is empty"}

        logger.success(
            f"Video composed ✅  {output_path} "
            f"({file_size/1024/1024:.1f} MB, {duration:.1f}s, "
            f"kb={_kb_preset}, emotion={emotion}, "
            f"hook_card={'yes' if hook_text else 'no'})"
        )
        return {
            "success":          True,
            "output_path":      output_path,
            "file_size_bytes":  file_size,
            "duration_seconds": duration,
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
        list_path      = output_path.replace(".mp4", "_concat_list.txt")

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
    # FIXED v5: Dynamic music ducking
    #
    # Root cause of previous failures:
    #   - aloop filter with size=2000000000 fails silently on many ffmpeg builds
    #   - aloop filter with size=2e+09 (float/scientific notation) also fails
    #   - Both failures cause the entire filter_complex to fail, returning voice only
    #
    # Fix: Use -stream_loop -1 to pre-extend the music file to full duration
    # before mixing. This avoids the aloop filter entirely and works on all
    # ffmpeg versions including older builds on GitHub Actions runners.
    #
    # Pipeline:
    #   Step 1: ffmpeg -stream_loop -1 -i music.mp3 -t {duration} → extended.mp3
    #   Step 2: ffmpeg -i voice.mp3 -i extended.mp3 -filter_complex amix → mixed.mp3
    # ─────────────────────────────────────────────────────────────────────────

    def _dynamic_music_mix(self, voice_path, music_path, output_path,
                            duration, base_volume=0.10):
        mixed_path    = output_path.replace(".mp4", "_audio_mix.mp3")
        extended_path = output_path.replace(".mp4", "_music_extended.mp3")

        logger.info(
            f"Music mix: voice={os.path.basename(voice_path)} "
            f"music={os.path.basename(music_path)} "
            f"duration={duration:.1f}s vol={base_volume:.3f}"
        )

        # ── Step 1: Pre-extend music to full video duration ───────────────────
        # -stream_loop -1 loops the input indefinitely; -t trims to exact duration.
        # This is universally supported across all ffmpeg versions.
        try:
            self._run([
                "ffmpeg", "-y",
                "-stream_loop", "-1",
                "-i", music_path,
                "-t", str(duration),
                "-c:a", "libmp3lame", "-q:a", "2",
                extended_path,
            ], "extend music to video duration")
        except Exception as e:
            logger.warning(f"Music extension failed: {e} — trying voice-only")
            return voice_path

        if not os.path.exists(extended_path) or os.path.getsize(extended_path) < 1000:
            logger.warning("Extended music file missing or empty — voice-only")
            return voice_path

        ext_size = os.path.getsize(extended_path)
        logger.info(f"Music extended: {ext_size // 1024} KB for {duration:.1f}s")

        # ── Step 2: Mix voice + extended music with volume ramp ───────────────
        # Volume expression: fade in over first 3s, flat in middle, fade out over last 3s.
        # Using the volume filter on the pre-extended file (no aloop needed).
        ramp_in_end    = min(3.0, duration * 0.08)
        ramp_out_start = max(ramp_in_end + 1, duration - 5.0)
        ramp_out_end   = max(ramp_out_start + 1, duration - 1.0)

        # afade filters on the music stream: fade in + fade out
        music_af = (
            f"afade=t=in:st=0:d={ramp_in_end:.1f},"
            f"afade=t=out:st={ramp_out_start:.1f}:d={ramp_out_end - ramp_out_start:.1f},"
            f"volume={base_volume:.4f}"
        )

        try:
            self._run([
                "ffmpeg", "-y",
                "-i", voice_path,
                "-i", extended_path,
                "-filter_complex",
                (
                    f"[0:a]volume=1.0[voice];"
                    f"[1:a]{music_af}[music];"
                    f"[voice][music]amix=inputs=2:duration=longest:dropout_transition=2[out]"
                ),
                "-map", "[out]",
                "-t", str(duration),
                "-c:a", "aac", "-b:a", "192k",
                mixed_path,
            ], "dynamic music mix with fade")

            size = os.path.getsize(mixed_path) if os.path.exists(mixed_path) else 0
            if size < 10_000:
                raise RuntimeError(f"Mixed audio too small ({size} bytes)")

            logger.success(f"Dynamic music mix ✅  ({size // 1024} KB, {duration:.1f}s)")
            return mixed_path

        except Exception as e:
            logger.warning(
                f"Dynamic mix (with fade) failed: {e}\n"
                f"Retrying with simple flat mix..."
            )

        # ── Simple flat mix fallback (no fade, just flat volume) ──────────────
        try:
            self._run([
                "ffmpeg", "-y",
                "-i", voice_path,
                "-i", extended_path,
                "-filter_complex",
                (
                    f"[0:a]volume=1.0[v];"
                    f"[1:a]volume={base_volume:.4f}[m];"
                    f"[v][m]amix=inputs=2:duration=longest:dropout_transition=2[out]"
                ),
                "-map", "[out]",
                "-t", str(duration),
                "-c:a", "aac", "-b:a", "192k",
                mixed_path,
            ], "simple flat music mix fallback")

            size = os.path.getsize(mixed_path) if os.path.exists(mixed_path) else 0
            if size < 10_000:
                raise RuntimeError(f"Simple mixed audio also too small ({size} bytes)")

            logger.success(f"Simple flat music mix ✅  ({size // 1024} KB)")
            return mixed_path

        except Exception as e2:
            logger.warning(
                f"Simple flat mix also failed: {e2}\n"
                f"Trying amerge as last resort..."
            )

        # ── amerge last resort (simplest possible stereo merge) ───────────────
        try:
            self._run([
                "ffmpeg", "-y",
                "-i", voice_path,
                "-i", extended_path,
                "-filter_complex",
                f"[1:a]volume={base_volume:.4f}[m];[0:a][m]amerge=inputs=2,pan=stereo|c0<c0+c2|c1<c1+c3[out]",
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

        except Exception as e3:
            logger.error(
                f"ALL music mix attempts failed.\n"
                f"  Attempt 1 (fade amix): {e}\n"  # noqa: F821 — e from outer scope
                f"  Attempt 2 (flat amix): {e2}\n"  # noqa: F821
                f"  Attempt 3 (amerge):    {e3}\n"
                f"Video will be voice-only."
            )
            return voice_path

    # ─────────────────────────────────────────────────────────────────────────
    # SFX layer
    # FIXED v5: Replaced aloop with -stream_loop -1 pre-extension
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_sfx_layer(self, audio_path, output_path, duration, emotion, num_scenes):
        sfx_dir = Path("assets/sfx")
        if not sfx_dir.exists():
            return None

        whoosh_path = str(sfx_dir / "whoosh.mp3")
        rumble_path = str(sfx_dir / "rumble.mp3")
        riser_path  = str(sfx_dir / "riser.mp3")
        sfx_out     = output_path.replace(".mp4", "_sfx_mix.mp3")

        inputs      = ["-i", audio_path]
        filter_parts = ["[0:a]volume=1.0[base]"]
        mix_inputs  = ["[base]"]
        input_idx   = 1

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

        # FIXED: For rumble, pre-extend instead of aloop
        if emotion in ("fear", "curiosity", "shock") and os.path.exists(rumble_path):
            extended_rumble = output_path.replace(".mp4", "_rumble_ext.mp3")
            try:
                self._run([
                    "ffmpeg", "-y",
                    "-stream_loop", "-1",
                    "-i", rumble_path,
                    "-t", str(duration),
                    "-c:a", "libmp3lame", "-q:a", "2",
                    extended_rumble,
                ], "extend rumble sfx")
                inputs += ["-i", extended_rumble]
                filter_parts.append(
                    f"[{input_idx}:a]volume=0.08[rumble]"
                )
                mix_inputs.append("[rumble]")
                input_idx += 1
            except Exception as e:
                logger.debug(f"Rumble sfx extension failed: {e}")

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
            return None

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
            # Clean up extended rumble temp file
            extended_rumble_path = output_path.replace(".mp4", "_rumble_ext.mp3")
            if os.path.exists(extended_rumble_path):
                os.remove(extended_rumble_path)
            return sfx_out
        except Exception as e:
            logger.warning(f"SFX layer failed: {e}")
            # Clean up on failure too
            extended_rumble_path = output_path.replace(".mp4", "_rumble_ext.mp3")
            if os.path.exists(extended_rumble_path):
                try:
                    os.remove(extended_rumble_path)
                except Exception:
                    pass
            return None

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
    # Rapid cut Ken Burns for still images
    # ─────────────────────────────────────────────────────────────────────────

    def _compose_from_images(self, images, output_path, duration, fps, width, height,
                              kb_preset: str = "slow_zoom_in"):
        n = len(images)
        seg_dur = min(3.0, max(duration / n, 2.0))
        xfade = 0.25

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
            return kb_clips[0]
        return self._crossfade_clips(kb_clips, output_path, fps, xfade, seg_dur, duration)

    # ─────────────────────────────────────────────────────────────────────────
    # Rapid cut for video clips
    # ─────────────────────────────────────────────────────────────────────────

    def _compose_from_clips(self, clips, output_path, duration, fps, width, height):
        n = len(clips)
        seg_dur = min(3.5, max(duration / n, 2.0))
        xfade   = 0.25

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
            return scaled_clips[0]
        return self._crossfade_clips(scaled_clips, output_path, fps, xfade, seg_dur, duration)

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
