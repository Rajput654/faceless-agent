"""
mcp_servers/tts_server.py
Text-to-Speech using Microsoft Edge TTS (free, no API key needed).
Generates MP3 audio + SRT subtitle files.

edge-tts 6.x changed the SubMaker API — this module handles both old and new.
Audio is always saved even if subtitle generation fails (non-fatal).
"""
import os
import re
import asyncio
from pathlib import Path
from loguru import logger

try:
    import edge_tts
    EDGE_TTS_VERSION = getattr(edge_tts, "__version__", "unknown")
    logger.debug(f"edge-tts version: {EDGE_TTS_VERSION}")
except ImportError:
    edge_tts = None
    EDGE_TTS_VERSION = None


def _parse_vtt_to_srt(vtt_content: str) -> str:
    """Convert WebVTT format to SRT format."""
    if isinstance(vtt_content, bytes):
        vtt_content = vtt_content.decode("utf-8")

    lines = vtt_content.strip().split("\n")
    srt_lines = []
    counter = 1
    i = 0

    # Skip WEBVTT header
    while i < len(lines) and "-->" not in lines[i]:
        i += 1

    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            timestamp = line.replace(".", ",", 2)
            timestamp = re.sub(
                r'\s+align:\S+|\s+position:\S+|\s+line:\S+|\s+size:\S+', '', timestamp
            )
            srt_lines.append(str(counter))
            srt_lines.append(timestamp)
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip() != "":
                text_lines.append(lines[i].strip())
                i += 1
            srt_lines.append("\n".join(text_lines))
            srt_lines.append("")
            counter += 1
        else:
            i += 1

    return "\n".join(srt_lines)


def _word_boundary_to_srt(word_boundaries: list) -> str:
    """
    Convert a list of WordBoundary chunks directly to SRT.
    This is the fallback when SubMaker is unavailable or broken.
    Each entry: {"offset": int_100ns, "duration": int_100ns, "text": str}
    """
    if not word_boundaries:
        return ""

    srt_lines = []
    # Group words into ~5-word chunks for readable captions
    chunk_size = 5
    chunks = [word_boundaries[i:i+chunk_size] for i in range(0, len(word_boundaries), chunk_size)]

    for idx, chunk in enumerate(chunks, start=1):
        start_100ns = chunk[0]["offset"]
        end_100ns = chunk[-1]["offset"] + chunk[-1]["duration"]

        def fmt(ns_100):
            total_ms = ns_100 // 10_000
            h = total_ms // 3_600_000
            m = (total_ms % 3_600_000) // 60_000
            s = (total_ms % 60_000) // 1_000
            ms = total_ms % 1_000
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        text = " ".join(w["text"] for w in chunk)
        srt_lines.append(str(idx))
        srt_lines.append(f"{fmt(start_100ns)} --> {fmt(end_100ns)}")
        srt_lines.append(text)
        srt_lines.append("")

    return "\n".join(srt_lines)


async def _generate_speech_async(
    text: str,
    output_path: str,
    subtitle_path: str,
    voice: str,
    rate: str,
    pitch: str,
    volume: str,
):
    """
    Async speech generation.
    Strategy:
      1. Stream audio + collect WordBoundary events in one pass.
      2. Try to build subtitles via SubMaker (handles edge-tts API changes).
      3. Fall back to manual SRT from raw WordBoundary data.
      4. If all subtitle paths fail, write an empty SRT — audio is never sacrificed.
    """
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)

    word_boundaries = []

    # --- Pass 1: stream audio + collect word boundaries ---
    with open(output_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                word_boundaries.append({
                    "offset": chunk.get("offset", 0),
                    "duration": chunk.get("duration", 0),
                    "text": chunk.get("text", ""),
                })

    # --- Pass 2: build subtitles ---
    srt_content = ""

    # Attempt A: SubMaker (try both old and new API signatures)
    try:
        sub_maker = edge_tts.SubMaker()

        for wb in word_boundaries:
            try:
                # edge-tts >= 6.1.x  feed(chunk_dict)
                sub_maker.feed({"type": "WordBoundary", **wb})
            except (TypeError, AttributeError):
                try:
                    # Older 6.x  create_sub((offset, duration), text)
                    sub_maker.create_sub((wb["offset"], wb["duration"]), wb["text"])
                except Exception:
                    pass  # SubMaker unusable — will fall back below

        # Try new API first, then old
        vtt_content = None
        for get_fn in ("get_subs", "generate_subs"):
            fn = getattr(sub_maker, get_fn, None)
            if fn:
                try:
                    vtt_content = fn()
                    break
                except Exception:
                    continue

        if vtt_content:
            srt_content = _parse_vtt_to_srt(
                vtt_content if isinstance(vtt_content, str) else vtt_content.decode("utf-8", errors="replace")
            )
            logger.debug("Subtitles built via SubMaker")

    except Exception as e:
        logger.warning(f"SubMaker failed ({e}); falling back to manual SRT")

    # Attempt B: manual SRT from raw word boundaries
    if not srt_content and word_boundaries:
        srt_content = _word_boundary_to_srt(word_boundaries)
        logger.debug("Subtitles built via manual WordBoundary grouping")

    # Always write *something* to subtitle_path so downstream code doesn't crash
    with open(subtitle_path, "w", encoding="utf-8") as srt_file:
        srt_file.write(srt_content)

    if not srt_content:
        logger.warning("No subtitle content generated — empty SRT written")


class TTSMCPServer:
    """Text-to-Speech MCP Server using Microsoft Edge TTS."""

    AVAILABLE_VOICES = [
        "en-US-GuyNeural",
        "en-US-AriaNeural",
        "en-US-JennyNeural",
        "en-US-DavisNeural",
        "en-GB-RyanNeural",
        "en-AU-NatashaNeural",
    ]

    def __init__(self):
        self.tools = {
            "generate_speech": self._generate_speech,
            "list_voices": self._list_voices,
        }

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _generate_speech(
        self,
        text: str,
        output_path: str,
        subtitle_path: str,
        voice: str = "en-US-GuyNeural",
        rate: str = "+10%",
        pitch: str = "0Hz",
        volume: str = "+0%",
        **kwargs,
    ):
        if not edge_tts:
            return {"success": False, "error": "edge-tts not installed. Run: pip install edge-tts"}

        if not text or not text.strip():
            return {"success": False, "error": "Empty text provided"}

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(subtitle_path).parent.mkdir(parents=True, exist_ok=True)

        if voice not in self.AVAILABLE_VOICES:
            logger.warning(f"Voice {voice} not in known list, trying anyway...")

        try:
            # Run async in sync context — handle already-running loops (e.g. Jupyter)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    raise RuntimeError("Loop already running")
                if loop.is_closed():
                    raise RuntimeError("Loop closed")
                loop.run_until_complete(
                    _generate_speech_async(text, output_path, subtitle_path, voice, rate, pitch, volume)
                )
            except RuntimeError:
                asyncio.run(
                    _generate_speech_async(text, output_path, subtitle_path, voice, rate, pitch, volume)
                )

            audio_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            sub_size = os.path.getsize(subtitle_path) if os.path.exists(subtitle_path) else 0

            if audio_size == 0:
                return {"success": False, "error": "Audio file is empty after TTS generation"}

            logger.success(
                f"TTS generated: {output_path} ({audio_size} bytes) | "
                f"subtitles: {subtitle_path} ({sub_size} bytes)"
            )
            return {
                "success": True,
                "audio_path": output_path,
                "subtitle_path": subtitle_path,
                "audio_size_bytes": audio_size,
                "voice_used": voice,
            }

        except Exception as e:
            logger.error(f"TTS generation failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _list_voices(self, **kwargs):
        return {"success": True, "voices": self.AVAILABLE_VOICES}
