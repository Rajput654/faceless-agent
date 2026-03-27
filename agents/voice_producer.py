"""
agents/voice_producer.py
Generates voiceover audio and subtitle files from a script using Edge TTS.
"""
import os
from loguru import logger
from mcp_servers.tts_server import TTSMCPServer


NICHE_VOICES = {
    "motivation": "en-US-GuyNeural",
    "horror": "en-US-DavisNeural",
    "reddit_story": "en-US-AriaNeural",
    "brainrot": "en-US-JennyNeural",
    "finance": "en-GB-RyanNeural",
}

NICHE_RATES = {
    "motivation": "+15%",
    "horror": "-5%",
    "reddit_story": "+5%",
    "brainrot": "+25%",
    "finance": "+10%",
}


class VoiceProducerAgent:
    def __init__(self, config):
        self.config = config
        self.tts_server = TTSMCPServer()
        self.voice_config = config.get("voice", {})

    def run(self, script: dict, video_id: str, output_dir: str = "/tmp", *args, **kwargs):
        logger.info(f"VoiceProducerAgent generating voice for video: {video_id}")

        niche = os.environ.get("NICHE", self.config.get("video", {}).get("niche", "motivation"))

        # Pick voice and rate based on niche, fall back to config defaults
        voice = NICHE_VOICES.get(niche, self.voice_config.get("primary", "en-US-GuyNeural"))
        rate = NICHE_RATES.get(niche, self.voice_config.get("rate", "+10%"))
        pitch = self.voice_config.get("pitch", "0Hz")
        volume = self.voice_config.get("volume", "+0%")

        script_text = script.get("script", "")
        if not script_text:
            logger.error("No script text found in script dict")
            return {"success": False, "error": "Empty script text"}

        audio_path = f"{output_dir}/{video_id}_voice.mp3"
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
            logger.success(f"Voice generated: {audio_path}")
            return {
                "success": True,
                "audio_path": audio_path,
                "subtitle_path": subtitle_path,
                "voice_used": result.get("voice_used", voice),
                "audio_size_bytes": result.get("audio_size_bytes", 0),
            }

        # Retry with fallback voice
        fallback_voice = self.voice_config.get("fallback", "en-US-AriaNeural")
        if fallback_voice != voice:
            logger.warning(f"Primary voice failed, retrying with fallback: {fallback_voice}")
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
                logger.success(f"Voice generated with fallback: {audio_path}")
                return {
                    "success": True,
                    "audio_path": audio_path,
                    "subtitle_path": subtitle_path,
                    "voice_used": fallback_voice,
                    "audio_size_bytes": result.get("audio_size_bytes", 0),
                }

        logger.error(f"Voice generation failed: {result.get('error')}")
        return {"success": False, "error": result.get("error", "TTS generation failed")}
