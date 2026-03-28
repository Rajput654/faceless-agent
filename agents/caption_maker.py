"""
agents/caption_maker.py

UPGRADED: Word-by-word highlight captions — MrBeast / premium Shorts style.

How it works:
  1. The TTS server (edge-tts) already collects per-word timing in _wbs_to_srt()
     and writes a standard SRT file where each caption block = 5 words.
  2. This agent parses that SRT to get word-group timings.
  3. It then builds an ASS subtitle file where:
     - Each 5-word group appears as a subtitle event
     - WITHIN that group, each individual word is highlighted in YELLOW
       as it is spoken, while the rest of the group stays WHITE
     - This creates the "karaoke highlight" effect used by every
       premium faceless channel

The result looks like this on screen (one frame example):
  "nobody told you  [THIS]  about money"
  where [THIS] is yellow and everything else is white.

TECHNICAL APPROACH:
  Because edge-tts gives us word-level offsets (in 100-nanosecond units),
  we can calculate exactly when each word starts and ends.
  We generate one ASS Dialogue line per WORD, where the full phrase is shown
  but only the current word uses the highlight colour override tag {\\c&H00FFFF&}.

  Wait — why not use ASS karaoke tags (\k)?
  \k tags only work inside a single dialogue line and require the engine to
  support karaoke. Many FFmpeg subtitle renderers (libass) DO support \k,
  but the timing units are centiseconds which we have. So we use BOTH:
  - Primary method: \k karaoke tags inside grouped lines (most compatible)
  - Fallback: one Dialogue line per word (works even if \k is unsupported)

FONT STYLE:
  Font: Arial Black (bold, high contrast)
  Size: 54px (safe for 1080 wide, no overflow on 4 words)
  Primary (unhighlighted): White &H00FFFFFF
  Highlight (active word): Yellow &H0000FFFF  (AABBGGRR in ASS = 0x00FFFF00 → yellow)
  Outline: Black, 4px thick
  Shadow: 2px
  Position: Bottom-centre, 200px margin from bottom (above safe zone)
"""
import os
import re
from pathlib import Path
from loguru import logger


# ---------------------------------------------------------------------------
# Colour constants (ASS format: &HAABBGGRR — alpha, blue, green, red)
# ---------------------------------------------------------------------------
COLOUR_WHITE      = "&H00FFFFFF"   # normal word
COLOUR_YELLOW     = "&H0000FFFF"   # active/highlighted word  (yellow in BGR)
COLOUR_OUTLINE    = "&H00000000"   # black outline
COLOUR_SHADOW     = "&H80000000"   # semi-transparent black shadow
COLOUR_BACKGROUND = "&H00000000"   # transparent box background

# Words per caption group — 4 is the premium sweet spot (readable + fast)
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
        """Convert SRT timestamp (HH:MM:SS,mmm) to milliseconds."""
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
        """Convert milliseconds to ASS timestamp (H:MM:SS.cc centiseconds)."""
        h    = ms // 3_600_000
        m    = (ms % 3_600_000) // 60_000
        s    = (ms % 60_000) // 1_000
        cs   = (ms % 1_000) // 10   # centiseconds
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    # ------------------------------------------------------------------
    # ASS header — defines the visual style
    # ------------------------------------------------------------------
    def _create_ass_header(self) -> str:
        """
        Premium caption style:
        - Arial Black, 54px
        - White text by default, yellow highlight
        - Thick black outline + soft shadow
        - Bottom-centre alignment with margin above safe zone
        """
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
            # Main style — white, bold, thick outline
            f"Style: Default,Arial,54,{COLOUR_WHITE},{COLOUR_YELLOW},"
            f"{COLOUR_OUTLINE},{COLOUR_SHADOW},"
            "-1,0,0,0,"                    # Bold=-1(true), Italic, Underline, StrikeOut
            "100,100,0,0,"                 # ScaleX, ScaleY, Spacing, Angle
            "1,4,2,"                       # BorderStyle, Outline, Shadow
            "2,80,80,200,1\n"              # Alignment=2(bottom-centre), margins, encoding
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

    # ------------------------------------------------------------------
    # CORE UPGRADE: Word-by-word karaoke highlight builder
    # ------------------------------------------------------------------
    def _build_karaoke_events(self, entries: list) -> str:
        """
        Takes parsed SRT entries (each entry = a group of ~5 words with timing)
        and builds ASS karaoke dialogue lines where each word highlights
        in yellow as it is spoken.

        ASS karaoke tag: {\\kNN} where NN = duration in centiseconds
        The \\k tag advances the highlight by NN centiseconds per word.

        Example output for one subtitle event:
          Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,
            {\\k20}nobody {\\k18}told {\\k15}you {\\k22}THIS {\\k19}about

        The secondary colour (yellow) fills in from left to right as each
        word's centisecond count elapses.
        """
        events = []
        for entry in entries:
            start_ms = entry["start_ms"]
            end_ms   = entry["end_ms"]
            total_ms = max(end_ms - start_ms, 100)
            text     = entry["text"].strip()

            # Clean the text
            text = re.sub(r"<[^>]+>", "", text)   # strip HTML
            text = text.upper()                     # uppercase = premium look

            words = text.split()
            if not words:
                continue

            n = len(words)

            # Distribute time evenly across words
            # (SRT from edge-tts already has word-group timing;
            #  we subdivide each group proportionally)
            ms_per_word = total_ms // n
            remainder   = total_ms - (ms_per_word * n)

            # Build the karaoke-tagged line
            # {\\k<cs>} = centiseconds for this word's highlight
            tagged_parts = []
            for i, word in enumerate(words):
                # Last word gets any leftover milliseconds
                word_ms = ms_per_word + (remainder if i == n - 1 else 0)
                cs = max(word_ms // 10, 1)   # convert ms → centiseconds, min 1
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
        """
        Fallback method for renderers that don't support ASS karaoke.
        Generates one Dialogue line per WORD. Each line shows the full
        group text, but wraps the current word in a yellow colour override.

        This is more Dialogue lines but guaranteed to work everywhere.
        """
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

                # Build line: grey out all words except current
                parts = []
                for j, w in enumerate(words):
                    if j == i:
                        # Active word → yellow + slight scale boost
                        parts.append(f"{{\\c{COLOUR_YELLOW}\\fscx110\\fscy110}}{w}{{\\c{COLOUR_WHITE}\\fscx100\\fscy100}}")
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
    # Rechunk entries into WORDS_PER_GROUP groups if needed
    # ------------------------------------------------------------------
    def _rechunk_entries(self, entries: list) -> list:
        """
        SRT from edge-tts already groups words into ~5-word blocks.
        This function re-chunks them to exactly WORDS_PER_GROUP words
        for visual consistency, redistributing time proportionally.
        """
        # Flatten all words with their approximate per-word timing
        all_words  = []
        total_span_ms = 0

        if not entries:
            return []

        total_audio_ms = entries[-1]["end_ms"] - entries[0]["start_ms"]
        total_words    = sum(len(e["text"].split()) for e in entries)

        if total_words == 0:
            return entries

        # Average ms per word across the whole script
        avg_ms_per_word = total_audio_ms / total_words

        # Rebuild word list with absolute start times
        cursor_ms = entries[0]["start_ms"]
        for entry in entries:
            for word in entry["text"].split():
                all_words.append({
                    "word":     word.upper(),
                    "start_ms": int(cursor_ms),
                    "end_ms":   int(cursor_ms + avg_ms_per_word),
                })
                cursor_ms += avg_ms_per_word

        # Group into WORDS_PER_GROUP chunks
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
    # Full SRT → ASS pipeline
    # ------------------------------------------------------------------
    def _srt_to_ass_karaoke(self, srt_path: str, ass_path: str) -> bool:
        entries = self._parse_srt(srt_path)
        if not entries:
            logger.warning("No SRT entries parsed.")
            return False

        # Rechunk to WORDS_PER_GROUP for consistent visual pacing
        entries = self._rechunk_entries(entries)

        ass_content = self._create_ass_header()

        # PRIMARY: karaoke \k tags
        karaoke_events = self._build_karaoke_events(entries)
        ass_content += karaoke_events + "\n"

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        return True

    # ------------------------------------------------------------------
    # Also write a "word-by-word fallback" ASS for the video server
    # to use if libass karaoke is not supported
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Simple legacy ASS (kept as final safety net)
    # ------------------------------------------------------------------
    def _srt_to_ass_simple(self, srt_path: str, ass_path: str) -> bool:
        """
        Original bold-white-all-caps style — used only if karaoke fails.
        """
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
            "Style: Default,Arial,56,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
            "-1,0,0,0,100,100,1,0,1,4,2,2,60,60,180,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        events = []
        for e in entries:
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
        logger.info(f"CaptionMakerAgent → word-by-word highlight captions for: {video_id}")

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

        # Count words for logging
        entries = self._parse_srt(srt_path)
        total_words = sum(len(e["text"].split()) for e in entries)

        # --- Attempt 1: Karaoke \k tags (best quality) ---
        karaoke_ok = False
        try:
            karaoke_ok = self._srt_to_ass_karaoke(srt_path, ass_karaoke_path)
        except Exception as e:
            logger.warning(f"Karaoke ASS generation failed: {e}")

        # --- Attempt 2: Word-by-word individual lines (fallback) ---
        fallback_ok = False
        try:
            fallback_ok = self._srt_to_ass_fallback(srt_path, ass_fallback_path)
        except Exception as e:
            logger.warning(f"Fallback ASS generation failed: {e}")

        # --- Attempt 3: Simple bold captions (safety net) ---
        simple_ok = False
        try:
            simple_ok = self._srt_to_ass_simple(srt_path, ass_simple_path)
        except Exception as e:
            logger.warning(f"Simple ASS generation failed: {e}")

        # Choose the best available caption file
        # Priority: karaoke > fallback > simple > srt
        if karaoke_ok and os.path.exists(ass_karaoke_path):
            chosen_ass  = ass_karaoke_path
            style       = "karaoke_highlight"
            logger.success(
                f"Captions ready [{style}] | {total_words} words | "
                f"{len(self._rechunk_entries(entries))} groups → {chosen_ass}"
            )
        elif fallback_ok and os.path.exists(ass_fallback_path):
            chosen_ass  = ass_fallback_path
            style       = "word_by_word"
            logger.info(f"Captions ready [{style}] → {chosen_ass}")
        elif simple_ok and os.path.exists(ass_simple_path):
            chosen_ass  = ass_simple_path
            style       = "simple_bold"
            logger.info(f"Captions ready [{style}] → {chosen_ass}")
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
        }
