"""
agents/caption_maker.py
Processes SRT subtitle files and prepares them for video burning via FFmpeg.
"""
import os
import re
from pathlib import Path
from loguru import logger


class CaptionMakerAgent:
    def __init__(self, config):
        self.config = config

    def _parse_srt(self, srt_path: str) -> list:
        """Parse SRT file into list of subtitle entries."""
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
                    entries.append({"index": idx, "times": times, "text": text})
                except (ValueError, IndexError):
                    continue

        return entries

    def _create_ass_style(self) -> str:
        """Create ASS subtitle style for bold, centered captions."""
        return """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,80,80,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def _srt_time_to_ass(self, srt_time: str) -> str:
        """Convert SRT timestamp (00:00:00,000) to ASS format (0:00:00.00)."""
        try:
            srt_time = srt_time.replace(",", ".")
            parts = srt_time.split(":")
            h = int(parts[0])
            m = int(parts[1])
            s_ms = parts[2].split(".")
            s = int(s_ms[0])
            ms = int(s_ms[1][:2]) if len(s_ms) > 1 else 0
            return f"{h}:{m:02d}:{s:02d}.{ms:02d}"
        except Exception:
            return "0:00:00.00"

    def _srt_to_ass(self, srt_path: str, ass_path: str) -> bool:
        """Convert SRT to ASS format for better FFmpeg styling."""
        entries = self._parse_srt(srt_path)
        if not entries:
            return False

        ass_content = self._create_ass_style()
        for entry in entries:
            times = entry["times"]
            if "-->" in times:
                start, end = times.split("-->")
                start_ass = self._srt_time_to_ass(start.strip())
                end_ass = self._srt_time_to_ass(end.strip())
                text = entry["text"].replace("\n", "\\N")
                # Make text uppercase for impact
                text = text.upper()
                ass_content += f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{text}\n"

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        return True

    def run(self, voice_result: dict, video_id: str, output_dir: str = "/tmp", *args, **kwargs):
        logger.info(f"CaptionMakerAgent processing captions for video: {video_id}")

        srt_path = voice_result.get("subtitle_path", f"{output_dir}/{video_id}_subtitles.srt")
        ass_path = f"{output_dir}/{video_id}_captions.ass"

        if not srt_path or not os.path.exists(srt_path):
            logger.warning(f"No SRT file found at: {srt_path}")
            return {
                "success": False,
                "srt_path": srt_path,
                "ass_path": None,
                "error": "No subtitle file found",
            }

        success = self._srt_to_ass(srt_path, ass_path)

        if success:
            logger.success(f"Captions processed: {ass_path}")
            return {
                "success": True,
                "srt_path": srt_path,
                "ass_path": ass_path,
                "subtitle_count": len(self._parse_srt(srt_path)),
            }
        else:
            logger.warning("ASS conversion failed; will use SRT directly.")
            return {
                "success": True,  # Non-fatal; video composer can use SRT
                "srt_path": srt_path,
                "ass_path": None,
                "subtitle_count": 0,
            }
