"""
agents/caption_maker.py

Processes SRT subtitle files and converts them to ASS format
with a bold, centered, YouTube-Shorts-style pop caption look.
"""
import os
import re
from pathlib import Path
from loguru import logger


class CaptionMakerAgent:
    def __init__(self, config):
        self.config = config

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
                    idx  = int(lines[0].strip())
                    times = lines[1].strip()
                    text  = " ".join(lines[2:]).strip()
                    entries.append({"index": idx, "times": times, "text": text})
                except (ValueError, IndexError):
                    continue
        return entries

    def _create_ass_header(self) -> str:
        """
        ASS header with a Shorts-style bold white caption.
        FontSize 56 is safe for 1080-wide portrait video — won't overflow.
        Alignment=2 = bottom-centre; MarginV=180 keeps text above the safe zone.
        """
        return (
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
            # Bold white text, thick black outline, drop shadow — classic Shorts style
            "Style: Default,Arial,56,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
            "-1,0,0,0,100,100,1,0,1,4,2,2,60,60,180,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

    def _srt_time_to_ass(self, srt_time: str) -> str:
        try:
            srt_time = srt_time.strip().replace(",", ".")
            parts = srt_time.split(":")
            h = int(parts[0])
            m = int(parts[1])
            s_ms = parts[2].split(".")
            s    = int(s_ms[0])
            cs   = int(s_ms[1][:2]) if len(s_ms) > 1 else 0
            return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
        except Exception:
            return "0:00:00.00"

    def _srt_to_ass(self, srt_path: str, ass_path: str) -> bool:
        entries = self._parse_srt(srt_path)
        if not entries:
            return False

        ass_content = self._create_ass_header()
        for entry in entries:
            if "-->" not in entry["times"]:
                continue
            start, end = entry["times"].split("-->")
            start_ass  = self._srt_time_to_ass(start)
            end_ass    = self._srt_time_to_ass(end)
            text       = entry["text"].replace("\n", "\\N")
            # Uppercase for punch — standard YouTube Shorts caption style
            text = text.upper()
            # Strip HTML tags that sometimes appear in SRT
            text = re.sub(r"<[^>]+>", "", text)
            ass_content += (
                f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{text}\n"
            )

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        return True

    def run(
        self,
        voice_result: dict,
        video_id:     str,
        output_dir:   str = "/tmp",
        *args, **kwargs,
    ):
        logger.info(f"CaptionMakerAgent processing captions for: {video_id}")

        srt_path = voice_result.get(
            "subtitle_path", f"{output_dir}/{video_id}_subtitles.srt"
        )
        ass_path = f"{output_dir}/{video_id}_captions.ass"

        if not srt_path or not os.path.exists(srt_path):
            logger.warning(f"No SRT file found at: {srt_path}")
            return {
                "success":   False,
                "srt_path":  srt_path,
                "ass_path":  None,
                "error":     "No subtitle file found",
            }

        success = self._srt_to_ass(srt_path, ass_path)

        if success:
            count = len(self._parse_srt(srt_path))
            logger.success(f"Captions processed ({count} entries): {ass_path}")
            return {
                "success":        True,
                "srt_path":       srt_path,
                "ass_path":       ass_path,
                "subtitle_count": count,
            }

        logger.warning("ASS conversion failed; video composer will use SRT directly")
        return {
            "success":        True,
            "srt_path":       srt_path,
            "ass_path":       None,
            "subtitle_count": 0,
        }
