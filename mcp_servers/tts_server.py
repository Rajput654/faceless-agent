"""
mcp_servers/tts_server.py

TTS backend priority:
  1. Kokoro ONNX  — near-human, Apache 2.0, auto-downloads with integrity check
  2. edge-tts     — Microsoft Neural voices, genuinely human-sounding
  3. gTTS         — robotic last resort

Fix: Kokoro model download now validates the file is a real ONNX protobuf
before accepting it.  A partial/corrupt download is deleted and retried.
"""
import os
import re
import asyncio
import struct
import subprocess
import urllib.request
from pathlib import Path
from loguru import logger

# ── Kokoro ────────────────────────────────────────────────────────────────────
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
# Kokoro model paths + download
# ─────────────────────────────────────────────────────────────────────────────
KOKORO_MODEL_URL   = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v1.0.onnx"
KOKORO_VOICES_URL  = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin"
KOKORO_MODEL_PATH  = os.environ.get("KOKORO_MODEL_PATH",  "kokoro-v1.0.onnx")
KOKORO_VOICES_PATH = os.environ.get("KOKORO_VOICES_PATH", "voices.bin")

# Minimum expected file sizes (bytes) — rejects obvious partial downloads
KOKORO_MODEL_MIN_BYTES  = 80_000_000   # model is ~300 MB; reject anything < 80 MB
KOKORO_VOICES_MIN_BYTES = 1_000_000    # voices.bin is ~4 MB; reject anything < 1 MB


def _is_valid_onnx(path: str) -> bool:
    """
    Quick sanity-check: real ONNX files start with a protobuf field header.
    This catches partial/corrupt downloads without loading the full model.
    """
    try:
        size = os.path.getsize(path)
        if size < KOKORO_MODEL_MIN_BYTES:
            logger.warning(f"ONNX file too small: {size} bytes (expected ≥ {KOKORO_MODEL_MIN_BYTES})")
            return False
        # Protobuf field 1 (ir_version), wire type 0 → byte 0x08
        with open(path, "rb") as f:
            first_byte = f.read(1)
        if first_byte not in (b'\x08', b'\n'):   # 0x08 or 0x0a are valid ONNX starts
            logger.warning(f"ONNX file has unexpected first byte: {first_byte!r}")
            return False
        return True
    except Exception as e:
        logger.warning(f"ONNX validation error: {e}")
        return False


def _download_kokoro_models() -> bool:
    """Download Kokoro model files, validating each after download."""
    downloads = [
        (KOKORO_MODEL_URL,  KOKORO_MODEL_PATH,  KOKORO_MODEL_MIN_BYTES,  "model"),
        (KOKORO_VOICES_URL, KOKORO_VOICES_PATH, KOKORO_VOICES_MIN_BYTES, "voices"),
    ]
    for url, path, min_bytes, label in downloads:
        # Delete corrupt/partial existing file
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size < min_bytes:
                logger.warning(f"Existing {label} file is too small ({size} bytes) — re-downloading")
                os.remove(path)
            elif label == "model" and not _is_valid_onnx(path):
                logger.warning(f"Existing {label} file is corrupt — re-downloading")
                os.remove(path)
            else:
                logger.info(f"Kokoro {label} already valid: {path}")
                continue

        logger.info(f"Downloading Kokoro {label}: {path} …")
        tmp = path + ".tmp"
        try:
            urllib.request.urlretrieve(url, tmp)
            # Validate before accepting
            actual_size = os.path.getsize(tmp)
            if actual_size < min_bytes:
                os.remove(tmp)
                logger.error(f"Downloaded {label} is too small ({actual_size} bytes) — download may have failed")
                return False
            if label == "model" and not _is_valid_onnx(tmp):
                os.remove(tmp)
                logger.error(f"Downloaded {label} failed ONNX validation — file is corrupt")
                return False
            os.rename(tmp, path)
            logger.success(f"Kokoro {label} downloaded: {path} ({actual_size/1e6:.0f} MB)")
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            logger.error(f"Kokoro {label} download failed: {e}")
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Voice maps
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
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15
        )
        return int(float(r.stdout.strip()) * 1000)
    except Exception:
        return 30_000


# ─────────────────────────────────────────────────────────────────────────────
# edge-tts async helpers
# ─────────────────────────────────────────────────────────────────────────────

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
            " ".join(w["text"] for w in c), "",
        ]
    return "\n".join(out)


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
    # Build SRT from word boundaries
    srt = _wbs_to_srt(wbs)
    if not srt:
        # fallback: try SubMaker
        try:
            sub = edge_tts.SubMaker()
            for wb in wbs:
                try:
                    sub.feed({"type": "WordBoundary", **wb})
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
    # Fallback SRT if still empty
    if not srt:
        duration_ms = _audio_duration_ms(out_path)
        srt = _build_srt(text, duration_ms)
    with open(sub_path, "w", encoding="utf-8") as f:
        f.write(srt)


# ─────────────────────────────────────────────────────────────────────────────
# Server
# ─────────────────────────────────────────────────────────────────────────────

class TTSMCPServer:
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
            ok = _download_kokoro_models()
            if not ok:
                raise RuntimeError("Kokoro model download/validation failed")
            logger.info("Loading Kokoro TTS model…")
            self._kokoro_model = Kokoro(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)
        return self._kokoro_model

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _generate_speech(self, text, output_path, subtitle_path,
                         voice="am_michael", rate="+10%", pitch="+0Hz",
                         volume="+0%", **kwargs):
        if not text or not text.strip():
            return {"success": False, "error": "Empty text"}

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(subtitle_path).parent.mkdir(parents=True, exist_ok=True)

        niche = os.environ.get("NICHE", "default")

        # 1️⃣ Kokoro
        if KOKORO_AVAILABLE:
            r = self._kokoro_generate(text, output_path, subtitle_path, voice, niche)
            if r.get("success"):
                return r
            logger.warning(f"Kokoro failed: {r.get('error')} — trying edge-tts")

        # 2️⃣ edge-tts (Microsoft Neural — human-sounding)
        if EDGE_TTS_AVAILABLE:
            edge_voice, edge_rate = EDGE_VOICE_MAP.get(niche, EDGE_VOICE_MAP["default"])
            r = self._edge_generate(text, output_path, subtitle_path,
                                    edge_voice, edge_rate, pitch, volume)
            if r.get("success"):
                return r
            logger.warning(f"edge-tts failed: {r.get('error')} — trying gTTS")

        # 3️⃣ gTTS (last resort)
        if GTTS_AVAILABLE:
            r = self._gtts_generate(text, output_path, subtitle_path)
            if r.get("success"):
                return r

        return {"success": False, "error": "All TTS backends failed"}

    # ── Kokoro ────────────────────────────────────────────────────────────────

    def _kokoro_generate(self, text, output_path, subtitle_path, voice, niche):
        try:
            kokoro_voice, speed = KOKORO_VOICE_MAP.get(niche, KOKORO_VOICE_MAP["default"])
            if voice in KOKORO_VOICES:
                kokoro_voice = voice

            kokoro = self._load_kokoro()
            samples, sr = kokoro.create(text, voice=kokoro_voice, speed=speed, lang="en-us")

            wav_path = output_path.replace(".mp3", "_tmp.wav")
            sf.write(wav_path, samples, sr)
            subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path,
                 "-codec:a", "libmp3lame", "-qscale:a", "2", output_path],
                capture_output=True, timeout=120,
            )
            if os.path.exists(wav_path):
                os.remove(wav_path)

            size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if size == 0:
                return {"success": False, "error": "Kokoro produced empty audio"}

            duration_ms = int(len(samples) / sr * 1000)
            with open(subtitle_path, "w", encoding="utf-8") as f:
                f.write(_build_srt(text, duration_ms))

            logger.success(f"Kokoro ✅  {kokoro_voice} speed={speed} | {size} bytes")
            return {
                "success": True, "audio_path": output_path,
                "subtitle_path": subtitle_path, "audio_size_bytes": size,
                "voice_used": kokoro_voice, "backend": "kokoro",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── edge-tts ──────────────────────────────────────────────────────────────

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

            size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if size == 0:
                return {"success": False, "error": "edge-tts produced empty audio"}

            # Ensure subtitle file exists even if edge-tts wrote nothing
            if not os.path.exists(subtitle_path) or os.path.getsize(subtitle_path) == 0:
                duration_ms = _audio_duration_ms(output_path)
                with open(subtitle_path, "w", encoding="utf-8") as f:
                    f.write(_build_srt(text, duration_ms))

            logger.success(f"edge-tts ✅  {voice} | {output_path} ({size} bytes)")
            return {
                "success": True, "audio_path": output_path,
                "subtitle_path": subtitle_path, "audio_size_bytes": size,
                "voice_used": voice, "backend": "edge-tts",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── gTTS ──────────────────────────────────────────────────────────────────

    def _gtts_generate(self, text, output_path, subtitle_path):
        try:
            gTTS(text=text, lang="en", slow=False).save(output_path)
            size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if size == 0:
                return {"success": False, "error": "gTTS produced empty file"}
            duration_ms = _audio_duration_ms(output_path)
            with open(subtitle_path, "w", encoding="utf-8") as f:
                f.write(_build_srt(text, duration_ms))
            logger.warning(f"gTTS ⚠️  (robotic fallback) {output_path}")
            return {
                "success": True, "audio_path": output_path,
                "subtitle_path": subtitle_path, "audio_size_bytes": size,
                "voice_used": "gtts-en", "backend": "gtts",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _list_voices(self, **kwargs):
        return {"success": True, "voices": self.AVAILABLE_VOICES}
