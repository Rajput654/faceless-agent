"""
agents/quality_reviewer.py
Reviews the final video for basic quality checks before upload.
"""
import os
import subprocess
from loguru import logger


class QualityReviewerAgent:
    def __init__(self, config):
        self.config = config
        self.quality_threshold = config.get("video", {}).get("quality_threshold", 0.75)

    def _get_video_info(self, video_path: str) -> dict:
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate,duration",
                "-show_entries", "format=duration,size,bit_rate",
                "-of", "json",
                video_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            import json
            data = json.loads(result.stdout)
            streams = data.get("streams", [{}])
            fmt = data.get("format", {})
            stream = streams[0] if streams else {}

            return {
                "width": int(stream.get("width", 0)),
                "height": int(stream.get("height", 0)),
                "duration": float(fmt.get("duration", 0)),
                "size_bytes": int(fmt.get("size", 0)),
                "bit_rate": int(fmt.get("bit_rate", 0)),
            }
        except Exception as e:
            logger.warning(f"Could not get video info: {e}")
            return {}

    def _check_has_audio(self, video_path: str) -> bool:
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return bool(result.stdout.strip())
        except Exception:
            return False

    def run(self, compose_result: dict, script: dict, video_id: str, *args, **kwargs):
        logger.info(f"QualityReviewerAgent reviewing video: {video_id}")

        video_path = compose_result.get("final_video_path")
        if not video_path or not os.path.exists(video_path):
            return {
                "success": False,
                "quality_score": 0.0,
                "passed": False,
                "issues": ["Video file not found"],
            }

        issues = []
        score = 1.0
        file_size = os.path.getsize(video_path)

        # Check 1: File size (must be > 500KB)
        if file_size < 500_000:
            issues.append(f"File too small: {file_size} bytes")
            score -= 0.4

        # Check 2: Video dimensions and duration
        info = self._get_video_info(video_path)
        if info:
            width = info.get("width", 0)
            height = info.get("height", 0)
            duration = info.get("duration", 0)

            if width < 720 or height < 1280:
                issues.append(f"Low resolution: {width}x{height}")
                score -= 0.2

            if duration < 10:
                issues.append(f"Too short: {duration:.1f}s")
                score -= 0.3
            elif duration > 180:
                issues.append(f"Too long: {duration:.1f}s")
                score -= 0.1

        # Check 3: Has audio track
        if not self._check_has_audio(video_path):
            issues.append("No audio track found")
            score -= 0.3

        score = max(0.0, min(1.0, score))
        passed = score >= self.quality_threshold and len([i for i in issues if "too small" in i.lower() or "audio" in i.lower()]) == 0

        if passed:
            logger.success(f"Quality check PASSED: score={score:.2f} for {video_id}")
        else:
            logger.warning(f"Quality check FAILED: score={score:.2f}, issues={issues}")

        return {
            "success": True,
            "quality_score": round(score, 2),
            "passed": passed,
            "issues": issues,
            "video_info": info,
            "file_size_bytes": file_size,
        }
