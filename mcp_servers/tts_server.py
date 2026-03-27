"""
mcp_servers/tts_server.py

TTS backend priority:
  1. gTTS  (Google TTS via HTTP — works from ANY IP including GitHub Actions)
  2. edge-tts (Microsoft — blocked on Azure/GH Actions IPs, fine locally)

gTTS does not provide word-level timestamps, so we synthesise a simple
evenly-spaced SRT from the script text. It keeps the rest of the pipeline
(captions, FFmpeg burn-in) working unchanged.
"""
import os
import re
import asyncio
from pathlib import Path
from loguru import logger

# ── gTTS ──────────────────────────────────────────────────────────────────────
try:
    from gtts import gTTS
    GTTS_AVAILABLE = True
except ImportError:
    GTTS_AVAILABLE = False

# ── edge-tts ──────────────────────────────────────────────────────────────────
try:
    import edge_tts
except ImportError:
    edge_tts = None


# ─────────────────────────────────────────────────────────────────────────────
# SRT helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ms_to_srt_ts(ms: int) -> str:
    h    = ms // 3_600_000
    m    = (ms % 3_600_000) // 60_000
    s    = (ms % 60_000) // 1_000
    msec = ms % 1_000
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"


def _build_srt_from_text(text: str, total_duration_ms: int, words_per_line: int = 5) -> str:
    """Evenly-spaced SRT — used when no word timestamps are available (gTTS)."""
    words = text.split()
    if not words:
        return ""
    chunks = [words[i:i + words_per_line] for i in range(0, len(words), words_per_line)]
    n = len(chunks)
    ms_per_chunk = total_duration_ms // n if n else total_duration_ms
    srt_lines = []
    for idx, chunk in enumerate(chunks, start=1):
        start_ms = (idx - 1) * ms_per_chunk
        end_ms   = min(idx * ms_per_chunk, total_duration_ms)
        srt_lines.append(str(idx))
        srt_lines.append(f"{_ms_to_srt_ts(start_ms)} --> {_ms_to_srt_ts(end_ms)}")
        srt_lines.append(" ".join(chunk))
        srt_lines.append("")
    return "\n".join(srt_lines)


def _get_mp3_duration_ms(mp3_path: str) -> int:
    try:
        from mutagen.mp3 import MP3
        return int(MP3(mp3_path).info.length * 1000)
    except Exception:
        pass
    try:
        # Fallback: 128 kbps heuristic
        return int(os.path.getsize(mp3_path) / 16_000 * 1000)
    except Exception:
        return 30_000


# ─────────────────────────────────────────────────────────────────────────────
# edge-tts async helper
# ─────────────────────────────────────────────────────────────────────────────

def _parse_vtt_to_srt(vtt_content: str) -> str:
    if isinstance(vtt_content, bytes):
        vtt_content = vtt_content.decode("utf-8")
    lines = vtt_content.strip().split("\n")
    srt_lines, counter, i = [], 1, 0
    while i < len(lines) and "-->" not in lines[i]:
        i += 1
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            ts = re.sub(r'\s+align:\S+|\s+position:\S+|\s+line:\S+|\s+size:\S+', '', line)
            ts = ts.replace(".", ",", 2)
            srt_lines.append(str(counter))
            srt_lines.append(ts)
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i].strip())
                i += 1
            srt_lines += ["\n".join(text_lines), ""]
            counter += 1
        else:
            i += 1
    return "\n".join(srt_lines)


def _word_boundary_to_srt(wbs: list) -> str:
    if not wbs:
        return ""
    chunk_size = 5
    chunks = [wbs[i:i + chunk_size] for i in range(0, len(wbs), chunk_size)]
    srt_lines = []
    for idx, chunk in enumerate(chunks, start=1):
        def fmt(ns):
            ms = ns // 10_000
            h, m = ms // 3_600_000, (ms % 3_600_000) // 60_000
            s, msec = (ms % 60_000) // 1_000, ms % 1_000
            return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"
        srt_lines += [
            str(idx),
            f"{fmt(chunk[0]['offset'])} --> {fmt(chunk[-1]['offset'] + chunk[-1]['duration'])}",
            " ".join(w["text"] for w in chunk),
            "",
        ]
    return "\n".join(srt_lines)


async def _edge_tts_async(text, output_path, subtitle_path, voice, rate, pitch, volume):
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    wbs = []
    with open(output_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                wbs.append({
                    "offset":   chunk.get("offset", 0),
                    "duration": chunk.get("duration", 0),
                    "text":     chunk.get("text", ""),
                })

    srt_content = ""
    try:
        sub = edge_tts.SubMaker()
        for wb in wbs:
            try:
                sub.feed({"type": "WordBoundary", **wb})
            except Exception:
                try:
                    sub.create_sub((wb["offset"], wb["duration"]), wb["text"])
                except Exception:
                    pass
        for fn_name in ("get_subs", "generate_subs"):
            fn = getattr(sub, fn_name, None)
            if fn:
                try:
                    raw = fn()
                    srt_content = _parse_vtt_to_srt(
                        raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace"))
                    break
                except Exception:
                    continue
    except Exception:
        pass

    if not srt_content and wbs:
        srt_content = _word_boundary_to_srt(wbs)

    with open(subtitle_path, "w", encoding="utf-8") as f:
        f.write(srt_content)


# ─────────────────────────────────────────────────────────────────────────────
# Server
# ─────────────────────────────────────────────────────────────────────────────

class TTSMCPServer:
    """
    TTS MCP Server — tries gTTS first (works on GitHub Actions),
    falls back to edge-tts (works locally).
    """

    AVAILABLE_VOICES = [
        "en-US-GuyNeural", "en-US-AriaNeural", "en-US-JennyNeural",
        "en-US-DavisNeural", "en-GB-RyanNeural", "en-AU-NatashaNeural",
    ]

    def __init__(self):
        self.tools = {
            "generate_speech": self._generate_speech,
            "list_voices":     self._list_voices,
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
        voice:  str = "en-US-GuyNeural",
        rate:   str = "+10%",
        pitch:  str = "+0Hz",
        volume: str = "+0%",
        **kwargs,
    ):
        if not text or not text.strip():
            return {"success": False, "error": "Empty text provided"}

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(subtitle_path).parent.mkdir(parents=True, exist_ok=True)

        # 1️⃣  gTTS — reliable from cloud runners
        if GTTS_AVAILABLE:
            result = self._gtts_generate(text, output_path, subtitle_path)
            if result.get("success"):
                return result
            logger.warning(f"gTTS failed ({result.get('error')}) — trying edge-tts")

        # 2️⃣  edge-tts — fallback (blocked on Azure/GH IPs)
        if edge_tts:
            result = self._edge_tts_generate(
                text, output_path, subtitle_path, voice, rate, pitch, volume)
            if result.get("success"):
                return result
            logger.error(f"edge-tts also failed: {result.get('error')}")

        return {
            "success": False,
            "error": "All TTS backends failed. Ensure gtts is installed: pip install gtts",
        }

    # ── gTTS ──────────────────────────────────────────────────────────────────

    def _gtts_generate(self, text: str, output_path: str, subtitle_path: str) -> dict:
        try:
            gTTS(text=text, lang="en", slow=False).save(output_path)

            audio_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if audio_size == 0:
                return {"success": False, "error": "gTTS produced empty audio file"}

            duration_ms = _get_mp3_duration_ms(output_path)
            with open(subtitle_path, "w", encoding="utf-8") as f:
                f.write(_build_srt_from_text(text, duration_ms))

            logger.success(
                f"gTTS ✅ {output_path} ({audio_size} bytes, ~{duration_ms // 1000}s)")
            return {
                "success": True,
                "audio_path":       output_path,
                "subtitle_path":    subtitle_path,
                "audio_size_bytes": audio_size,
                "voice_used":       "gtts-en",
                "backend":          "gtts",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── edge-tts ──────────────────────────────────────────────────────────────

    def _edge_tts_generate(
        self, text, output_path, subtitle_path, voice, rate, pitch, volume
    ) -> dict:
        try:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running() or loop.is_closed():
                    raise RuntimeError
                loop.run_until_complete(
                    _edge_tts_async(text, output_path, subtitle_path,
                                    voice, rate, pitch, volume))
            except RuntimeError:
                asyncio.run(
                    _edge_tts_async(text, output_path, subtitle_path,
                                    voice, rate, pitch, volume))

            audio_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if audio_size == 0:
                return {"success": False, "error": "edge-tts produced empty audio"}

            logger.success(f"edge-tts ✅ {output_path} ({audio_size} bytes)")
            return {
                "success": True,
                "audio_path":       output_path,
                "subtitle_path":    subtitle_path,
                "audio_size_bytes": audio_size,
                "voice_used":       voice,
                "backend":          "edge-tts",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _list_voices(self, **kwargs):
        return {"success": True, "voices": self.AVAILABLE_VOICES}
