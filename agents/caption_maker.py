"""
agents/caption_maker.py

UPGRADED v2: Center-screen word-by-word highlight captions — viral Shorts style.

Key changes from v1:
  - Captions repositioned to CENTER-SCREEN (Alignment=5) from bottom (Alignment=2)
  - Font size increased from 54px → 72px for mobile readability
  - Words per group reduced from 5 → 4 (snappier, more readable on small screens)
  - Added UPPERCASE enforcement at the ASS level
  - Stroke weight increased from 4px → 6px for better contrast on varied backgrounds
  - Shadow upgraded to a stronger drop shadow

FIX: _rechunk_entries — division by zero when total_audio_ms == 0
  (e.g. ffprobe fails on voice file). Guard added with max(total_audio_ms, 1000).
"""
import os
import re
from pathlib import Path
from loguru import logger


# ---------------------------------------------------------------------------
# Colour constants (ASS format: &HAABBGGRR — alpha, blue, green, red)
# ---------------------------------------------------------------------------
COLOUR_WHITE      = "&H00FFFFFF"   # normal word
COLOUR_YELLOW     = "&H0000FFFF"   # active/highlighted word (yellow in BGR)
COLOUR_OUTLINE    = "&H00000000"   # black outline
COLOUR_SHADOW     = "&H80000000"   # semi-transparent black shadow

# Words per caption group — 4 is the viral sweet spot
WORDS_PER_GROUP = 4


class CaptionMakerAgent:
    def __init__(self, config):
        self.config = config

    # ------------------------------------------------------------------
    # SRT parser — returns list of {index, start_ms, end_ms, text}
    # ------------------------------------------------------------------
    def _parse_srt(self, srt_path: str) -> list:
        if not os.path.exists(srt_path):
            return []
        with open(srt_path, "r", encoding="utf-8") as f:
            content = f.read()

        entries = []
        blocks = re.split(r"\n\n+", content.strip())
        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) >= 3:
                try:
                    idx = int(lines[0].strip())
                    times = lines[1].strip()
                    text = " ".join(lines[2:]).strip()
                    text = re.sub(r"<[^>]+>", "", text)  # strip HTML tags

                    if "-->" not in times:
                        continue
                    start_str, end_str = times.split("-->")
                    start_ms = self._srt_time_to_ms(start_str.strip())
                    end_ms   = self._srt_time_to_ms(end_str.strip())

                    entries.append({
                        "index":    idx,
                        "start_ms": start_ms,
                        "end_ms":   end_ms,
                        "text":     text,
                    })
                except (ValueError, IndexError):
                    continue
        return entries

    @staticmethod
    def _srt_time_to_ms(t: str) -> int:
        t = t.strip().replace(",", ".")
        parts = t.split(":")
        h = int(parts[0])
        m = int(parts[1])
        s_ms = parts[2].split(".")
        s  = int(s_ms[0])
        ms = int(s_ms[1][:3]) if len(s_ms) > 1 else 0
        return h * 3_600_000 + m * 60_000 + s * 1_000 + ms

    @staticmethod
    def _ms_to_ass(ms: int) -> str:
        h    = ms // 3_600_000
        m    = (ms % 3_600_000) // 60_000
        s    = (ms % 60_000) // 1_000
        cs   = (ms % 1_000) // 10
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    # ------------------------------------------------------------------
    # ASS header — center-screen, 72px, heavier stroke
    # ------------------------------------------------------------------
    def _create_ass_header(self) -> str:
        return (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "PlayResX: 1080\n"
            "PlayResY: 1920\n"
            "ScaledBorderAndShadow: yes\n"
            "WrapStyle: 1\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,Arial,72,{COLOUR_WHITE},{COLOUR_YELLOW},"
            f"{COLOUR_OUTLINE},{COLOUR_SHADOW},"
            "-1,0,0,0,"
            "100,100,2,0,"
            "1,6,3,"
            "5,80,80,0,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

    # ------------------------------------------------------------------
    # CORE: Word-by-word karaoke highlight builder
    # ------------------------------------------------------------------
    def _build_karaoke_events(self, entries: list) -> str:
        events = []
        for entry in entries:
            start_ms = entry["start_ms"]
            end_ms   = entry["end_ms"]
            total_ms = max(end_ms - start_ms, 100)
            text     = entry["text"].strip()

            text = re.sub(r"<[^>]+>", "", text)
            text = text.upper()

            words = text.split()
            if not words:
                continue

            n = len(words)
            ms_per_word = total_ms // n
            remainder   = total_ms - (ms_per_word * n)

            tagged_parts = []
            for i, word in enumerate(words):
                word_ms = ms_per_word + (remainder if i == n - 1 else 0)
                cs = max(word_ms // 10, 1)
                tagged_parts.append(f"{{\\k{cs}}}{word}")

            karaoke_text = " ".join(tagged_parts)
            start_ass = self._ms_to_ass(start_ms)
            end_ass   = self._ms_to_ass(end_ms)

            events.append(
                f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{karaoke_text}"
            )

        return "\n".join(events)

    # ------------------------------------------------------------------
    # Fallback: per-word individual Dialogue lines (no \k dependency)
    # ------------------------------------------------------------------
    def _build_word_by_word_events(self, entries: list) -> str:
        events = []
        for entry in entries:
            start_ms = entry["start_ms"]
            end_ms   = entry["end_ms"]
            total_ms = max(end_ms - start_ms, 100)
            text     = re.sub(r"<[^>]+>", "", entry["text"].strip()).upper()
            words    = text.split()

            if not words:
                continue

            n = len(words)
            ms_per_word = total_ms // n
            remainder   = total_ms - (ms_per_word * n)

            for i, active_word in enumerate(words):
                word_start_ms = start_ms + i * ms_per_word
                word_end_ms   = word_start_ms + ms_per_word + (remainder if i == n - 1 else 0)

                parts = []
                for j, w in enumerate(words):
                    if j == i:
                        parts.append(
                            f"{{\\c{COLOUR_YELLOW}\\fscx115\\fscy115}}{w}"
                            f"{{\\c{COLOUR_WHITE}\\fscx100\\fscy100}}"
                        )
                    else:
                        parts.append(w)

                line_text = " ".join(parts)
                start_ass = self._ms_to_ass(word_start_ms)
                end_ass   = self._ms_to_ass(word_end_ms)

                events.append(
                    f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{line_text}"
                )

        return "\n".join(events)

    # ------------------------------------------------------------------
    # Re-chunk entries into WORDS_PER_GROUP=4 groups
    # FIX: Guard against total_audio_ms == 0 (division by zero)
    # ------------------------------------------------------------------
    def _rechunk_entries(self, entries: list) -> list:
        if not entries:
            return []

        total_audio_ms = entries[-1]["end_ms"] - entries[0]["start_ms"]
        total_words    = sum(len(e["text"].split()) for e in entries)

        if total_words == 0:
            return entries

        # FIX: Guard division by zero — if ffprobe failed and all timestamps
        # are 0, use a sensible fallback of 500ms per word (typical speech rate)
        if total_audio_ms <= 0:
            logger.warning(
                "_rechunk_entries: total_audio_ms is 0 — "
                "ffprobe may have failed. Using 500ms/word fallback."
            )
            total_audio_ms = total_words * 500

        avg_ms_per_word = total_audio_ms / total_words

        all_words = []
        cursor_ms = entries[0]["start_ms"]
        for entry in entries:
            for word in entry["text"].split():
                all_words.append({
                    "word":     word.upper(),
                    "start_ms": int(cursor_ms),
                    "end_ms":   int(cursor_ms + avg_ms_per_word),
                })
                cursor_ms += avg_ms_per_word

        rechunked = []
        idx = 1
        for i in range(0, len(all_words), WORDS_PER_GROUP):
            chunk = all_words[i: i + WORDS_PER_GROUP]
            if not chunk:
                continue
            rechunked.append({
                "index":    idx,
                "start_ms": chunk[0]["start_ms"],
                "end_ms":   chunk[-1]["end_ms"],
                "text":     " ".join(w["word"] for w in chunk),
            })
            idx += 1

        return rechunked

    # ------------------------------------------------------------------
    # SRT → ASS pipeline
    # ------------------------------------------------------------------
    def _srt_to_ass_karaoke(self, srt_path: str, ass_path: str) -> bool:
        entries = self._parse_srt(srt_path)
        if not entries:
            logger.warning("No SRT entries parsed.")
            return False

        entries = self._rechunk_entries(entries)
        ass_content = self._create_ass_header()
        karaoke_events = self._build_karaoke_events(entries)
        ass_content += karaoke_events + "\n"

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        return True

    def _srt_to_ass_fallback(self, srt_path: str, ass_fallback_path: str) -> bool:
        entries = self._parse_srt(srt_path)
        if not entries:
            return False

        entries = self._rechunk_entries(entries)
        ass_content = self._create_ass_header()
        ass_content += self._build_word_by_word_events(entries) + "\n"

        with open(ass_fallback_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        return True

    def _srt_to_ass_simple(self, srt_path: str, ass_path: str) -> bool:
        """Simple bold-white-all-caps style — safety net fallback."""
        entries = self._parse_srt(srt_path)
        if not entries:
            return False

        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "PlayResX: 1080\n"
            "PlayResY: 1920\n"
            "ScaledBorderAndShadow: yes\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
            "-1,0,0,0,100,100,2,0,1,6,3,5,80,80,0,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        events = []
        for e in self._rechunk_entries(entries):
            start = self._ms_to_ass(e["start_ms"])
            end   = self._ms_to_ass(e["end_ms"])
            text  = re.sub(r"<[^>]+>", "", e["text"]).upper()
            events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(header + "\n".join(events) + "\n")

        return True

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(
        self,
        voice_result: dict,
        video_id:     str,
        output_dir:   str = "/tmp",
        *args, **kwargs,
    ) -> dict:
        logger.info(
            f"CaptionMakerAgent → center-screen word-highlight captions for: {video_id}"
        )

        srt_path          = voice_result.get("subtitle_path", f"{output_dir}/{video_id}_subtitles.srt")
        ass_karaoke_path  = f"{output_dir}/{video_id}_captions_karaoke.ass"
        ass_fallback_path = f"{output_dir}/{video_id}_captions_fallback.ass"
        ass_simple_path   = f"{output_dir}/{video_id}_captions_simple.ass"

        if not srt_path or not os.path.exists(srt_path):
            logger.warning(f"No SRT file at: {srt_path}")
            return {
                "success":        False,
                "srt_path":       srt_path,
                "ass_path":       None,
                "caption_style":  "none",
                "error":          "No subtitle file found",
            }

        entries = self._parse_srt(srt_path)
        total_words = sum(len(e["text"].split()) for e in entries)

        # --- Attempt 1: Karaoke \k tags (best quality) ---
        karaoke_ok = False
        try:
            karaoke_ok = self._srt_to_ass_karaoke(srt_path, ass_karaoke_path)
        except Exception as e:
            logger.warning(f"Karaoke ASS generation failed: {e}")

        # --- Attempt 2: Word-by-word individual lines ---
        fallback_ok = False
        try:
            fallback_ok = self._srt_to_ass_fallback(srt_path, ass_fallback_path)
        except Exception as e:
            logger.warning(f"Fallback ASS generation failed: {e}")

        # --- Attempt 3: Simple bold captions ---
        simple_ok = False
        try:
            simple_ok = self._srt_to_ass_simple(srt_path, ass_simple_path)
        except Exception as e:
            logger.warning(f"Simple ASS generation failed: {e}")

        # Choose best available
        if karaoke_ok and os.path.exists(ass_karaoke_path):
            chosen_ass  = ass_karaoke_path
            style       = "karaoke_highlight_center"
            logger.success(
                f"Captions ✅ [{style}] | {total_words} words | "
                f"{len(self._rechunk_entries(entries))} groups of {WORDS_PER_GROUP} | "
                f"Center-screen 72px"
            )
        elif fallback_ok and os.path.exists(ass_fallback_path):
            chosen_ass  = ass_fallback_path
            style       = "word_by_word_center"
            logger.info(f"Captions [{style}] → {chosen_ass}")
        elif simple_ok and os.path.exists(ass_simple_path):
            chosen_ass  = ass_simple_path
            style       = "simple_bold_center"
            logger.info(f"Captions [{style}] → {chosen_ass}")
        else:
            logger.warning("All ASS generation failed — video composer will use raw SRT")
            return {
                "success":        True,
                "srt_path":       srt_path,
                "ass_path":       None,
                "caption_style":  "srt_fallback",
                "subtitle_count": len(entries),
                "word_count":     total_words,
            }

        return {
            "success":              True,
            "srt_path":             srt_path,
            "ass_path":             chosen_ass,
            "ass_karaoke_path":     ass_karaoke_path  if karaoke_ok  else None,
            "ass_fallback_path":    ass_fallback_path if fallback_ok else None,
            "ass_simple_path":      ass_simple_path   if simple_ok   else None,
            "caption_style":        style,
            "subtitle_count":       len(entries),
            "word_count":           total_words,
            "words_per_group":      WORDS_PER_GROUP,
            "position":             "center_screen",
            "font_size":            72,
        }
