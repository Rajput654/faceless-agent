"""
agents/quality_reviewer.py
Reviews the final video for quality, duration, and compliance before publishing.
"""
import os
import subprocess
from loguru import logger


class QualityReviewerAgent:
    def __init__(self, config):
        self.config = config
        self.video_config = config.get("video", {})
        self.quality_threshold = self.video_config.get("quality_threshold", 0.75)

    def _get_video_duration(self, video_path: str) -> float:
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return float(result.stdout.strip())
        except Exception:
            return 0.0

    def _get_video_resolution(self, video_path: str):
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
                video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            parts = result.stdout.strip().split(",")
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return 0, 0

    def _check_audio_present(self, video_path: str) -> bool:
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return result.stdout.strip() == "audio"
        except Exception:
            return False

    def run(self, compose_result: dict, script: dict, video_id: str, *args, **kwargs):
        logger.info(f"QualityReviewerAgent reviewing video: {video_id}")

        issues = []
        score = 1.0

        video_path = compose_result.get("final_video_path")

        # Check 1: Video file exists
        if not video_path or not os.path.exists(video_path):
            return {
                "passed": False,
                "quality_score": 0.0,
                "issues": ["Video file not found"],
            }

        # Check 2: File size > 0
        file_size = os.path.getsize(video_path)
        if file_size < 10_000:
            issues.append(f"Video file too small: {file_size} bytes")
            score -= 0.5

        # Check 3: Duration check (15–90 seconds acceptable)
        duration = self._get_video_duration(video_path)
        target_duration = self.video_config.get("duration_seconds", 55)
        if duration < 10:
            issues.append(f"Video too short: {duration:.1f}s")
            score -= 0.4
        elif duration > 90:
            issues.append(f"Video too long: {duration:.1f}s (max 90s)")
            score -= 0.2
        elif abs(duration - target_duration) > 15:
            issues.append(f"Duration {duration:.1f}s deviates from target {target_duration}s")
            score -= 0.1

        # Check 4: Resolution check (expect 1080x1920)
        width, height = self._get_video_resolution(video_path)
        if width == 0 or height == 0:
            issues.append("Could not determine video resolution")
            score -= 0.2
        elif width < 720 or height < 1280:
            issues.append(f"Resolution too low: {width}x{height}")
            score -= 0.2

        # Check 5: Audio present
        if not self._check_audio_present(video_path):
            issues.append("No audio track found in video")
            score -= 0.4

        # Check 6: Script sanity
        script_text = script.get("script", "")
        word_count = len(script_text.split()) if script_text else 0
        if word_count < 20:
            issues.append(f"Script too short: {word_count} words")
            score -= 0.2

        # Check 7: Title present
        if not script.get("title"):
            issues.append("Missing video title")
            score -= 0.1

        score = max(0.0, round(score, 2))
        passed = score >= self.quality_threshold and not any(
            "too short" in i or "not found" in i or "No audio" in i for i in issues
        )

        if passed:
            logger.success(f"Quality review PASSED: {video_id} | Score: {score} | Duration: {duration:.1f}s")
        else:
            logger.warning(f"Quality review FAILED: {video_id} | Score: {score} | Issues: {issues}")

        return {
            "passed": passed,
            "quality_score": score,
            "issues": issues,
            "duration_seconds": duration,
            "resolution": f"{width}x{height}",
            "file_size_bytes": file_size,
            "word_count": word_count,
        }
