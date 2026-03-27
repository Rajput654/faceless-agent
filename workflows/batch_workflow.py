"""
workflows/batch_workflow.py
Runs the full daily batch: research → generate N videos → optionally upload.
"""
import json
import time
from pathlib import Path
from loguru import logger
from workflows.video_workflow import VideoWorkflow
from mcp_servers.analytics_server import AnalyticsMCPServer


class BatchWorkflow:
    def __init__(self, config):
        self.config = config
        self.analytics = AnalyticsMCPServer()
        self.video_workflow = VideoWorkflow(config)

    def run_daily_batch(self, topics_path: str = "topics.json", mode: str = "sequential"):
        logger.info("=== BatchWorkflow starting daily batch ===")

        try:
            with open(topics_path) as f:
                topics = json.load(f)
        except FileNotFoundError:
            logger.error(f"Topics file not found: {topics_path}")
            return {"success": False, "error": "Topics file not found"}

        daily_count = self.config.get("video", {}).get("daily_count", 10)
        topics = topics[:daily_count]

        logger.info(f"Processing {len(topics)} videos...")

        results = []
        passed = 0
        failed = 0

        for i, topic in enumerate(topics):
            video_id = f"video_{i:03d}"
            logger.info(f"\n--- Video {i+1}/{len(topics)}: {topic.get('topic', 'Unknown')} ---")

            try:
                result = self.video_workflow.run_single_video(topic, video_id, i, upload=False)
                results.append(result)

                if result.get("outcome") == "success":
                    passed += 1
                    logger.success(f"✅ Video {i+1} passed: {result.get('final_video_path')}")

                    # Save to analytics
                    self.analytics.call(
                        "save_video",
                        video_id=video_id,
                        title=result.get("title", ""),
                        topic=result.get("topic", ""),
                        niche=self.config.get("video", {}).get("niche", "motivation"),
                        quality_score=result.get("quality_score", 0.0),
                        status="ready",
                    )
                else:
                    failed += 1
                    logger.warning(f"❌ Video {i+1} failed: {result.get('error', 'Unknown error')}")

            except Exception as e:
                failed += 1
                logger.error(f"❌ Video {i+1} crashed: {e}")
                results.append({"video_id": video_id, "outcome": "failed", "error": str(e)})

            # Small delay between videos to avoid rate limits
            if i < len(topics) - 1:
                time.sleep(2)

        # Save batch summary
        niche = self.config.get("video", {}).get("niche", "motivation")
        self.analytics.call("save_batch_run", total=len(topics), passed=passed, failed=failed, niche=niche)

        summary = {
            "success": True,
            "total": len(topics),
            "passed": passed,
            "failed": failed,
            "pass_rate": f"{passed/len(topics)*100:.1f}%" if topics else "0%",
            "results": results,
        }

        logger.info(f"\n=== Batch complete: {passed}/{len(topics)} passed ({summary['pass_rate']}) ===")
        return summary
