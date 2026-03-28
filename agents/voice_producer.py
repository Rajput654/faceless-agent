"""
agents/voice_producer.py

Generates voiceover audio and subtitle files from a script.
Niche is resolved ONCE here and passed through explicitly — no env
var race conditions across agents.
"""
import os
import re
from loguru import logger
from mcp_servers.tts_server import TTSMCPServer


NICHE_VOICES = {
    "motivation":   "en-US-GuyNeural",
    "horror":       "en-US-DavisNeural",
    "reddit_story": "en-US-AriaNeural",
    "brainrot":     "en-US-JennyNeural",
    "finance":      "en-GB-RyanNeural",
}

NICHE_RATES = {
    "motivation":   "+15%",
    "horror":       "-5%",
    "reddit_story": "+5%",
    "brainrot":     "+25%",
    "finance":      "+10%",
}


def _fix_sign(value: str, unit: str = "") -> str:
    """Ensure value has a leading + or - sign (required by edge-tts)."""
    value = value.strip()
    if value and value[0] not in ("+", "-"):
        value = "+" + value
    return value


class VoiceProducerAgent:
    def __init__(self, config):
        self.config       = config
        self.tts_server   = TTSMCPServer()
        self.voice_config = config.get("voice", {})

    def _get_niche(self) -> str:
        return os.environ.get(
            "NICHE",
            self.config.get("video", {}).get("niche", "motivation")
        )

    def run(self, script: dict, video_id: str, output_dir: str = "/tmp", *args, **kwargs):
        niche = self._get_niche()
        logger.info(f"VoiceProducerAgent → niche={niche} video={video_id}")

        voice  = NICHE_VOICES.get(niche, self.voice_config.get("primary", "en-US-GuyNeural"))
        rate   = _fix_sign(NICHE_RATES.get(niche, self.voice_config.get("rate",   "+10%")))
        pitch  = _fix_sign(self.voice_config.get("pitch",  "+0Hz"))
        volume = _fix_sign(self.voice_config.get("volume", "+0%"))

        logger.debug(f"TTS → voice={voice} rate={rate} pitch={pitch} volume={volume}")

        script_text = script.get("script", "")
        if not script_text:
            logger.error("Empty script text in voice producer")
            return {"success": False, "error": "Empty script text"}

        audio_path    = f"{output_dir}/{video_id}_voice.mp3"
        subtitle_path = f"{output_dir}/{video_id}_subtitles.srt"

        result = self.tts_server.call(
            "generate_speech",
            text=script_text,
            output_path=audio_path,
            subtitle_path=subtitle_path,
            voice=voice,
            rate=rate,
            pitch=pitch,
            volume=volume,
        )

        if result.get("success"):
            backend = result.get("backend", "unknown")
            logger.success(f"Voice generated via {backend}: {audio_path}")
            return {
                "success":          True,
                "audio_path":       audio_path,
                "subtitle_path":    subtitle_path,
                "voice_used":       result.get("voice_used", voice),
                "audio_size_bytes": result.get("audio_size_bytes", 0),
                "backend":          backend,
            }

        # Retry with fallback voice
        fallback_voice = self.voice_config.get("fallback", "en-US-AriaNeural")
        if fallback_voice != voice:
            logger.warning(f"Primary voice failed — retrying with {fallback_voice}")
            result = self.tts_server.call(
                "generate_speech",
                text=script_text,
                output_path=audio_path,
                subtitle_path=subtitle_path,
                voice=fallback_voice,
                rate=rate,
                pitch=pitch,
                volume=volume,
            )
            if result.get("success"):
                return {
                    "success":          True,
                    "audio_path":       audio_path,
                    "subtitle_path":    subtitle_path,
                    "voice_used":       fallback_voice,
                    "audio_size_bytes": result.get("audio_size_bytes", 0),
                    "backend":          result.get("backend", "unknown"),
                }

        logger.error(f"Voice generation failed: {result.get('error')}")
        return {"success": False, "error": result.get("error", "TTS generation failed")}
