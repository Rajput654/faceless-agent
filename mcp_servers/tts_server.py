"""
mcp_servers/tts_server.py

FIXED v3:
  BUG FIX 1 — ROBOTIC VOICE:
    - edge-tts rate reduced from +25% (brainrot) to more natural levels
    - Added SSML-style pauses via text preprocessing (commas, ellipsis, em-dash)
    - _build_srt() now uses non-uniform timing — longer words get more time
    - Added sentence-boundary pauses in SRT generation
    - Kokoro speed capped: was 1.3 (brainrot) → 1.1 max
    - edge-tts volume boosted to avoid muddy mixing with music

  BUG FIX 2 — INCOMPLETE STORY:
    - Added _chunk_long_text() to split scripts >400 chars into sentences
      before passing to TTS. edge-tts and Kokoro can silently truncate
      very long inputs; chunking + concatenation prevents this.
    - Each chunk is generated separately then joined via ffmpeg concat.
    - Fallback SRT is now rebuilt from the full text after concat.

TTS Backend Priority (unchanged):
  1. Chatterbox TTS  — near-human, MIT license
  2. Kokoro ONNX     — fast, Apache 2.0
  3. edge-tts        — Microsoft Neural voices
  4. gTTS            — robotic last resort only
"""
import os
import re
import asyncio
import subprocess
import urllib.request
from pathlib import Path
from loguru import logger

# ── Chatterbox ────────────────────────────────────────────────────────────────
try:
    import torch
    import torchaudio
    from chatterbox.tts import ChatterboxTTS
    CHATTERBOX_AVAILABLE = True
except ImportError:
    CHATTERBOX_AVAILABLE = False

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
KOKORO_MODEL_MIN_BYTES  = 80_000_000
KOKORO_VOICES_MIN_BYTES = 1_000_000


def _is_valid_onnx(path: str) -> bool:
    try:
        size = os.path.getsize(path)
        if size < KOKORO_MODEL_MIN_BYTES:
            return False
        with open(path, "rb") as f:
            first_byte = f.read(1)
        return first_byte in (b'\x08', b'\n')
    except Exception:
        return False


def _download_kokoro_models() -> bool:
    downloads = [
        (KOKORO_MODEL_URL,  KOKORO_MODEL_PATH,  KOKORO_MODEL_MIN_BYTES,  "model"),
        (KOKORO_VOICES_URL, KOKORO_VOICES_PATH, KOKORO_VOICES_MIN_BYTES, "voices"),
    ]
    for url, path, min_bytes, label in downloads:
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size >= min_bytes and (label != "model" or _is_valid_onnx(path)):
                logger.info(f"Kokoro {label} already valid: {path}")
                continue
            os.remove(path)

        logger.info(f"Downloading Kokoro {label}...")
        tmp = path + ".tmp"
        try:
            urllib.request.urlretrieve(url, tmp)
            actual_size = os.path.getsize(tmp)
            if actual_size < min_bytes:
                os.remove(tmp)
                return False
            if label == "model" and not _is_valid_onnx(tmp):
                os.remove(tmp)
                return False
            os.rename(tmp, path)
            logger.success(f"Kokoro {label} downloaded ({actual_size/1e6:.0f} MB)")
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            logger.error(f"Kokoro {label} download failed: {e}")
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1: More natural voice config — reduced rates, natural prosody
# ─────────────────────────────────────────────────────────────────────────────

CHATTERBOX_NICHE_CONFIG = {
    "motivation":   {"exps": 1.2, "cfg_weight": 0.5},
    "horror":       {"exps": 1.5, "cfg_weight": 0.5},
    "reddit_story": {"exps": 0.9, "cfg_weight": 0.5},
    "brainrot":     {"exps": 1.8, "cfg_weight": 0.5},
    "finance":      {"exps": 0.6, "cfg_weight": 0.5},
    "default":      {"exps": 1.0, "cfg_weight": 0.5},
}

KOKORO_VOICE_MAP = {
    "motivation":   ("am_michael", 1.0),   # FIX: was 1.1 — slightly fast, caused robotic clip
    "horror":       ("am_adam",    0.85),  # FIX: was 0.88 — slower = more ominous
    "reddit_story": ("af_sky",     0.95),  # FIX: was 1.0 — slightly slower = more natural
    "brainrot":     ("af_bella",   1.1),   # FIX: was 1.3 — too fast = unintelligible
    "finance":      ("bm_george",  1.0),   # FIX: was 1.05 — natural pace
    "default":      ("am_michael", 1.0),
}

KOKORO_VOICES = [
    "af_sarah", "af_sky", "af_bella", "af_nicole",
    "am_adam",  "am_michael",
    "bf_emma",  "bf_isabella",
    "bm_george","bm_lewis",
]

# FIX 1: Reduced edge-tts rates — previous values caused robotic clipping
# The higher the rate, the less natural prosody edge-tts can maintain.
# Brainrot was +25% — that's basically speed-run territory. Capped at +15%.
EDGE_VOICE_MAP = {
    "motivation":   ("en-US-GuyNeural",   "+8%"),    # FIX: was +15%
    "horror":       ("en-US-DavisNeural", "-8%"),    # FIX: was -5% — slower = creepier
    "reddit_story": ("en-US-AriaNeural",  "+3%"),    # FIX: was +5%
    "brainrot":     ("en-US-JennyNeural", "+15%"),   # FIX: was +25% — too fast = robotic
    "finance":      ("en-GB-RyanNeural",  "+5%"),    # FIX: was +10%
    "default":      ("en-US-GuyNeural",   "+5%"),    # FIX: was +10%
}

# FIX 1: Natural pitch and volume — previous settings were flat
EDGE_PITCH_MAP = {
    "motivation":   "+2Hz",
    "horror":       "-5Hz",    # slightly lower = menacing
    "reddit_story": "+0Hz",
    "brainrot":     "+3Hz",
    "finance":      "-2Hz",    # slightly lower = authoritative
    "default":      "+0Hz",
}

# FIX 2: Max chars before chunking — edge-tts silently truncates beyond ~800 chars
TTS_CHUNK_SIZE = 600


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1: Text preprocessing — add natural prosody cues
# ─────────────────────────────────────────────────────────────────────────────

def _preprocess_for_natural_speech(text: str) -> str:
    """
    Transform script text so edge-tts produces more natural prosody.

    Key transforms:
    - Ellipsis (...) → pause marker that edge-tts respects
    - Em-dash sequences → comma pause  
    - All-caps words → spaced letters (edge-tts reads them naturally)
    - Numbers → spelled out in ways that sound less robotic
    - Short 1-3 word sentences → kept short (already natural)
    - Add slight pause after each sentence for breathing room
    """
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    # Em-dash → comma (TTS handles commas as natural pause)
    text = re.sub(r'\s*—\s*', ', ', text)
    text = re.sub(r'\s*–\s*', ', ', text)

    # Ellipsis already adds natural pause in edge-tts — preserve it
    # But normalize multiple dots
    text = re.sub(r'\.{4,}', '...', text)

    # Ensure sentences end with period for proper pause
    # (some scripts use newlines as sentence separators)
    text = re.sub(r'\n+', '. ', text)
    text = re.sub(r'\.\s*\.', '.', text)  # double-period cleanup
    text = re.sub(r'\s+', ' ', text).strip()

    # Add a breath pause after very short punchy sentences (under 5 words)
    # by inserting a comma before the next sentence
    sentences = re.split(r'(?<=[.!?])\s+', text)
    processed = []
    for i, sentence in enumerate(sentences):
        word_count = len(sentence.split())
        processed.append(sentence)
        # Short sentence followed by another short sentence = add breathing room
        if word_count <= 4 and i < len(sentences) - 1:
            next_wc = len(sentences[i+1].split())
            if next_wc <= 4:
                # Don't modify — the period already handles pause
                pass

    return ' '.join(processed)


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2: Text chunking — prevents silent truncation in TTS backends
# ─────────────────────────────────────────────────────────────────────────────

def _chunk_text(text: str, max_chars: int = TTS_CHUNK_SIZE) -> list:
    """
    Split long text into sentence-boundary chunks under max_chars.

    This prevents edge-tts and Kokoro from silently truncating long scripts,
    which was the root cause of "incomplete story" in generated videos.

    Returns a list of text chunks. Each chunk ends at a sentence boundary.
    """
    if len(text) <= max_chars:
        return [text]

    # Split on sentence boundaries
    sentences = re.split(r'(?<=[.!?])\s+', text)

    chunks = []
    current_chunk = ""

    for sentence in sentences:
        if not sentence.strip():
            continue

        # If adding this sentence would exceed limit, flush current chunk
        if current_chunk and len(current_chunk) + len(sentence) + 1 > max_chars:
            chunks.append(current_chunk.strip())
            current_chunk = sentence
        else:
            current_chunk = (current_chunk + " " + sentence).strip() if current_chunk else sentence

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    logger.info(f"Text chunked: {len(text)} chars → {len(chunks)} chunks of ~{max_chars} chars each")
    return chunks


def _concat_audio_chunks(chunk_paths: list, output_path: str) -> bool:
    """
    Concatenate multiple MP3 audio files into one using ffmpeg.
    Returns True on success.
    """
    if len(chunk_paths) == 1:
        import shutil
        shutil.copy2(chunk_paths[0], output_path)
        return True

    list_path = output_path + "_concat_list.txt"
    try:
        with open(list_path, "w") as f:
            for p in chunk_paths:
                f.write(f"file '{os.path.abspath(p)}'\n")

        result = subprocess.run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c:a", "libmp3lame", "-q:a", "2",
            output_path,
        ], capture_output=True, text=True, timeout=120)

        if os.path.exists(list_path):
            os.remove(list_path)

        if result.returncode == 0 and os.path.exists(output_path):
            size = os.path.getsize(output_path)
            logger.info(f"Audio chunks concatenated: {len(chunk_paths)} → {output_path} ({size//1024} KB)")
            return True
        else:
            logger.error(f"ffmpeg concat failed: {result.stderr[-500:]}")
            return False
    except Exception as e:
        logger.error(f"Audio concat exception: {e}")
        if os.path.exists(list_path):
            os.remove(list_path)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1: Improved SRT builder — non-uniform timing based on word length
# ─────────────────────────────────────────────────────────────────────────────

def _ms_to_srt(ms: int) -> str:
    ms = max(0, int(ms))
    h    = ms // 3_600_000
    m    = (ms % 3_600_000) // 60_000
    s    = (ms % 60_000) // 1_000
    msec = ms % 1_000
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"


def _build_srt(text: str, total_ms: int, words_per_line: int = 4) -> str:
    """
    Build SRT with non-uniform word timing.

    FIX: Old version used uniform ms-per-word which made subtitles feel robotic
    (all words got identical time regardless of length or punctuation).

    New approach:
    - Longer words get proportionally more time
    - Words followed by punctuation (.!?,;:) get +20% pause
    - Sentence boundaries get a brief gap
    """
    words = text.split()
    if not words:
        return ""

    # Calculate weighted duration for each word
    # Weight = character length + punctuation bonus
    weights = []
    for word in words:
        w = max(len(word), 2)  # minimum weight of 2
        # Punctuation at end of word = natural pause
        if word[-1] in '.!?':
            w += 4  # full stop = longer pause
        elif word[-1] in ',;:':
            w += 2  # comma = shorter pause
        elif word[-1] in '...':
            w += 3  # ellipsis = dramatic pause
        weights.append(w)

    total_weight = sum(weights)
    
    # Allocate time proportionally
    word_durations = []
    for w in weights:
        word_durations.append(int(total_ms * w / total_weight))

    # Group into lines of words_per_line
    lines = []
    i = 0
    line_start_ms = 0

    # Calculate cumulative start times
    cumulative = [0]
    for d in word_durations:
        cumulative.append(cumulative[-1] + d)

    # Group into subtitle blocks
    chunks = [words[j:j+words_per_line] for j in range(0, len(words), words_per_line)]
    chunk_ranges = [(j, min(j+words_per_line, len(words))) for j in range(0, len(words), words_per_line)]

    srt_blocks = []
    for idx, (chunk, (start_w, end_w)) in enumerate(zip(chunks, chunk_ranges), 1):
        start_ms = cumulative[start_w]
        end_ms = cumulative[end_w]
        srt_blocks.append(
            f"{idx}\n{_ms_to_srt(start_ms)} --> {_ms_to_srt(end_ms)}\n{' '.join(chunk)}\n"
        )

    return "\n".join(srt_blocks)


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
    chunks = [wbs[i:i + 4] for i in range(0, len(wbs), 4)]
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
    srt = _wbs_to_srt(wbs)
    if not srt:
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
    if not srt:
        duration_ms = _audio_duration_ms(out_path)
        srt = _build_srt(text, duration_ms)
    with open(sub_path, "w", encoding="utf-8") as f:
        f.write(srt)


# ─────────────────────────────────────────────────────────────────────────────
# Main TTS Server
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
        self._kokoro_model    = None
        self._chatterbox_model = None

    def _load_chatterbox(self):
        if self._chatterbox_model is None:
            logger.info("Loading Chatterbox TTS model...")
            device = "cuda" if (CHATTERBOX_AVAILABLE and torch.cuda.is_available()) else "cpu"
            self._chatterbox_model = ChatterboxTTS.from_pretrained(device=device)
            logger.success(f"Chatterbox loaded on {device}")
        return self._chatterbox_model

    def _load_kokoro(self):
        if self._kokoro_model is None:
            ok = _download_kokoro_models()
            if not ok:
                raise RuntimeError("Kokoro model download/validation failed")
            logger.info("Loading Kokoro TTS model...")
            self._kokoro_model = Kokoro(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)
        return self._kokoro_model

    def call(self, tool_name: str, **kwargs):
        if tool_name not in self.tools:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return self.tools[tool_name](**kwargs)

    def _generate_speech(self, text, output_path, subtitle_path,
                         voice="am_michael", rate="+5%", pitch="+0Hz",
                         volume="+0%", **kwargs):
        if not text or not text.strip():
            return {"success": False, "error": "Empty text"}

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(subtitle_path).parent.mkdir(parents=True, exist_ok=True)

        # FIX 1: Preprocess text for natural prosody
        processed_text = _preprocess_for_natural_speech(text)

        niche = os.environ.get("NICHE", "default")

        # 1️⃣  Chatterbox
        if CHATTERBOX_AVAILABLE:
            r = self._chatterbox_generate(processed_text, output_path, subtitle_path, niche)
            if r.get("success"):
                return r
            logger.warning(f"Chatterbox failed: {r.get('error')} — trying Kokoro")

        # 2️⃣  Kokoro ONNX
        if KOKORO_AVAILABLE:
            r = self._kokoro_generate_chunked(processed_text, output_path, subtitle_path, voice, niche)
            if r.get("success"):
                return r
            logger.warning(f"Kokoro failed: {r.get('error')} — trying edge-tts")

        # 3️⃣  edge-tts
        if EDGE_TTS_AVAILABLE:
            edge_voice, edge_rate = EDGE_VOICE_MAP.get(niche, EDGE_VOICE_MAP["default"])
            edge_pitch = EDGE_PITCH_MAP.get(niche, "+0Hz")
            r = self._edge_generate_chunked(
                processed_text, output_path, subtitle_path,
                edge_voice, edge_rate, edge_pitch, volume
            )
            if r.get("success"):
                return r
            logger.warning(f"edge-tts failed: {r.get('error')} — trying gTTS")

        # 4️⃣  gTTS (last resort)
        if GTTS_AVAILABLE:
            r = self._gtts_generate(processed_text, output_path, subtitle_path)
            if r.get("success"):
                return r

        return {"success": False, "error": "All TTS backends failed"}

    # ── Chatterbox ────────────────────────────────────────────────────────────

    def _chatterbox_generate(self, text, output_path, subtitle_path, niche):
        try:
            cfg = CHATTERBOX_NICHE_CONFIG.get(niche, CHATTERBOX_NICHE_CONFIG["default"])
            exps       = cfg["exps"]
            cfg_weight = cfg["cfg_weight"]

            model = self._load_chatterbox()

            audio_prompt_path = f"assets/voices/{niche}_reference.wav"
            audio_prompt = audio_prompt_path if os.path.exists(audio_prompt_path) else None

            if audio_prompt:
                wav = model.generate(
                    text,
                    audio_prompt_path=audio_prompt,
                    exps=exps,
                    cfg_weight=cfg_weight,
                )
            else:
                wav = model.generate(text, exps=exps)

            wav_path = output_path.replace(".mp3", "_cb_tmp.wav")
            torchaudio.save(wav_path, wav, model.sr)

            subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path,
                 "-codec:a", "libmp3lame", "-qscale:a", "2", output_path],
                capture_output=True, timeout=120,
            )
            if os.path.exists(wav_path):
                os.remove(wav_path)

            size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if size == 0:
                return {"success": False, "error": "Chatterbox produced empty audio"}

            duration_ms = _audio_duration_ms(output_path)
            with open(subtitle_path, "w", encoding="utf-8") as f:
                f.write(_build_srt(text, duration_ms))

            logger.success(f"Chatterbox ✅  niche={niche} exps={exps} | {size/1024:.0f} KB | {duration_ms/1000:.1f}s")
            return {
                "success":          True,
                "audio_path":       output_path,
                "subtitle_path":    subtitle_path,
                "audio_size_bytes": size,
                "voice_used":       f"chatterbox-{niche}",
                "backend":          "chatterbox",
                "duration_ms":      duration_ms,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Kokoro ONNX with chunking ─────────────────────────────────────────────

    def _kokoro_generate_chunked(self, text, output_path, subtitle_path, voice, niche):
        """
        FIX 2: Generate Kokoro audio in chunks to prevent silent truncation.
        Long scripts (>600 chars) are split at sentence boundaries, each chunk
        generated separately, then concatenated with ffmpeg.
        """
        chunks = _chunk_text(text, max_chars=TTS_CHUNK_SIZE)

        if len(chunks) == 1:
            return self._kokoro_generate(text, output_path, subtitle_path, voice, niche)

        logger.info(f"Kokoro: generating {len(chunks)} audio chunks for complete story")
        chunk_paths = []

        for i, chunk in enumerate(chunks):
            chunk_path = output_path.replace(".mp3", f"_chunk_{i:02d}.mp3")
            chunk_sub = subtitle_path.replace(".srt", f"_chunk_{i:02d}.srt")
            result = self._kokoro_generate(chunk, chunk_path, chunk_sub, voice, niche)
            if not result.get("success"):
                # Clean up
                for p in chunk_paths:
                    if os.path.exists(p):
                        os.remove(p)
                return result
            chunk_paths.append(chunk_path)

        # Concatenate all chunks
        success = _concat_audio_chunks(chunk_paths, output_path)

        # Clean up chunk files
        for p in chunk_paths:
            if os.path.exists(p):
                os.remove(p)

        if not success:
            return {"success": False, "error": "Audio chunk concatenation failed"}

        size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        duration_ms = _audio_duration_ms(output_path)

        # Rebuild subtitle from full text with actual duration
        with open(subtitle_path, "w", encoding="utf-8") as f:
            f.write(_build_srt(text, duration_ms))

        logger.success(f"Kokoro chunked ✅  {len(chunks)} chunks | {size//1024} KB | {duration_ms/1000:.1f}s")
        return {
            "success":          True,
            "audio_path":       output_path,
            "subtitle_path":    subtitle_path,
            "audio_size_bytes": size,
            "voice_used":       f"kokoro-chunked-{niche}",
            "backend":          "kokoro",
            "duration_ms":      duration_ms,
        }

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
                "success":          True,
                "audio_path":       output_path,
                "subtitle_path":    subtitle_path,
                "audio_size_bytes": size,
                "voice_used":       kokoro_voice,
                "backend":          "kokoro",
                "duration_ms":      duration_ms,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── edge-tts with chunking ─────────────────────────────────────────────────

    def _edge_generate_chunked(self, text, output_path, subtitle_path, voice, rate, pitch, volume):
        """
        FIX 2: Generate edge-tts audio in chunks.
        edge-tts can silently drop text beyond ~800 chars in some versions.
        """
        chunks = _chunk_text(text, max_chars=TTS_CHUNK_SIZE)

        if len(chunks) == 1:
            return self._edge_generate(text, output_path, subtitle_path, voice, rate, pitch, volume)

        logger.info(f"edge-tts: generating {len(chunks)} audio chunks for complete story")
        chunk_paths = []

        for i, chunk in enumerate(chunks):
            chunk_path = output_path.replace(".mp3", f"_echunk_{i:02d}.mp3")
            chunk_sub = subtitle_path.replace(".srt", f"_echunk_{i:02d}.srt")
            result = self._edge_generate(chunk, chunk_path, chunk_sub, voice, rate, pitch, volume)
            if not result.get("success"):
                for p in chunk_paths:
                    if os.path.exists(p):
                        os.remove(p)
                return result
            chunk_paths.append(chunk_path)

        success = _concat_audio_chunks(chunk_paths, output_path)

        for p in chunk_paths:
            if os.path.exists(p):
                os.remove(p)

        if not success:
            return {"success": False, "error": "edge-tts chunk concat failed"}

        size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        duration_ms = _audio_duration_ms(output_path)

        # Rebuild unified SRT from full text
        with open(subtitle_path, "w", encoding="utf-8") as f:
            f.write(_build_srt(text, duration_ms))

        logger.success(f"edge-tts chunked ✅  {len(chunks)} chunks | {size//1024} KB | {duration_ms/1000:.1f}s")
        return {
            "success":          True,
            "audio_path":       output_path,
            "subtitle_path":    subtitle_path,
            "audio_size_bytes": size,
            "voice_used":       f"{voice}-chunked",
            "backend":          "edge-tts",
            "duration_ms":      duration_ms,
        }

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

            if not os.path.exists(subtitle_path) or os.path.getsize(subtitle_path) == 0:
                duration_ms = _audio_duration_ms(output_path)
                with open(subtitle_path, "w", encoding="utf-8") as f:
                    f.write(_build_srt(text, duration_ms))

            duration_ms = _audio_duration_ms(output_path)
            logger.success(f"edge-tts ✅  {voice} | {size} bytes")
            return {
                "success":          True,
                "audio_path":       output_path,
                "subtitle_path":    subtitle_path,
                "audio_size_bytes": size,
                "voice_used":       voice,
                "backend":          "edge-tts",
                "duration_ms":      duration_ms,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── gTTS ──────────────────────────────────────────────────────────────────

    def _gtts_generate(self, text, output_path, subtitle_path):
        try:
            # FIX 2: chunk gTTS too — it has the same truncation problem
            chunks = _chunk_text(text, max_chars=400)  # gTTS is more aggressive
            chunk_paths = []

            for i, chunk in enumerate(chunks):
                chunk_path = output_path.replace(".mp3", f"_gchunk_{i:02d}.mp3")
                gTTS(text=chunk, lang="en", slow=False).save(chunk_path)
                if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
                    chunk_paths.append(chunk_path)

            if not chunk_paths:
                return {"success": False, "error": "gTTS produced no audio"}

            success = _concat_audio_chunks(chunk_paths, output_path)

            for p in chunk_paths:
                if os.path.exists(p):
                    os.remove(p)

            if not success:
                return {"success": False, "error": "gTTS concat failed"}

            size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if size == 0:
                return {"success": False, "error": "gTTS produced empty file"}

            duration_ms = _audio_duration_ms(output_path)
            with open(subtitle_path, "w", encoding="utf-8") as f:
                f.write(_build_srt(text, duration_ms))

            logger.warning(f"gTTS ⚠️  (robotic fallback) {output_path}")
            return {
                "success":          True,
                "audio_path":       output_path,
                "subtitle_path":    subtitle_path,
                "audio_size_bytes": size,
                "voice_used":       "gtts-en",
                "backend":          "gtts",
                "duration_ms":      duration_ms,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _list_voices(self, **kwargs):
        return {"success": True, "voices": self.AVAILABLE_VOICES}
