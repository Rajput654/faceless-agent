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
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()

    validate_env()
    config = load_config()

    logger.info("FACELESS AGENT - Starting...")
    logger.info(f"Mode: {args.mode} | Niche: {config['video']['niche']}")

    # Import and run appropriate workflow
    from agents.research_scout import ResearchScoutAgent
    from workflows.batch_workflow import BatchWorkflow

    if args.mode == "research":
        scout = ResearchScoutAgent(config)
        topics = scout.run()
        with open(args.topics_file, "w") as f:
            json.dump(topics, f, indent=2)
        logger.success(f"Research complete: {len(topics)} topics")

    elif args.mode == "batch" or args.mode == "daily":
        # Run research first
        scout = ResearchScoutAgent(config)
        topics = scout.run()
        with open(args.topics_file, "w") as f:
            json.dump(topics, f, indent=2)

        # Then batch generate
        workflow = BatchWorkflow(config)
        workflow.run_daily_batch(args.topics_file)

    else:
        logger.info(f"Single video mode: index {args.video_index}")
        # Load topics and run single video
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
            args.video_index
        )
        logger.info(f"Result: {result}")


if __name__ == "__main__":
    main()
