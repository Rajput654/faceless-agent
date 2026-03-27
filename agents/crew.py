"""
agents/crew.py
Optional CrewAI crew definition (not used in the main pipeline).
The main pipeline uses VideoWorkflow directly.
"""
from loguru import logger


class FacelessVideoProductionCrew:
    def __init__(self, config):
        self.config = config
        logger.info("FacelessVideoProductionCrew initialized (using VideoWorkflow pipeline)")

    def run_crew_for_topic(self, topic_brief: dict, video_id: str):
        """Delegates to VideoWorkflow."""
        from workflows.video_workflow import VideoWorkflow
        workflow = VideoWorkflow(self.config)
        return workflow.run_single_video(topic_brief, video_id)
