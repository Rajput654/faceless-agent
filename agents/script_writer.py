"""
agents/script_writer.py
Uses Groq LLM + niche templates to write YouTube Shorts scripts.
"""
import os
import json
import yaml
from pathlib import Path
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

try:
    from groq import Groq
except ImportError:
    Groq = None


class ScriptWriterAgent:
    def __init__(self, config):
        self.config = config
        self.niche = os.environ.get("NICHE", config.get("video", {}).get("niche", "motivation"))
        self.groq_key = os.environ.get("GROQ_API_KEY", "")
        self.template = self._load_template()

    def _load_template(self):
        template_path = Path(f"templates/{self.niche}.yaml")
        if template_path.exists():
            with open(template_path) as f:
                return yaml.safe_load(f)
        # Default template
        return {
            "niche": self.niche,
            "system_prompt": f"You write engaging YouTube Shorts scripts for {self.niche} content. Hook → Body → CTA structure. 55 seconds max.",
            "tone": "engaging",
            "avg_words_per_second": 2.5,
            "hook_patterns": ["Nobody told you this about [topic]..."],
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _call_llm(self, prompt, system_prompt):
        if not Groq or not self.groq_key:
            raise RuntimeError("Groq not available")

        client = Groq(api_key=self.groq_key)
        model = self.config.get("llm", {}).get("primary_model", "llama-3.3-70b-versatile")

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=self.config.get("llm", {}).get("temperature", 0.8),
            max_tokens=self.config.get("llm", {}).get("max_tokens", 2000),
        )
        return response.choices[0].message.content.strip()

    def run(self, topic_brief, video_id, *args, **kwargs):
        logger.info(f"ScriptWriterAgent writing script for: {topic_brief.get('topic', 'Unknown')}")

        duration = self.config.get("video", {}).get("duration_seconds", 55)
        wps = self.template.get("avg_words_per_second", 2.5)
        target_words = int(duration * wps)
        tone = self.template.get("tone", "engaging")
        system_prompt = self.template.get("system_prompt", f"You write {self.niche} YouTube Shorts scripts.")

        hook_patterns = self.template.get("hook_patterns", [])
        hook_hint = hook_patterns[0] if hook_patterns else ""

        prompt = f"""Write a YouTube Shorts script for this topic:
Topic: {topic_brief.get('topic', '')}
Hook: {topic_brief.get('hook', hook_hint)}
Angle: {topic_brief.get('angle', 'Unique perspective')}
Emotion to evoke: {topic_brief.get('emotion', 'inspiration')}
Target duration: {duration} seconds (~{target_words} words)
Tone: {tone}

Structure:
1. HOOK (first 3 seconds - must grab attention immediately)
2. BODY (build tension, deliver value, keep them watching)
3. CTA (end with engagement prompt)

Return ONLY a JSON object (no markdown):
{{
  "video_id": "{video_id}",
  "title": "YouTube-optimized title under 60 chars",
  "description": "Description with hashtags",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "script": "Full script text ready for TTS narration",
  "hook": "The opening hook line",
  "cta": "The call to action at the end",
  "word_count": {target_words},
  "emotion": "{topic_brief.get('emotion', 'inspiration')}",
  "topic_brief": {json.dumps(topic_brief)}
}}"""

        try:
            raw = self._call_llm(prompt, system_prompt)
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            script_data = json.loads(raw)
            script_data["video_id"] = video_id
            logger.success(f"Script written: {script_data.get('title', 'Untitled')}")
            return script_data
        except Exception as e:
            logger.warning(f"LLM script generation failed: {e}. Using fallback script.")
            return self._fallback_script(topic_brief, video_id, target_words)

    def _fallback_script(self, topic_brief, video_id, target_words):
        topic = topic_brief.get("topic", "success")
        hook = topic_brief.get("hook", f"Nobody told you this about {topic}...")
        script = (
            f"{hook} "
            f"Most people go through life without understanding the real truth about {topic}. "
            f"But the ones who do understand it? They change everything. "
            f"Here is what you need to know right now. "
            f"The secret is not about working harder. It is about working smarter, with purpose. "
            f"Every single day is a chance to become better. "
            f"Start today. Not tomorrow. Today. "
            f"If this hit different, follow for more."
        )
        return {
            "video_id": video_id,
            "title": f"{topic[:55]}",
            "description": f"{topic} #Shorts #Motivation #Success",
            "tags": ["shorts", "motivation", "success", "mindset", "viral"],
            "script": script,
            "hook": hook,
            "cta": "Follow for more.",
            "word_count": len(script.split()),
            "emotion": topic_brief.get("emotion", "inspiration"),
            "topic_brief": topic_brief,
        }
