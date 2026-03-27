"""
agents/voice_producer.py
Converts script text to MP3 audio + SRT subtitles using edge-tts.
"""
import os
from loguru import logger
from mcp_servers.tts_server import TTSMCPServer


class VoiceProducerAgent:
    def __init__(self, config):
        self.config = config
        self.tts = TTSMCPServer()
        self.voice_config = config.get("voice", {})

    def run(self, script: dict, video_id: str, output_dir: str = "/tmp", *args, **kwargs):
        logger.info(f"VoiceProducerAgent generating audio for video: {video_id}")

        script_text = script.get("script", "")
        if not script_text:
            return {"success": False, "error": "No script text provided"}

        audio_path = f"{output_dir}/{video_id}_voice.mp3"
        subtitle_path = f"{output_dir}/{video_id}_subtitles.srt"

        primary_voice = self.voice_config.get("primary", "en-US-GuyNeural")
        fallback_voice = self.voice_config.get("fallback", "en-US-AriaNeural")
        rate = self.voice_config.get("rate", "+10%")
        pitch = self.voice_config.get("pitch", "0Hz")
        volume = self.voice_config.get("volume", "+0%")

        # Try primary voice
        result = self.tts.call(
            "generate_speech",
            text=script_text,
            output_path=audio_path,
            subtitle_path=subtitle_path,
            voice=primary_voice,
            rate=rate,
            pitch=pitch,
            volume=volume,
        )

        if not result.get("success"):
            logger.warning(f"Primary voice failed: {result.get('error')}. Trying fallback voice.")
            result = self.tts.call(
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
            logger.success(f"Audio generated: {audio_path}")
            return {
                "success": True,
                "audio_path": audio_path,
                "subtitle_path": subtitle_path,
                "voice_used": result.get("voice_used", primary_voice),
                "audio_size_bytes": result.get("audio_size_bytes", 0),
            }
        else:
            logger.error(f"Voice production failed: {result.get('error')}")
            return {"success": False, "error": result.get("error", "TTS failed")}
