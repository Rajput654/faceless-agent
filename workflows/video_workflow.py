"""
workflows/video_workflow.py
Full video production pipeline: Research → Script → Voice → Visuals → Music → Captions → Compose → Review → Publish
"""
import os
from loguru import logger
from pydantic import BaseModel
from typing import Dict, List, Optional, Literal


class VideoState(BaseModel):
    video_id: str = ""
    video_index: int = 0
    topic_brief: Dict = {}
    script: Dict = {}
    voice_result: Dict = {}
    visual_paths: List[str] = []
    music_path: Optional[str] = None
    caption_result: Dict = {}
    final_video_path: Optional[str] = None
    quality_result: Dict = {}
    quality_score: float = 0.0
    publish_result: Dict = {}
    retry_count: int = 0
    max_retries: int = 3
    status: Literal[
        "pending", "scripting", "voice", "visuals",
        "composing", "reviewing", "publishing",
        "passed", "revise", "reject", "failed"
    ] = "pending"
    error_message: Optional[str] = None
    output_dir: str = "/tmp"

    class Config:
        arbitrary_types_allowed = True


class VideoWorkflow:
    def __init__(self, config):
        self.config = config
        self.output_dir = "/tmp"
        self._init_agents()

    def _init_agents(self):
        from agents.script_writer import ScriptWriterAgent
        from agents.voice_producer import VoiceProducerAgent
        from agents.visual_director import VisualDirectorAgent
        from agents.music_director import MusicDirectorAgent
        from agents.caption_maker import CaptionMakerAgent
        from agents.video_composer import VideoComposerAgent
        from agents.quality_reviewer import QualityReviewerAgent
        from agents.social_publisher import SocialPublisherAgent

        self.script_writer = ScriptWriterAgent(self.config)
        self.voice_producer = VoiceProducerAgent(self.config)
        self.visual_director = VisualDirectorAgent(self.config)
        self.music_director = MusicDirectorAgent(self.config)
        self.caption_maker = CaptionMakerAgent(self.config)
        self.video_composer = VideoComposerAgent(self.config)
        self.quality_reviewer = QualityReviewerAgent(self.config)
        self.social_publisher = SocialPublisherAgent(self.config)

    def run_single_video(self, topic_brief: dict, video_id: str, video_index: int = 0, upload: bool = False):
        logger.info(f"=== VideoWorkflow starting: {video_id} ===")
        logger.info(f"Topic: {topic_brief.get('topic', 'Unknown')}")

        state = VideoState(
            video_id=video_id,
            video_index=video_index,
            topic_brief=topic_brief,
            output_dir=self.output_dir,
            max_retries=self.config.get("video", {}).get("max_retries", 3),
        )

        try:
            # Step 1: Write Script
            state.status = "scripting"
            logger.info("Step 1/7: Writing script...")
            state.script = self.script_writer.run(topic_brief, video_id)
            if not state.script:
                raise RuntimeError("Script writer returned empty result")

            # Step 2: Generate Voice
            state.status = "voice"
            logger.info("Step 2/7: Generating voice...")
            state.voice_result = self.voice_producer.run(state.script, video_id, self.output_dir)
            if not state.voice_result.get("success"):
                raise RuntimeError(f"Voice producer failed: {state.voice_result.get('error')}")

            # Step 3: Fetch Visuals (parallel with voice is ideal, sequential here)
            logger.info("Step 3/7: Fetching visuals...")
            visual_result = self.visual_director.run(state.script, video_id, self.output_dir)
            state.visual_paths = visual_result.get("image_paths", [])

            # Step 4: Fetch Music
            logger.info("Step 4/7: Fetching music...")
            music_result = self.music_director.run(state.script, video_id, self.output_dir)
            state.music_path = music_result.get("music_path")

            # Step 5: Process Captions
            logger.info("Step 5/7: Processing captions...")
            state.caption_result = self.caption_maker.run(state.voice_result, video_id, self.output_dir)

            # Step 6: Compose Video
            state.status = "composing"
            logger.info("Step 6/7: Composing video...")
            compose_result = self.video_composer.run(
                script=state.script,
                voice_result=state.voice_result,
                visual_result={"image_paths": state.visual_paths},
                caption_result=state.caption_result,
                music_result=music_result,
                video_id=video_id,
                output_dir=self.output_dir,
            )
            state.final_video_path = compose_result.get("final_video_path")

            if not compose_result.get("success"):
                raise RuntimeError(f"Video composition failed: {compose_result.get('error')}")

            # Step 7: Quality Review
            state.status = "reviewing"
            logger.info("Step 7/7: Quality review...")
            state.quality_result = self.quality_reviewer.run(compose_result, state.script, video_id)
            state.quality_score = state.quality_result.get("quality_score", 0.0)

            if not state.quality_result.get("passed"):
                logger.warning(f"Quality check failed: {state.quality_result.get('issues')}")
                state.status = "reject"
                return self._build_result(state, "failed")

            state.status = "passed"

            # Optional: Upload to YouTube
            if upload and state.final_video_path:
                state.status = "publishing"
                logger.info("Uploading to YouTube...")
                state.publish_result = self.social_publisher.run(
                    state.final_video_path, state.script, video_index
                )

            logger.success(f"=== Video pipeline complete: {video_id} | Score: {state.quality_score} ===")
            return self._build_result(state, "success")

        except Exception as e:
            logger.error(f"Video pipeline failed for {video_id}: {e}")
            state.status = "failed"
            state.error_message = str(e)
            return self._build_result(state, "failed")

    def _build_result(self, state: VideoState, outcome: str) -> dict:
        return {
            "video_id": state.video_id,
            "outcome": outcome,
            "status": state.status,
            "final_video_path": state.final_video_path,
            "quality_score": state.quality_score,
            "quality_issues": state.quality_result.get("issues", []),
            "publish_result": state.publish_result,
            "error": state.error_message,
            "title": state.script.get("title", ""),
            "topic": state.topic_brief.get("topic", ""),
        }
