"""
agents/script_writer.py

UPGRADED: Three-Pass Prompt Chain for high-retention scripts.

Pass 1 — EXTRACTOR: Pulls the raw emotional core and key facts from the topic.
Pass 2 — SCRIPTWRITER: Writes a full Hook → Body → CTA script with dramatic pacing.
Pass 3 — HOOK SHARPENER: Rewrites ONLY the opening 15 words to be visceral and specific.

Each pass sleeps between calls to respect Groq's free-tier rate limits.
The result is a script that feels written by a human storyteller, not a bot.
"""
import os
import json
import time
import yaml
from pathlib import Path
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

try:
    from groq import Groq
except ImportError:
    Groq = None


# ---------------------------------------------------------------------------
# Niche-specific system prompts for Pass 2 (the main scriptwriter)
# ---------------------------------------------------------------------------
NICHE_SYSTEM_PROMPTS = {
    "motivation": (
        "You are an elite YouTube Shorts scriptwriter. You write motivational scripts "
        "that feel like a trusted mentor speaking directly to the viewer. Every sentence "
        "earns its place. You create emotional urgency without being preachy. "
        "Hook → Body → CTA structure. Max 55 seconds when spoken at 2.5 words/sec."
    ),
    "horror": (
        "You write spine-chilling horror scripts for YouTube Shorts. True-events style. "
        "Second-person perspective ('You open the door...'). Build tension progressively — "
        "never reveal the twist too early. Every sentence should make the listener feel "
        "slightly more uncomfortable than the last. End on a deeply unsettling revelation, "
        "not a jump-scare. Max 55 seconds at 2.0 words/sec."
    ),
    "reddit_story": (
        "You turn Reddit posts into gripping first-person narratives for YouTube Shorts. "
        "Authentic voice — like someone telling their friend the story at 2am. "
        "Build to a satisfying twist the viewer did not see coming. "
        "CTA must ask viewers to share their own experience. Max 55 seconds at 2.3 words/sec."
    ),
    "brainrot": (
        "You write Gen Z brainrot content. Maximum controlled chaos. Short punchy sentences. "
        "Internet culture references. Absurdist logic that somehow makes sense. "
        "Each fact must be genuinely surprising. End with something completely unexpected "
        "that recontextualizes everything before it. Max 55 seconds at 3.0 words/sec."
    ),
    "finance": (
        "You write high-value personal finance scripts for YouTube Shorts. "
        "Lead with a surprising counterintuitive financial fact backed by a real number. "
        "ONE specific actionable tip per video. Frame as educational, not financial advice. "
        "Viewer should feel smarter and slightly alarmed after watching. "
        "Max 55 seconds at 2.2 words/sec."
    ),
}

# Pass 1 system prompt — same for all niches
EXTRACTOR_SYSTEM = (
    "You are a story analyst. Your job is to extract the raw emotional core from a topic brief. "
    "Be ruthlessly concise. Return only a JSON object, no markdown, no preamble."
)

# Pass 3 system prompt — same for all niches
HOOK_SHARPENER_SYSTEM = (
    "You are a master of opening lines. Your only job is to rewrite the first sentence of a "
    "script to be more visceral, specific, and impossible to scroll past. "
    "Rules: No vague words like 'amazing', 'incredible', 'unbelievable'. "
    "Use concrete details. Create a curiosity gap or an immediate emotional punch. "
    "Return ONLY the improved opening sentence. Nothing else. No quotes around it."
)


class ScriptWriterAgent:
    def __init__(self, config):
        self.config = config
        self.niche = os.environ.get("NICHE", config.get("video", {}).get("niche", "motivation"))
        self.groq_key = os.environ.get("GROQ_API_KEY", "")
        self.template = self._load_template()
        # Sleep duration between Groq calls to respect free-tier rate limits
        self._inter_call_sleep = float(os.environ.get("GROQ_SLEEP_SECONDS", "12"))

    def _load_template(self):
        template_path = Path(f"templates/{self.niche}.yaml")
        if template_path.exists():
            with open(template_path) as f:
                return yaml.safe_load(f)
        return {
            "niche": self.niche,
            "system_prompt": NICHE_SYSTEM_PROMPTS.get(self.niche, f"You write engaging YouTube Shorts scripts for {self.niche} content."),
            "tone": "engaging",
            "avg_words_per_second": 2.5,
            "hook_patterns": ["Nobody told you this about [topic]..."],
        }

    # ------------------------------------------------------------------
    # Core LLM caller — shared by all three passes
    # ------------------------------------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=15))
    def _call_llm(self, prompt: str, system_prompt: str, model: str = None, max_tokens: int = None) -> str:
        if not Groq or not self.groq_key:
            raise RuntimeError("Groq not available — set GROQ_API_KEY")

        client = Groq(api_key=self.groq_key)
        _model = model or self.config.get("llm", {}).get("primary_model", "llama-3.3-70b-versatile")
        _max_tokens = max_tokens or self.config.get("llm", {}).get("max_tokens", 2000)

        response = client.chat.completions.create(
            model=_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": prompt},
            ],
            temperature=self.config.get("llm", {}).get("temperature", 0.8),
            max_tokens=_max_tokens,
        )
        return response.choices[0].message.content.strip()

    # ------------------------------------------------------------------
    # PASS 1 — EXTRACTOR
    # Pulls the emotional core, key tension, and 3 most surprising facts
    # from the raw topic brief.
    # ------------------------------------------------------------------
    def _pass1_extract(self, topic_brief: dict) -> dict:
        logger.info("  [Pass 1/3] Extracting emotional core...")

        prompt = f"""Analyze this topic brief and extract the storytelling essentials.

Topic: {topic_brief.get('topic', '')}
Hook hint: {topic_brief.get('hook', '')}
Angle: {topic_brief.get('angle', '')}
Emotion to evoke: {topic_brief.get('emotion', 'curiosity')}

Return ONLY this JSON object (no markdown):
{{
  "core_mystery": "The single most surprising or disturbing thing about this topic in one sentence",
  "emotional_trigger": "The specific human fear, desire, or curiosity this taps into",
  "key_facts": [
    "Most surprising specific fact",
    "Second most surprising specific fact",
    "The fact that will make viewers pause and rewind"
  ],
  "tension_arc": "How the tension should build: what the viewer thinks at the start vs what they realize at the end",
  "twist": "The unexpected angle or revelation that recontextualizes everything"
}}"""

        try:
            raw = self._call_llm(prompt, EXTRACTOR_SYSTEM, max_tokens=600)
            raw = self._strip_json_fences(raw)
            extracted = json.loads(raw)
            logger.success("  [Pass 1/3] Core extracted successfully")
            return extracted
        except Exception as e:
            logger.warning(f"  [Pass 1/3] Extraction failed: {e}. Using raw topic brief.")
            return {
                "core_mystery": topic_brief.get("topic", ""),
                "emotional_trigger": topic_brief.get("emotion", "curiosity"),
                "key_facts": [topic_brief.get("hook", ""), topic_brief.get("angle", ""), ""],
                "tension_arc": "Build curiosity, deliver surprise",
                "twist": topic_brief.get("hook", ""),
            }

    # ------------------------------------------------------------------
    # PASS 2 — SCRIPTWRITER
    # Uses the extracted core to write a full structured script.
    # ------------------------------------------------------------------
    def _pass2_write_script(self, topic_brief: dict, extracted: dict, video_id: str) -> dict:
        logger.info("  [Pass 2/3] Writing full script...")

        duration = self.config.get("video", {}).get("duration_seconds", 55)
        wps = self.template.get("avg_words_per_second", 2.5)
        target_words = int(duration * wps)
        tone = self.template.get("tone", "engaging")
        system_prompt = NICHE_SYSTEM_PROMPTS.get(
            self.niche,
            self.template.get("system_prompt", f"You write {self.niche} YouTube Shorts scripts.")
        )

        hook_patterns = self.template.get("hook_patterns", [])
        hook_hint = hook_patterns[0] if hook_patterns else topic_brief.get("hook", "")

        prompt = f"""Write a YouTube Shorts script using this storytelling brief:

TOPIC: {topic_brief.get('topic', '')}
CORE MYSTERY: {extracted.get('core_mystery', '')}
EMOTIONAL TRIGGER: {extracted.get('emotional_trigger', '')}
KEY FACTS TO WEAVE IN:
  - {extracted.get('key_facts', [''])[0]}
  - {extracted.get('key_facts', ['', ''])[1] if len(extracted.get('key_facts', [])) > 1 else ''}
  - {extracted.get('key_facts', ['', '', ''])[2] if len(extracted.get('key_facts', [])) > 2 else ''}
TENSION ARC: {extracted.get('tension_arc', '')}
TWIST TO LAND: {extracted.get('twist', '')}
HOOK HINT: {hook_hint}
TARGET TONE: {tone}
TARGET WORDS: {target_words} (strict — this controls video duration)

STRUCTURE RULES:
- HOOK (first 10 words): Must create an immediate curiosity gap. No setup. Drop the viewer mid-story.
- BODY: Deliver the key facts in order of increasing surprise. Each sentence should make the viewer MORE invested.
- TWIST: Land the unexpected angle 10 seconds before the end.
- CTA: One natural, conversational sentence. Never say "like and subscribe".

PACING RULES for TTS:
- Short sentences = faster pace = tension
- Longer sentences = slower pace = reflection
- Use "..." sparingly for dramatic pauses
- Never use em-dashes (they break TTS)

Return ONLY this JSON object (no markdown):
{{
  "video_id": "{video_id}",
  "title": "YouTube-optimized title under 60 chars, no clickbait words",
  "description": "2-sentence description ending with 3 relevant hashtags",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "script": "Full script text ready for TTS. No stage directions. Pure spoken words only.",
  "hook": "The exact opening line (first sentence only)",
  "cta": "The call to action (last sentence only)",
  "word_count": {target_words},
  "emotion": "{topic_brief.get('emotion', 'curiosity')}",
  "topic_brief": {json.dumps(topic_brief)}
}}"""

        try:
            raw = self._call_llm(prompt, system_prompt, max_tokens=2000)
            raw = self._strip_json_fences(raw)
            script_data = json.loads(raw)
            script_data["video_id"] = video_id
            logger.success(f"  [Pass 2/3] Script written: '{script_data.get('title', 'Untitled')}'")
            return script_data
        except Exception as e:
            logger.warning(f"  [Pass 2/3] Script writing failed: {e}. Using fallback.")
            return self._fallback_script(topic_brief, video_id, target_words)

    # ------------------------------------------------------------------
    # PASS 3 — HOOK SHARPENER
    # Rewrites ONLY the opening line to be maximally scroll-stopping.
    # Does NOT touch the rest of the script.
    # ------------------------------------------------------------------
    def _pass3_sharpen_hook(self, script_data: dict) -> dict:
        logger.info("  [Pass 3/3] Sharpening hook...")

        original_script = script_data.get("script", "")
        original_hook = script_data.get("hook", "")

        if not original_hook or not original_script:
            logger.warning("  [Pass 3/3] No hook to sharpen. Skipping.")
            return script_data

        # Extract the first sentence to sharpen
        sentences = original_script.split(". ")
        first_sentence = sentences[0] if sentences else original_hook
        rest_of_script = ". ".join(sentences[1:]) if len(sentences) > 1 else ""

        prompt = f"""The following is the opening line of a YouTube Shorts script in the '{self.niche}' niche.

CURRENT OPENING: "{first_sentence}"

FULL SCRIPT CONTEXT (do NOT rewrite this — just use it to understand the story):
"{original_script[:300]}..."

Rewrite ONLY the opening line to be more visceral and specific.
- It must be under 20 words
- It must create an immediate curiosity gap OR an emotional punch
- It must feel like a real person dropping a bombshell, not a YouTube intro
- No vague words: amazing, incredible, unbelievable, shocking, insane
- Use a specific concrete detail from the script if possible

Return ONLY the improved opening line. No quotes. No explanation."""

        try:
            sharpened_hook = self._call_llm(
                prompt, HOOK_SHARPENER_SYSTEM,
                max_tokens=80  # Keep it fast — we only need one sentence
            ).strip().strip('"').strip("'")

            if sharpened_hook and len(sharpened_hook.split()) <= 25:
                # Rebuild the script with the new hook
                if rest_of_script:
                    new_script = sharpened_hook + ". " + rest_of_script
                else:
                    new_script = sharpened_hook

                script_data["script"] = new_script
                script_data["hook"] = sharpened_hook
                logger.success(f"  [Pass 3/3] Hook sharpened: \"{sharpened_hook}\"")
            else:
                logger.warning(f"  [Pass 3/3] Sharpened hook was too long or empty. Keeping original.")

        except Exception as e:
            logger.warning(f"  [Pass 3/3] Hook sharpening failed: {e}. Keeping original hook.")

        return script_data

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self, topic_brief: dict, video_id: str, *args, **kwargs) -> dict:
        logger.info(f"ScriptWriterAgent → 3-pass chain for: {topic_brief.get('topic', 'Unknown')}")

        try:
            # PASS 1
            extracted = self._pass1_extract(topic_brief)
            time.sleep(self._inter_call_sleep)

            # PASS 2
            script_data = self._pass2_write_script(topic_brief, extracted, video_id)
            time.sleep(self._inter_call_sleep)

            # PASS 3
            script_data = self._pass3_sharpen_hook(script_data)

            logger.success(
                f"ScriptWriterAgent complete | "
                f"Title: '{script_data.get('title', 'Untitled')}' | "
                f"Hook: '{script_data.get('hook', '')[:60]}...'"
            )
            return script_data

        except Exception as e:
            logger.error(f"ScriptWriterAgent 3-pass chain failed: {e}. Falling back.")
            duration = self.config.get("video", {}).get("duration_seconds", 55)
            wps = self.template.get("avg_words_per_second", 2.5)
            target_words = int(duration * wps)
            return self._fallback_script(topic_brief, video_id, target_words)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _strip_json_fences(raw: str) -> str:
        """Remove markdown code fences that LLMs sometimes wrap JSON in."""
        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            # parts[1] is the content between the first pair of fences
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return raw.strip()

    def _fallback_script(self, topic_brief: dict, video_id: str, target_words: int) -> dict:
        """Emergency fallback if all three passes fail."""
        topic = topic_brief.get("topic", "this")
        hook = topic_brief.get("hook", f"Nobody told you this about {topic}...")
        script = (
            f"{hook} "
            f"Most people go through life without understanding the real truth about {topic}. "
            f"But the ones who do? They change everything. "
            f"Here is what you need to know right now. "
            f"The answer is not what you expect. "
            f"Start paying attention. "
            f"If this made you think, share it with someone who needs to hear it."
        )
        return {
            "video_id": video_id,
            "title": str(topic)[:55],
            "description": f"{topic} #Shorts #Viral",
            "tags": ["shorts", "viral", "facts", "motivation", "trending"],
            "script": script,
            "hook": hook,
            "cta": "Share it with someone who needs to hear it.",
            "word_count": len(script.split()),
            "emotion": topic_brief.get("emotion", "curiosity"),
            "topic_brief": topic_brief,
        }
