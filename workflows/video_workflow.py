# workflows/video_workflow.py
# Full LangGraph state machine implementation
# Paste full code from the code generation session

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

    def run_single_video(self, topic_brief, video_id, video_index=0):
        raise NotImplementedError("Paste full workflow code")
