"""
mcp_servers/tts_server.py

TTS backend priority (best → fallback):
  1. Kokoro TTS  — Apache 2.0, 82M params, near-human quality.
                   Model files are auto-downloaded on first run.
  2. edge-tts    — Microsoft Neural voices, genuinely human-sounding.
                   Works locally; may be blocked on some GitHub Actions runners.
  3. gTTS        — last resort, robotic but always works.

Install:  pip install kokoro-onnx soundfile gtts mutagen edge-tts
"""
import os
import re
import asyncio
import subprocess
import urllib.request
from pathlib import Path
from loguru import logger

# ── Kokoro TTS ────────────────────────────────────────────────────────────────
try:
    from kokoro_onnx import Kokoro
    import soundfile as sf
    KOKORO_AVAILABLE = True
except ImportError:
    KOKORO_AVAILABLE = False

# ── gTTS ──────────────────────────────────────────────────────────────────────
try:
    from gtts import gTTS
    GTTS_AVAILABLE = True
except ImportError:
    GTTS_AVAILABLE = False

# ── edge-tts ──────────────────────────────────────────────────────────────────
try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    edge_tts = None
    EDGE_TTS_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Kokoro model auto-download URLs
# ─────────────────────────────────────────────────────────────────────────────
KOKORO_MODEL_URL  = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v1.0.onnx"
KOKORO_VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin"
KOKORO_MODEL_PATH  = os.environ.get("KOKORO_MODEL_PATH",  "kokoro-v1.0.onnx")
KOKORO_VOICES_PATH = os.environ.get("KOKORO_VOICES_PATH", "voices.bin")


def _download_kokoro_models() -> bool:
    """Download Kokoro model files if they don't exist. Returns True on success."""
    try:
        for url, path in [(KOKORO_MODEL_URL, KOKORO_MODEL_PATH),
                          (KOKORO_VOICES_URL, KOKORO_VOICES_PATH)]:
            if not os.path.exists(path):
                logger.info(f"Downloading Kokoro model: {path} (~300 MB total, first run only)…")
                tmp = path + ".tmp"
                urllib.request.urlretrieve(url, tmp)
                os.rename(tmp, path)
                logger.success(f"Downloaded: {path}")
        return True
    except Exception as e:
        logger.warning(f"Kokoro model download failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Niche → Kokoro voice map
# ─────────────────────────────────────────────────────────────────────────────
KOKORO_VOICE_MAP = {
    "motivation":   ("am_michael", 1.1),
    "horror":       ("am_adam",    0.88),
    "reddit_story": ("af_sky",     1.0),
    "brainrot":     ("af_bella",   1.3),
    "finance":      ("bm_george",  1.05),
    "default":      ("am_michael", 1.0),
}

KOKORO_VOICES = [
    "af_sarah", "af_sky", "af_bella", "af_nicole",
    "am_adam",  "am_michael",
    "bf_emma",  "bf_isabella",
    "bm_george","bm_lewis",
]

# Niche → edge-tts neural voice map (human-quality Microsoft voices)
EDGE_VOICE_MAP = {
    "motivation":   ("en-US-GuyNeural",    "+15%"),
    "horror":       ("en-US-DavisNeural",  "-5%"),
    "reddit_story": ("en-US-AriaNeural",   "+5%"),
    "brainrot":     ("en-US-JennyNeural",  "+25%"),
    "finance":      ("en-GB-RyanNeural",   "+10%"),
    "default":      ("en-US-GuyNeural",    "+10%"),
}


# ─────────────────────────────────────────────────────────────────────────────
# SRT helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ms_to_srt(ms: int) -> str:
    h    = ms // 3_600_000
    m    = (ms % 3_600_000) // 60_000
    s    = (ms % 60_000) // 1_000
    msec = ms % 1_000
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"


def _build_srt(text: str, total_ms: int, words_per_line: int = 5) -> str:
    """Even-spaced SRT — used when no word timestamps are available."""
    words  = text.split()
    chunks = [words[i:i + words_per_line] for i in range(0, len(words), words_per_line)]
    n      = max(len(chunks), 1)
    mpc    = total_ms // n
    lines  = []
    for i, chunk in enumerate(chunks, 1):
        s = (i - 1) * mpc
        e = min(i * mpc, total_ms)
        lines += [str(i), f"{_ms_to_srt(s)} --> {_ms_to_srt(e)}", " ".join(chunk), ""]
    return "\n".join(lines)


def _audio_duration_ms(path: str) -> int:
    try:
        from mutagen.mp3 import MP3
        return int(MP3(path).info.length * 1000)
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15
        )
        return int(float(r.stdout.strip()) * 1000)
    except Exception:
        pass
    try:
        info = sf.info(path)
        return int(info.duration * 1000)
    except Exception:
        return 30_000


# ─────────────────────────────────────────────────────────────────────────────
# edge-tts async helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_vtt_to_srt(vtt: str) -> str:
    if isinstance(vtt, bytes):
        vtt = vtt.decode("utf-8")
    lines, out, counter, i = vtt.strip().split("\n"), [], 1, 0
    while i < len(lines) and "-->" not in lines[i]:
        i += 1
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            ts = re.sub(r'\s+\S+:\S+', '', line).replace(".", ",", 2)
            out.append(str(counter))
            out.append(ts)
            i += 1
            txt = []
            while i < len(lines) and lines[i].strip():
                txt.append(lines[i].strip())
                i += 1
            out += ["\n".join(txt), ""]
            counter += 1
        else:
            i += 1
    return "\n".join(out)


def _wbs_to_srt(wbs: list) -> str:
    if not wbs:
        return ""
    chunks = [wbs[i:i + 5] for i in range(0, len(wbs), 5)]
    out = []
    for i, c in enumerate(chunks, 1):
        def fmt(ns):
            ms = ns // 10_000
            h, m = ms // 3_600_000, (ms % 3_600_000) // 60_000
            s, msec = (ms % 60_000) // 1_000, ms % 1_000
            return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"
        out += [
            str(i),
            f"{fmt(c[0]['offset'])} --> {fmt(c[-1]['offset'] + c[-1]['duration'])}",
            " ".join(w["text"] for w in c),
            "",
        ]
    return "\n".join(out)


async def _edge_async(text, out_path, sub_path, voice, rate, pitch, volume):
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    wbs  = []
    with open(out_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                wbs.append({
                    "offset":   chunk.get("offset", 0),
                    "duration": chunk.get("duration", 0),
                    "text":     chunk.get("text", ""),
                })
    srt = ""
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
                    srt = _parse_vtt_to_srt(
                        raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                    break
                except Exception:
                    continue
    except Exception:
        pass
    if not srt and wbs:
        srt = _wbs_to_srt(wbs)
    with open(sub_path, "w", encoding="utf-8") as f:
        f.write(srt)


# ─────────────────────────────────────────────────────────────────────────────
# Server
# ─────────────────────────────────────────────────────────────────────────────

class TTSMCPServer:
    """
    TTS MCP Server.
    Priority: Kokoro (near-human, auto-downloaded) → edge-tts (Microsoft Neural) → gTTS (fallback).
    """

    AVAILABLE_VOICES = KOKORO_VOICES + [
        "en-US-GuyNeural", "en-US-AriaNeural", "en-US-JennyNeural",
        "en-US-DavisNeural", "en-GB-RyanNeural", "en-AU-NatashaNeural",
    ]

    def __init__(self):
        self.tools = {
            "generate_speech": self._generate_speech,
            "list_voices":     self._list_voices,
        }
        self._kokoro_model = None

    def _load_kokoro(self):
        if self._kokoro_model is None:
            if not (os.path.exists(KOKORO_MODEL_PATH) and os.path.exists(KOKORO_VOICES_PATH)):
                ok = _download_kokoro_models()
                if not ok:
                    raise RuntimeError("Kokoro model files unavailable and download failed")
            logger.info("Loading Kokoro TTS model…")
            self._kokoro_model = Kokoro(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)
        return self._kokoro_model

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _generate_speech(
        self,
        text:          str,
        output_path:   str,
        subtitle_path: str,
        voice:         str = "am_michael",
        rate:          str = "+10%",
        pitch:         str = "+0Hz",
        volume:        str = "+0%",
        **kwargs,
    ):
        if not text or not text.strip():
            return {"success": False, "error": "Empty text provided"}

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(subtitle_path).parent.mkdir(parents=True, exist_ok=True)

        niche = os.environ.get("NICHE", "default")

        # 1️⃣  Kokoro — near-human quality, CPU-safe, auto-downloads model
        if KOKORO_AVAILABLE:
            result = self._kokoro_generate(text, output_path, subtitle_path, voice, niche)
            if result.get("success"):
                return result
            logger.warning(f"Kokoro failed ({result.get('error')}) — trying edge-tts")

        # 2️⃣  edge-tts — Microsoft Neural voices, genuinely human-sounding
        if EDGE_TTS_AVAILABLE:
            edge_voice, edge_rate = EDGE_VOICE_MAP.get(niche, EDGE_VOICE_MAP["default"])
            # allow caller-supplied voice to override if it's an edge voice
            if voice in [v for v, _ in EDGE_VOICE_MAP.values()]:
                edge_voice = voice
            result = self._edge_generate(
                text, output_path, subtitle_path, edge_voice, edge_rate, pitch, volume)
            if result.get("success"):
                return result
            logger.warning(f"edge-tts failed ({result.get('error')}) — trying gTTS")

        # 3️⃣  gTTS — last resort, robotic but reliable
        if GTTS_AVAILABLE:
            result = self._gtts_generate(text, output_path, subtitle_path)
            if result.get("success"):
                return result

        return {
            "success": False,
            "error": "All TTS backends failed. Run: pip install kokoro-onnx soundfile edge-tts",
        }

    # ── Kokoro backend ────────────────────────────────────────────────────────

    def _kokoro_generate(self, text, output_path, subtitle_path, voice, niche="default"):
        try:
            kokoro_voice, speed = KOKORO_VOICE_MAP.get(niche, KOKORO_VOICE_MAP["default"])
            if voice in KOKORO_VOICES:
                kokoro_voice = voice

            kokoro  = self._load_kokoro()
            samples, sample_rate = kokoro.create(
                text, voice=kokoro_voice, speed=speed, lang="en-us"
            )

            wav_path = output_path.replace(".mp3", "_tmp.wav")
            sf.write(wav_path, samples, sample_rate)

            subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path,
                 "-codec:a", "libmp3lame", "-qscale:a", "2", output_path],
                capture_output=True, timeout=120,
            )
            if os.path.exists(wav_path):
                os.remove(wav_path)

            audio_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if audio_size == 0:
                return {"success": False, "error": "Kokoro produced empty audio"}

            duration_ms = int(len(samples) / sample_rate * 1000)
            with open(subtitle_path, "w", encoding="utf-8") as f:
                f.write(_build_srt(text, duration_ms))

            logger.success(
                f"Kokoro ✅  voice={kokoro_voice} speed={speed} "
                f"| {audio_size} bytes ~{duration_ms // 1000}s → {output_path}"
            )
            return {
                "success":          True,
                "audio_path":       output_path,
                "subtitle_path":    subtitle_path,
                "audio_size_bytes": audio_size,
                "voice_used":       kokoro_voice,
                "backend":          "kokoro",
            }
        except Exception as e:
            logger.warning(f"Kokoro error: {e}")
            return {"success": False, "error": str(e)}

    # ── edge-tts backend ──────────────────────────────────────────────────────

    def _edge_generate(self, text, output_path, subtitle_path, voice, rate, pitch, volume):
        try:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running() or loop.is_closed():
                    raise RuntimeError
                loop.run_until_complete(
                    _edge_async(text, output_path, subtitle_path, voice, rate, pitch, volume))
            except RuntimeError:
                asyncio.run(
                    _edge_async(text, output_path, subtitle_path, voice, rate, pitch, volume))

            audio_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if audio_size == 0:
                return {"success": False, "error": "edge-tts produced empty audio"}

            logger.success(f"edge-tts ✅  voice={voice} | {output_path} ({audio_size} bytes)")
            return {
                "success":          True,
                "audio_path":       output_path,
                "subtitle_path":    subtitle_path,
                "audio_size_bytes": audio_size,
                "voice_used":       voice,
                "backend":          "edge-tts",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── gTTS backend ──────────────────────────────────────────────────────────

    def _gtts_generate(self, text, output_path, subtitle_path):
        try:
            gTTS(text=text, lang="en", slow=False).save(output_path)
            audio_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if audio_size == 0:
                return {"success": False, "error": "gTTS produced empty file"}
            duration_ms = _audio_duration_ms(output_path)
            with open(subtitle_path, "w", encoding="utf-8") as f:
                f.write(_build_srt(text, duration_ms))
            logger.warning(f"gTTS ⚠️  (robotic fallback) {output_path} ({audio_size} bytes)")
            return {
                "success":          True,
                "audio_path":       output_path,
                "subtitle_path":    subtitle_path,
                "audio_size_bytes": audio_size,
                "voice_used":       "gtts-en",
                "backend":          "gtts",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _list_voices(self, **kwargs):
        return {"success": True, "voices": self.AVAILABLE_VOICES}
