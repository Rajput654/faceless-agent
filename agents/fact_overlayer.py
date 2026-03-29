"""
agents/fact_overlayer.py

Burns bold on-screen fact/keyword overlays into the video at key moments.
These are the large-text cards that appear mid-screen — the "dual stimulus"
technique used by every top faceless channel.

What it does:
  - Takes key_facts from the Pass 1 extractor output (via script["_extracted"])
  - Places up to 3 bold yellow text cards at 25%, 50%, 72% of video duration
  - Each card appears for 2.0 seconds with a subtle fade-in/fade-out
  - Cards are center-screen (not competing with caption track at bottom)
  - Falls back gracefully — if overlay fails, original video is returned

Visual style:
  - Font: DejaVu Sans Bold (always available on Ubuntu/CI)
  - Size: 72px (readable on mobile)
  - Color: Yellow text, thick black border (matches caption highlight color)
  - Position: Vertically centered, horizontally centered
  - Animation: 0.2s fade in, 0.2s fade out per card

This creates the "dual stimulus" effect: viewer is reading the card AND
listening to the voice simultaneously, doubling engagement and killing
the urge to swipe.
"""
import os
import re
import subprocess
from pathlib import Path
from loguru import logger


# System font paths (in order of preference, all available on Ubuntu)
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
    return ""  # ffmpeg will use built-in default if no font found


def _clean_text_for_drawtext(text: str, max_chars: int = 40) -> str:
    """
    Sanitize text for FFmpeg drawtext filter.
    drawtext is sensitive to: single quotes, colons, backslashes, newlines.
    """
    # Truncate
    text = text[:max_chars]
    # Remove problematic chars
    text = re.sub(r"[':=\\]", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Uppercase for impact
    return text.upper()


def _split_into_lines(text: str, max_chars_per_line: int = 20) -> str:
    """
    Split long text into two lines for better readability on mobile.
    Returns text with \\n separator for drawtext multiline.
    """
    words = text.split()
    if not words:
        return text

    line1, line2 = [], []
    char_count = 0
    on_line2 = False

    for word in words:
        if not on_line2 and char_count + len(word) + 1 > max_chars_per_line:
            on_line2 = True
            char_count = 0
        if on_line2:
            line2.append(word)
        else:
            line1.append(word)
            char_count += len(word) + 1

    if line2:
        return " ".join(line1) + r"\n" + " ".join(line2)
    return " ".join(line1)


class FactOverlayerAgent:
    def __init__(self, config):
        self.config = config
        self.font_path = _find_font()

    def run(
        self,
        video_path: str,
        script: dict,
        video_id: str,
        output_dir: str = "/tmp",
        *args, **kwargs,
    ) -> dict:
        logger.info(f"FactOverlayerAgent → burning fact overlays for: {video_id}")

        if not video_path or not os.path.exists(video_path):
            logger.warning("No video path provided to FactOverlayerAgent")
            return {"success": False, "video_path": video_path, "overlays_added": 0}

        extracted = script.get("_extracted", {})
        key_facts = extracted.get("key_facts", [])

        # Also try to get a "wow" number or statistic from the script title/hook
        hook = script.get("hook", "")
        title = script.get("title", "")

        # Build overlay texts — key facts are best, fall back to hook keywords
        overlay_texts = []
        for fact in key_facts:
            if fact and len(fact.strip()) > 3:
                overlay_texts.append(fact.strip())

        # If we have fewer than 2 overlay texts, extract hook keywords
        if len(overlay_texts) < 2 and hook:
            # Extract the most impactful words from the hook
            hook_words = " ".join(hook.split()[:6])
            if hook_words not in overlay_texts:
                overlay_texts.insert(0, hook_words)

        if not overlay_texts:
            logger.info("No overlay texts found — skipping fact overlays")
            return {"success": True, "video_path": video_path, "overlays_added": 0}

        # Get video duration
        duration = self._get_duration(video_path)
        if duration <= 0:
            logger.warning("Could not determine video duration")
            return {"success": True, "video_path": video_path, "overlays_added": 0}

        # Placement timestamps: 20%, 48%, 72% of video
        # Avoid the first 3s (hook) and last 5s (CTA)
        safe_start = 4.0
        safe_end = max(duration - 6.0, duration * 0.7)
        safe_range = safe_end - safe_start

        placement_ratios = [0.20, 0.50, 0.75]
        timestamps = [
            safe_start + safe_range * r
            for r in placement_ratios[:len(overlay_texts)]
        ]

        output_path = f"{output_dir}/{video_id}_overlaid.mp4"

        result = self._burn_overlays(
            video_path=video_path,
            output_path=output_path,
            overlay_texts=overlay_texts[:3],
            timestamps=timestamps,
            duration=duration,
        )

        if result:
            logger.success(
                f"Fact overlays burned ✅  {len(overlay_texts[:3])} cards → {output_path}"
            )
            return {
                "success": True,
                "video_path": output_path,
                "overlays_added": len(overlay_texts[:3]),
                "texts_used": overlay_texts[:3],
            }
        else:
            logger.warning("Fact overlay burn failed — returning original video")
            return {
                "success": True,  # non-fatal
                "video_path": video_path,
                "overlays_added": 0,
            }

    def _burn_overlays(
        self,
        video_path: str,
        output_path: str,
        overlay_texts: list,
        timestamps: list,
        duration: float,
        card_duration: float = 2.0,
        fade_duration: float = 0.2,
    ) -> bool:
        """
        Burn all overlay cards into the video using a single FFmpeg pass.
        Uses drawtext with alpha/fade via the enable expression.
        """
        font_arg = f"fontfile={self.font_path}:" if self.font_path else ""

        drawtext_filters = []

        for i, (text, ts) in enumerate(zip(overlay_texts, timestamps)):
            clean = _clean_text_for_drawtext(text, max_chars=38)
            display = _split_into_lines(clean, max_chars_per_line=18)

            if not clean:
                continue

            t_start = ts
            t_end = ts + card_duration
            t_fade_in_end = t_start + fade_duration
            t_fade_out_start = t_end - fade_duration

            # Alpha expression: fade in → sustain → fade out
            # FFmpeg alpha via drawtext doesn't support per-character fade,
            # but we can use 'alpha' expression for the whole text block.
            # We layer a semi-transparent black box behind using a second drawtext trick.

            # Background shadow box (black, semi-transparent via box)
            box_filter = (
                f"drawtext="
                f"{font_arg}"
                f"text='{display}':"
                f"fontsize=72:"
                f"fontcolor=black@0.0:"  # invisible text, box only
                f"box=1:boxcolor=black@0.5:boxborderw=20:"
                f"x=(w-text_w)/2:y=(h/2-text_h/2-10):"
                f"enable='between(t,{t_start:.2f},{t_end:.2f})'"
            )

            # Main text (yellow, thick black border)
            text_filter = (
                f"drawtext="
                f"{font_arg}"
                f"text='{display}':"
                f"fontsize=72:"
                f"fontcolor=yellow:"
                f"bordercolor=black:borderw=5:"
                f"x=(w-text_w)/2:y=(h/2-text_h/2-10):"
                f"alpha='if(lt(t,{t_fade_in_end:.2f}),"
                f"(t-{t_start:.2f})/{fade_duration:.2f},"
                f"if(gt(t,{t_fade_out_start:.2f}),"
                f"({t_end:.2f}-t)/{fade_duration:.2f},1))':"
                f"enable='between(t,{t_start:.2f},{t_end:.2f})'"
            )

            drawtext_filters.append(box_filter)
            drawtext_filters.append(text_filter)

        if not drawtext_filters:
            return False

        vf = ",".join(drawtext_filters)

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "21",
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path,
        ]

        logger.debug(f"FFmpeg overlay command: {' '.join(cmd[:12])}...")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                logger.error(f"FFmpeg overlay error:\n{result.stderr[-1500:]}")
                return False

            size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if size < 10_000:
                logger.error(f"Overlay output too small: {size} bytes")
                return False

            return True

        except subprocess.TimeoutExpired:
            logger.error("FFmpeg overlay timed out")
            return False
        except Exception as e:
            logger.error(f"FFmpeg overlay exception: {e}")
            return False

    def _get_duration(self, path: str) -> float:
        try:
            r = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True, text=True, timeout=15,
            )
            return float(r.stdout.strip())
        except Exception:
            return 0.0
