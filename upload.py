#!/usr/bin/env python3
"""Standalone YouTube upload script"""
import os, sys, json, argparse, yaml
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-index", type=int, required=True)
    parser.add_argument("--topics-file", default="topics.json")
    args = parser.parse_args()

    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)

    with open(args.topics_file) as f:
        topics = json.load(f)

    video_id = f"video_{args.video_index:03d}"
    video_path = f"/tmp/{video_id}_final.mp4"

    if not os.path.exists(video_path):
        logger.error(f"Video not found: {video_path}")
        sys.exit(1)

    topic = topics[args.video_index] if args.video_index < len(topics) else {}
    script = {
        "video_id": video_id,
        "title": topic.get("topic", "Amazing Short"),
        "description": f"{topic.get('topic', '')} #Shorts",
        "tags": ["shorts", "youtube", "viral"],
        "emotion": topic.get("emotion", "inspiration"),
        "topic_brief": topic,
    }

    from agents.social_publisher import SocialPublisherAgent
    publisher = SocialPublisherAgent(config)
    result = publisher.run(video_path, script, args.video_index)

    if result.get("status") == "published":
        logger.success(f"Uploaded: {result.get('youtube_url')}")
        sys.exit(0)
    else:
        logger.error(f"Upload failed: {result}")
        sys.exit(1)

if __name__ == "__main__":
    main()
