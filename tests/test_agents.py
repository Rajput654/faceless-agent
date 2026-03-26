"""Basic integration tests for Faceless Agent"""
import os
import pytest
import yaml


@pytest.fixture
def config():
    with open("config/config.yaml") as f:
        return yaml.safe_load(f)


def test_config_loads(config):
    """Test config file loads correctly"""
    assert "video" in config
    assert "llm" in config
    assert "voice" in config
    assert config["video"]["niche"] in [
        "motivation", "horror", "reddit_story", "brainrot", "finance"
    ]


def test_ffmpeg_available():
    """Test FFmpeg is installed"""
    import subprocess
    result = subprocess.run(
        ["ffmpeg", "-version"],
        capture_output=True
    )
    assert result.returncode == 0, "FFmpeg not found!"


def test_env_has_groq_key():
    """Test Groq API key is set"""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        pytest.skip("GROQ_API_KEY not set (required in production)")
    assert key.startswith("gsk_"), "Invalid Groq API key format"


def test_voice_generation():
    """Test Edge TTS voice generation"""
    from mcp_servers.tts_server import TTSMCPServer
    tts = TTSMCPServer()

    result = tts.call(
        "generate_speech",
        text="Testing the faceless agent system.",
        output_path="/tmp/test_voice.mp3",
        subtitle_path="/tmp/test_voice.srt",
        voice="en-US-GuyNeural"
    )
    assert result.get("success"), f"TTS failed: {result}"
    assert os.path.exists("/tmp/test_voice.mp3")


def test_mcp_servers_importable():
    """Test all MCP servers can be imported"""
    from mcp_servers import (
        ScraperMCPServer, TTSMCPServer, ImageMCPServer,
        VideoMCPServer, MusicMCPServer, SocialMCPServer,
        AnalyticsMCPServer
    )
    assert True
