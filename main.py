#!/usr/bin/env python3
"""
FACELESS AGENT - Main Entry Point
GitHub-Native Edition | 10+ Videos/Day | 100% Free

NICHE is resolved ONCE here and propagated via:
  1. os.environ["NICHE"]        — for agents that read env directly
  2. config["video"]["niche"]   — for agents that read config dict
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
logger.add(
    sys.stdout, level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | <cyan>{message}</cyan>"
)


def load_config():
    with open("config/config.yaml", "r") as f:
        raw = yaml.safe_load(f)
    return raw


def resolve_niche(args_niche: str, config: dict) -> str:
    """
    Single authoritative niche resolution.
    Priority: CLI arg > NICHE env var > config default
    """
    if args_niche and args_niche.strip():
        return args_niche.strip()
    env_niche = os.environ.get("NICHE", "").strip()
    if env_niche:
        return env_niche
    return config.get("video", {}).get("niche", "motivation")


def validate_env():
    if not os.environ.get("GROQ_API_KEY"):
        logger.error("Missing GROQ_API_KEY! Get it free at console.groq.com")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Faceless Agent")
    parser.add_argument("--mode",
        choices=["research", "single", "batch", "daily"],
        default="single")
    parser.add_argument("--niche",
        choices=["motivation", "horror", "reddit_story", "brainrot", "finance"],
        default="",
        help="Content niche (overrides NICHE env var and config)")
    parser.add_argument("--video-id",    default="video_001")
    parser.add_argument("--video-index", type=int, default=0)
    parser.add_argument("--count",       type=int, default=10)
    parser.add_argument("--topics-file", default="topics.json")
    parser.add_argument("--no-upload",   action="store_true",
                        help="Skip YouTube upload")
    args = parser.parse_args()

    should_upload = not args.no_upload

    validate_env()
    config = load_config()

    # ── Resolve niche ONCE and write back to both env + config ───────────────
    niche = resolve_niche(args.niche, config)
    os.environ["NICHE"] = niche
    config["video"]["niche"] = niche
    # ─────────────────────────────────────────────────────────────────────────

    logger.info("FACELESS AGENT — Starting…")
    logger.info(f"Mode: {args.mode} | Niche: {niche} | Upload: {should_upload}")

    from agents.research_scout import ResearchScoutAgent
    from workflows.batch_workflow import BatchWorkflow

    if args.mode == "research":
        scout  = ResearchScoutAgent(config)
        topics = scout.run()
        with open(args.topics_file, "w") as f:
            json.dump(topics, f, indent=2)
        logger.success(f"Research complete: {len(topics)} topics saved to {args.topics_file}")

    elif args.mode in ("batch", "daily"):
        scout  = ResearchScoutAgent(config)
        topics = scout.run()
        with open(args.topics_file, "w") as f:
            json.dump(topics, f, indent=2)

        workflow = BatchWorkflow(config)
        workflow.run_daily_batch(args.topics_file, upload=should_upload)

    else:   # "single"
        logger.info(f"Single video: index={args.video_index} | upload={should_upload}")
        try:
            with open(args.topics_file) as f:
                topics = json.load(f)
        except FileNotFoundError:
            scout  = ResearchScoutAgent(config)
            topics = scout.run()

        from workflows.video_workflow import VideoWorkflow
        workflow = VideoWorkflow(config)
        result   = workflow.run_single_video(
            topics[args.video_index],
            args.video_id,
            args.video_index,
            upload=should_upload,
        )
        logger.info(f"Result: {result}")

        if result.get("outcome") != "success":
            logger.error(
                f"Pipeline FAILED for {args.video_id}: {result.get('error', 'Unknown error')}\n"
                f"Status: {result.get('status')} | Quality score: {result.get('quality_score')}\n"
                f"Issues: {result.get('quality_issues', [])}"
            )
            sys.exit(1)

        logger.success(f"Pipeline SUCCEEDED for {args.video_id}: {result.get('final_video_path')}")


if __name__ == "__main__":
    main()
