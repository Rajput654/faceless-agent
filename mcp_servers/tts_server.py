"""
mcp_servers/tts_server.py
Text-to-Speech using Microsoft Edge TTS (free, no API key needed).
Generates MP3 audio + SRT subtitle files.
"""
import os
import re
import asyncio
from pathlib import Path
from loguru import logger

try:
    import edge_tts
except ImportError:
    edge_tts = None


def _parse_vtt_to_srt(vtt_content: str) -> str:
    """Convert WebVTT format to SRT format."""
    lines = vtt_content.strip().split("\n")
    srt_lines = []
    counter = 1
    i = 0

    # Skip WEBVTT header
    while i < len(lines) and not "-->" in lines[i]:
        i += 1

    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            # Convert VTT timestamp (00:00:00.000) to SRT (00:00:00,000)
            timestamp = line.replace(".", ",", 2)
            # Remove any VTT positioning cues
            timestamp = re.sub(r'\s+align:\S+|\s+position:\S+|\s+line:\S+|\s+size:\S+', '', timestamp)
            srt_lines.append(str(counter))
            srt_lines.append(timestamp)
            i += 1
            # Collect subtitle text
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


async def _generate_speech_async(text: str, output_path: str, subtitle_path: str, voice: str, rate: str, pitch: str, volume: str):
    """Async speech generation with edge-tts."""
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    sub_maker = edge_tts.SubMaker()

    with open(output_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                sub_maker.create_sub((chunk["offset"], chunk["duration"]), chunk["text"])

    # Save subtitles
    vtt_content = sub_maker.generate_subs()
    if isinstance(vtt_content, bytes):
        vtt_content = vtt_content.decode("utf-8")
    srt_content = _parse_vtt_to_srt(vtt_content)
    with open(subtitle_path, "w", encoding="utf-8") as srt_file:
        srt_file.write(srt_content)


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

        # Ensure output directories exist
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(subtitle_path).parent.mkdir(parents=True, exist_ok=True)

        # Validate voice
        if voice not in self.AVAILABLE_VOICES:
            logger.warning(f"Voice {voice} not in known list, trying anyway...")

        try:
            # Run async in sync context
            try:
                loop = asyncio.get_event_loop()
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
                return {"success": False, "error": "Audio file is empty"}

            logger.success(f"TTS generated: {output_path} ({audio_size} bytes), subtitles: {subtitle_path} ({sub_size} bytes)")
            return {
                "success": True,
                "audio_path": output_path,
                "subtitle_path": subtitle_path,
                "audio_size_bytes": audio_size,
                "voice_used": voice,
            }

        except Exception as e:
            logger.error(f"TTS generation failed: {e}")
            return {"success": False, "error": str(e)}

    def _list_voices(self, **kwargs):
        return {"success": True, "voices": self.AVAILABLE_VOICES}
