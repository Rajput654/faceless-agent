#!/usr/bin/env python3
"""
FACELESS AGENT - Main Entry Point
GitHub-Native Edition | 10+ Videos/Day | 100% Free
"""
import os
import sys
import json
import argparse
import yaml
from pathlib import Path
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

logger.remove()
logger.add(sys.stdout, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | "
                  "<level>{level}</level> | <cyan>{message}</cyan>")


def load_config():
    with open("config/config.yaml", "r") as f:
        return yaml.safe_load(f)


def validate_env():
    if not os.environ.get("GROQ_API_KEY"):
        logger.error("Missing GROQ_API_KEY! Get it free at console.groq.com")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Faceless Agent")
    parser.add_argument("--mode",
        choices=["research", "single", "batch", "daily"],
        default="single")
    parser.add_argument("--video-id", default="video_001")
    parser.add_argument("--video-index", type=int, default=0)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--topics-file", default="topics.json")
    parser.add_argument("--no-upload", action="store_true",
                        help="Skip YouTube upload (default: upload is ON)")
    args = parser.parse_args()

    should_upload = not args.no_upload

    validate_env()
    config = load_config()

    logger.info("FACELESS AGENT - Starting...")
    logger.info(f"Mode: {args.mode} | Niche: {config['video']['niche']} | Upload: {should_upload}")

    from agents.research_scout import ResearchScoutAgent
    from workflows.batch_workflow import BatchWorkflow

    if args.mode == "research":
        scout = ResearchScoutAgent(config)
        topics = scout.run()
        with open(args.topics_file, "w") as f:
            json.dump(topics, f, indent=2)
        logger.success(f"Research complete: {len(topics)} topics")

    elif args.mode == "batch" or args.mode == "daily":
        scout = ResearchScoutAgent(config)
        topics = scout.run()
        with open(args.topics_file, "w") as f:
            json.dump(topics, f, indent=2)

        workflow = BatchWorkflow(config)
        workflow.run_daily_batch(args.topics_file, upload=should_upload)

    else:
        logger.info(f"Single video mode: index {args.video_index} | upload={should_upload}")
        try:
            with open(args.topics_file) as f:
                topics = json.load(f)
        except FileNotFoundError:
            scout = ResearchScoutAgent(config)
            topics = scout.run()

        from workflows.video_workflow import VideoWorkflow
        workflow = VideoWorkflow(config)
        result = workflow.run_single_video(
            topics[args.video_index],
            args.video_id,
            args.video_index,
            upload=should_upload,
        )
        logger.info(f"Result: {result}")

        # FIX: exit with code 1 so GitHub Actions marks the step as FAILED
        # and the error is visible in the logs instead of silently passing
        if result.get("outcome") != "success":
            logger.error(
                f"Pipeline FAILED for {args.video_id}: {result.get('error', 'Unknown error')}\n"
                f"Status: {result.get('status')} | Quality score: {result.get('quality_score')}\n"
                f"Issues: {result.get('quality_issues', [])}"
            )
            sys.exit(1)

        logger.success(
            f"Pipeline SUCCEEDED for {args.video_id}: {result.get('final_video_path')}"
        )


if __name__ == "__main__":
    main()
