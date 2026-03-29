"""
agents/script_writer.py

UPGRADED: Four-Pass Prompt Chain for maximum retention scripts.

Pass 1 — EXTRACTOR: Pulls the raw emotional core and key facts from the topic.
Pass 2 — SCRIPTWRITER: Writes a full Hook → Body → CTA script with dramatic pacing.
         Now enforces VISUAL RHYTHM RULES: one idea per sentence, 8-12 words max,
         18-25 sentences total, visual anchors every 4th sentence.
Pass 3 — HOOK SHARPENER: Rewrites ONLY the opening 15 words to be visceral and specific.
Pass 4 — LOOP ENGINEER: Rewrites the CTA to call back to the hook, engineering rewatches.

Each pass sleeps between calls to respect Groq's free-tier rate limits.
The result is a script that feels written by a human storyteller, not a bot,
AND is optimized for rapid visual cutting (one sentence = one visual cut).
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
        "Hook → Body → CTA structure. Max 55 seconds when spoken at 2.5 words/sec. "
        "CRITICAL: Write ONE idea per sentence. Max 12 words per sentence. "
        "Each sentence must pair with one visual cut."
    ),
    "horror": (
        "You write spine-chilling horror scripts for YouTube Shorts. True-events style. "
        "Second-person perspective ('You open the door...'). Build tension progressively — "
        "never reveal the twist too early. Every sentence should make the listener feel "
        "slightly more uncomfortable than the last. End on a deeply unsettling revelation, "
        "not a jump-scare. Max 55 seconds at 2.0 words/sec. "
        "CRITICAL: Write ONE idea per sentence. Max 12 words per sentence. "
        "Short sentences build dread. Use them."
    ),
    "reddit_story": (
        "You turn Reddit posts into gripping first-person narratives for YouTube Shorts. "
        "Authentic voice — like someone telling their friend the story at 2am. "
        "Build to a satisfying twist the viewer did not see coming. "
        "CTA must ask viewers to share their own experience. Max 55 seconds at 2.3 words/sec. "
        "CRITICAL: Write ONE idea per sentence. Max 12 words per sentence. "
        "Conversational rhythm. Each sentence lands before the next begins."
    ),
    "brainrot": (
        "You write Gen Z brainrot content. Maximum controlled chaos. Short punchy sentences. "
        "Internet culture references. Absurdist logic that somehow makes sense. "
        "Each fact must be genuinely surprising. End with something completely unexpected "
        "that recontextualizes everything before it. Max 55 seconds at 3.0 words/sec. "
        "CRITICAL: Write ONE idea per sentence. Max 10 words per sentence. Faster is better."
    ),
    "finance": (
        "You write high-value personal finance scripts for YouTube Shorts. "
        "Lead with a surprising counterintuitive financial fact backed by a real number. "
        "ONE specific actionable tip per video. Frame as educational, not financial advice. "
        "Viewer should feel smarter and slightly alarmed after watching. "
        "Max 55 seconds at 2.2 words/sec. "
        "CRITICAL: Write ONE idea per sentence. Max 12 words per sentence. "
        "Real numbers in every other sentence. Concrete beats abstract."
    ),
}

EXTRACTOR_SYSTEM = (
    "You are a story analyst. Your job is to extract the raw emotional core from a topic brief. "
    "Be ruthlessly concise. Return only a JSON object, no markdown, no preamble."
)

HOOK_SHARPENER_SYSTEM = (
    "You are a master of opening lines. Your only job is to rewrite the first sentence of a "
    "script to be more visceral, specific, and impossible to scroll past. "
    "Rules: No vague words like 'amazing', 'incredible', 'unbelievable'. "
    "Use concrete details. Create a curiosity gap or an immediate emotional punch. "
    "Return ONLY the improved opening sentence. Nothing else. No quotes around it."
)

LOOP_ENGINEER_SYSTEM = (
    "You are a YouTube retention expert. Your job is to rewrite a video's CTA (call to action) "
    "so it subtly loops back to the opening line. When the video loops, the ending should flow "
    "naturally into the beginning — making viewers feel like they are watching a new angle. "
    "Rules: Under 15 words. Must reference or mirror a concept from the opening. "
    "No 'like and subscribe'. Return ONLY the new CTA sentence. Nothing else."
)

# Emotion → Ken Burns preset mapping
EMOTION_KB_PRESET = {
    "inspiration": "zoom_out",
    "urgency":     "zoom_out",
    "fear":        "slow_zoom_in",
    "dread":       "slow_zoom_in",
    "shock":       "slow_zoom_in",
    "curiosity":   "slow_zoom_in",
    "amusement":   "fast_pan",
    "chaos":       "fast_pan",
    "default":     "slow_zoom_in",
}

# Visual rhythm rules injected into Pass 2 prompt
VISUAL_RHYTHM_RULES = """
VISUAL RHYTHM RULES (these are critical for viewer retention — do not skip):
- Write EXACTLY ONE idea per sentence. One sentence = one visual cut.
- Maximum 12 words per sentence. Shorter is almost always better.
- Target 18-25 sentences total for a 55-second video.
- Every 4th sentence must be a VISUAL ANCHOR — a concrete image the viewer
  can picture: a specific person, a specific place, a specific number, a specific action.
  Example of bad visual anchor: "This changes everything about how you think."
  Example of good visual anchor: "A 27-year-old accountant discovered this by accident."
- NEVER write two abstract sentences in a row.
- Short sentences = urgency and tension. Use them for the hook and twist.
- Slightly longer sentences (10-12 words) = reflection. Use them for the body facts.
- Use "..." sparingly — maximum 2 times in the entire script, for dramatic pauses only.
- Never use em-dashes (they break TTS). Use periods instead.
- No parentheses, no quotes within the script text.
"""


class ScriptWriterAgent:
    def __init__(self, config):
        self.config = config
        self.niche = os.environ.get("NICHE", config.get("video", {}).get("niche", "motivation"))
        self.groq_key = os.environ.get("GROQ_API_KEY", "")
        self.template = self._load_template()
        self._inter_call_sleep = float(os.environ.get("GROQ_SLEEP_SECONDS", "12"))

    def _load_template(self):
        template_path = Path(f"templates/{self.niche}.yaml")
        if template_path.exists():
            with open(template_path) as f:
                return yaml.safe_load(f)
        return {
            "niche": self.niche,
            "system_prompt": NICHE_SYSTEM_PROMPTS.get(
                self.niche, f"You write engaging YouTube Shorts scripts for {self.niche} content."
            ),
            "tone": "engaging",
            "avg_words_per_second": 2.5,
            "hook_patterns": ["Nobody told you this about [topic]..."],
        }

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
    # ------------------------------------------------------------------
    def _pass1_extract(self, topic_brief: dict) -> dict:
        logger.info("  [Pass 1/4] Extracting emotional core...")

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
    "Most surprising specific fact — include a real number or concrete detail if possible",
    "Second most surprising specific fact — include a real number or concrete detail",
    "The fact that will make viewers pause and rewind — the twist or revelation"
  ],
  "tension_arc": "How the tension should build: what the viewer thinks at the start vs what they realize at the end",
  "twist": "The unexpected angle or revelation that recontextualizes everything",
  "visual_anchors": [
    "A specific concrete image for scene 1 (person/place/action)",
    "A specific concrete image for the midpoint scene",
    "A specific concrete image for the twist reveal"
  ]
}}"""

        try:
            raw = self._call_llm(prompt, EXTRACTOR_SYSTEM, max_tokens=700)
            raw = self._strip_json_fences(raw)
            extracted = json.loads(raw)
            logger.success("  [Pass 1/4] Core extracted successfully")
            return extracted
        except Exception as e:
            logger.warning(f"  [Pass 1/4] Extraction failed: {e}. Using raw topic brief.")
            return {
                "core_mystery": topic_brief.get("topic", ""),
                "emotional_trigger": topic_brief.get("emotion", "curiosity"),
                "key_facts": [topic_brief.get("hook", ""), topic_brief.get("angle", ""), ""],
                "tension_arc": "Build curiosity, deliver surprise",
                "twist": topic_brief.get("hook", ""),
                "visual_anchors": [topic_brief.get("topic", ""), "", ""],
            }

    # ------------------------------------------------------------------
    # PASS 2 — SCRIPTWRITER (with Visual Rhythm Rules)
    # ------------------------------------------------------------------
    def _pass2_write_script(self, topic_brief: dict, extracted: dict, video_id: str) -> dict:
        logger.info("  [Pass 2/4] Writing full script with visual rhythm rules...")

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

        key_facts = extracted.get("key_facts", ["", "", ""])
        visual_anchors = extracted.get("visual_anchors", ["", "", ""])
        fact1 = key_facts[0] if len(key_facts) > 0 else ""
        fact2 = key_facts[1] if len(key_facts) > 1 else ""
        fact3 = key_facts[2] if len(key_facts) > 2 else ""
        anchor1 = visual_anchors[0] if len(visual_anchors) > 0 else ""
        anchor2 = visual_anchors[1] if len(visual_anchors) > 1 else ""
        anchor3 = visual_anchors[2] if len(visual_anchors) > 2 else ""

        prompt = f"""Write a YouTube Shorts script using this storytelling brief:

TOPIC: {topic_brief.get('topic', '')}
CORE MYSTERY: {extracted.get('core_mystery', '')}
EMOTIONAL TRIGGER: {extracted.get('emotional_trigger', '')}
KEY FACTS TO WEAVE IN:
  - {fact1}
  - {fact2}
  - {fact3}
VISUAL ANCHORS (use these as concrete scene descriptions):
  - Opening scene: {anchor1}
  - Midpoint scene: {anchor2}
  - Twist scene: {anchor3}
TENSION ARC: {extracted.get('tension_arc', '')}
TWIST TO LAND: {extracted.get('twist', '')}
HOOK HINT: {hook_hint}
TARGET TONE: {tone}
TARGET WORDS: {target_words} (strict — this controls video duration)
TARGET SENTENCES: 18-25 sentences

{VISUAL_RHYTHM_RULES}

STRUCTURE RULES:
- HOOK (first 1-2 sentences, max 15 words total): Must create an immediate curiosity gap.
  No setup. Drop the viewer mid-story. Make it impossible to swipe away.
- BODY (sentences 3-20): Deliver key facts in order of increasing surprise.
  Alternate between facts and visual anchors. Each sentence = one visual cut.
  Every 4th sentence must be a visual anchor (concrete image the viewer can see).
- TWIST (sentences 19-22): Land the unexpected angle 10 seconds before the end.
  Use a short sentence (under 8 words) to deliver it. Let it breathe.
- CTA (last 1-2 sentences): Natural, conversational. Never say "like and subscribe".
  Ask the viewer a question or challenge them. This will be rewritten in Pass 4.

Return ONLY this JSON object (no markdown):
{{
  "video_id": "{video_id}",
  "title": "YouTube-optimized title under 60 chars, no clickbait words",
  "description": "2-sentence description ending with 3 relevant hashtags",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "script": "Full script text ready for TTS. No stage directions. Pure spoken words only. Each sentence on its own. Short. Punchy. One idea per sentence.",
  "hook": "The exact opening line (first sentence only)",
  "cta": "The call to action (last sentence only)",
  "word_count": {target_words},
  "sentence_count": 20,
  "emotion": "{topic_brief.get('emotion', 'curiosity')}",
  "topic_brief": {json.dumps(topic_brief)}
}}"""

        try:
            raw = self._call_llm(prompt, system_prompt, max_tokens=2000)
            raw = self._strip_json_fences(raw)
            script_data = json.loads(raw)
            script_data["video_id"] = video_id

            # Validate sentence structure — warn if too long
            script_text = script_data.get("script", "")
            sentences = [s.strip() for s in script_text.split(".") if s.strip()]
            long_sentences = [s for s in sentences if len(s.split()) > 14]
            if long_sentences:
                logger.warning(
                    f"  [Pass 2/4] {len(long_sentences)} sentences exceed 14 words — "
                    f"visual rhythm may suffer. Example: '{long_sentences[0][:60]}'"
                )

            logger.success(
                f"  [Pass 2/4] Script written: '{script_data.get('title', 'Untitled')}' | "
                f"{len(sentences)} sentences"
            )
            return script_data
        except Exception as e:
            logger.warning(f"  [Pass 2/4] Script writing failed: {e}. Using fallback.")
            return self._fallback_script(topic_brief, video_id, target_words)

    # ------------------------------------------------------------------
    # PASS 3 — HOOK SHARPENER
    # ------------------------------------------------------------------
    def _pass3_sharpen_hook(self, script_data: dict) -> dict:
        logger.info("  [Pass 3/4] Sharpening hook...")

        original_script = script_data.get("script", "")
        original_hook = script_data.get("hook", "")

        if not original_hook or not original_script:
            logger.warning("  [Pass 3/4] No hook to sharpen. Skipping.")
            return script_data

        sentences = original_script.split(". ")
        first_sentence = sentences[0] if sentences else original_hook
        rest_of_script = ". ".join(sentences[1:]) if len(sentences) > 1 else ""

        prompt = f"""The following is the opening line of a YouTube Shorts script in the '{self.niche}' niche.

CURRENT OPENING: "{first_sentence}"

FULL SCRIPT CONTEXT (do NOT rewrite this — just use it to understand the story):
"{original_script[:300]}..."

Rewrite ONLY the opening line to be more visceral and specific.
- It must be under 18 words
- It must create an immediate curiosity gap OR an emotional punch
- It must feel like a real person dropping a bombshell, not a YouTube intro
- No vague words: amazing, incredible, unbelievable, shocking, insane, crazy
- Use a specific concrete detail from the script if possible
- Start mid-action if possible — drop the viewer into the story immediately

Return ONLY the improved opening line. No quotes. No explanation."""

        try:
            sharpened_hook = self._call_llm(
                prompt, HOOK_SHARPENER_SYSTEM, max_tokens=80
            ).strip().strip('"').strip("'")

            if sharpened_hook and len(sharpened_hook.split()) <= 25:
                new_script = sharpened_hook + ". " + rest_of_script if rest_of_script else sharpened_hook
                script_data["script"] = new_script
                script_data["hook"] = sharpened_hook
                logger.success(f"  [Pass 3/4] Hook sharpened: \"{sharpened_hook}\"")
            else:
                logger.warning("  [Pass 3/4] Sharpened hook was too long or empty. Keeping original.")

        except Exception as e:
            logger.warning(f"  [Pass 3/4] Hook sharpening failed: {e}. Keeping original hook.")

        return script_data

    # ------------------------------------------------------------------
    # PASS 4 — LOOP ENGINEER (NEW)
    # ------------------------------------------------------------------
    def _pass4_loop_engineer(self, script_data: dict) -> dict:
        """
        Rewrites the CTA to subtly call back to the hook, engineering rewatches.
        When the video loops, the ending flows naturally into the beginning.
        This drives 100%+ Average Percentage Viewed — the viral metric.
        """
        logger.info("  [Pass 4/4] Engineering loop CTA...")

        hook = script_data.get("hook", "")
        current_cta = script_data.get("cta", "")
        script_text = script_data.get("script", "")

        if not hook or not script_text:
            logger.warning("  [Pass 4/4] Insufficient data for loop engineering. Skipping.")
            return script_data

        prompt = f"""Opening line of this YouTube Short: "{hook}"

Current CTA (last line): "{current_cta}"

The TOPIC is: {script_data.get('topic_brief', {}).get('topic', '')}
The EMOTION is: {script_data.get('emotion', 'curiosity')}

Rewrite the CTA so that when the video loops back to the opening line,
it feels like a natural continuation — almost like the ending and beginning
are part of a circle. The viewer should feel compelled to watch again.

Examples of good loop CTAs:
  - Hook: "The bank never told you this one thing." → CTA: "Go back to the beginning. Did you catch what they're hiding?"
  - Hook: "You are being watched right now." → CTA: "Now watch it again. You'll see it this time."
  - Hook: "This man changed everything in 47 seconds." → CTA: "Does 47 seconds mean something different to you now?"

Rules:
- Under 15 words
- Must reference the OPENING line's concept or a word/number from it
- Must create the desire to rewatch
- No "like and subscribe"

Return ONLY the new CTA sentence. No explanation. No quotes."""

        try:
            loop_cta = self._call_llm(
                prompt, LOOP_ENGINEER_SYSTEM, max_tokens=60
            ).strip().strip('"').strip("'")

            if loop_cta and 3 < len(loop_cta.split()) <= 20:
                # Replace the last sentence of the script with the loop CTA
                script_text = script_data.get("script", "")
                # Find the last period and replace everything after it
                last_period_idx = script_text.rfind(".")
                if last_period_idx > len(script_text) // 2:
                    # Only replace if the last period is in the second half (safety check)
                    new_script = script_text[:last_period_idx + 1] + " " + loop_cta
                else:
                    new_script = script_text + " " + loop_cta

                script_data["script"] = new_script.strip()
                script_data["cta"] = loop_cta
                logger.success(f"  [Pass 4/4] Loop CTA engineered: \"{loop_cta}\"")
            else:
                logger.warning(
                    f"  [Pass 4/4] Loop CTA was invalid ('{loop_cta}'). Keeping original CTA."
                )

        except Exception as e:
            logger.warning(f"  [Pass 4/4] Loop engineering failed: {e}. Keeping original CTA.")

        return script_data

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self, topic_brief: dict, video_id: str, *args, **kwargs) -> dict:
        logger.info(f"ScriptWriterAgent → 4-pass chain for: {topic_brief.get('topic', 'Unknown')}")

        try:
            # PASS 1
            extracted = self._pass1_extract(topic_brief)
            time.sleep(self._inter_call_sleep)

            # PASS 2
            script_data = self._pass2_write_script(topic_brief, extracted, video_id)
            time.sleep(self._inter_call_sleep)

            # PASS 3
            script_data = self._pass3_sharpen_hook(script_data)
            time.sleep(self._inter_call_sleep)

            # PASS 4 — Loop Engineer
            script_data = self._pass4_loop_engineer(script_data)

            # ── Attach extracted data and KB preset so VideoWorkflow ──
            emotion = script_data.get("emotion", "curiosity")
            script_data["_extracted"] = extracted
            script_data["_kb_preset"] = EMOTION_KB_PRESET.get(emotion, EMOTION_KB_PRESET["default"])

            logger.success(
                f"ScriptWriterAgent complete | "
                f"Title: '{script_data.get('title', 'Untitled')}' | "
                f"Hook: '{script_data.get('hook', '')[:60]}' | "
                f"CTA (loop): '{script_data.get('cta', '')[:60]}' | "
                f"KB preset: {script_data['_kb_preset']}"
            )
            return script_data

        except Exception as e:
            logger.error(f"ScriptWriterAgent 4-pass chain failed: {e}. Falling back.")
            duration = self.config.get("video", {}).get("duration_seconds", 55)
            wps = self.template.get("avg_words_per_second", 2.5)
            target_words = int(duration * wps)
            return self._fallback_script(topic_brief, video_id, target_words)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _strip_json_fences(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return raw.strip()

    def _fallback_script(self, topic_brief: dict, video_id: str, target_words: int) -> dict:
        topic = topic_brief.get("topic", "this")
        hook = topic_brief.get("hook", f"Nobody told you this about {topic}...")
        emotion = topic_brief.get("emotion", "curiosity")
        script = (
            f"{hook} "
            f"Most people never learn the truth. "
            f"It happens every single day. "
            f"Somewhere, right now, someone is figuring this out. "
            f"And it changes everything. "
            f"The answer is not what you expect. "
            f"Here is what the evidence actually shows. "
            f"Pay close attention to this part. "
            f"Start applying this today. "
            f"Does this change how you see it now?"
        )
        return {
            "video_id":   video_id,
            "title":      str(topic)[:55],
            "description": f"{topic} #Shorts #Viral",
            "tags":       ["shorts", "viral", "facts", "motivation", "trending"],
            "script":     script,
            "hook":       hook,
            "cta":        "Does this change how you see it now?",
            "word_count": len(script.split()),
            "sentence_count": len([s for s in script.split(".") if s.strip()]),
            "emotion":    emotion,
            "topic_brief": topic_brief,
            "_extracted": {
                "core_mystery":      topic,
                "emotional_trigger": emotion,
                "key_facts":         [hook, "", ""],
                "tension_arc":       "Build curiosity, deliver surprise",
                "twist":             hook,
                "visual_anchors":    [topic, "", ""],
            },
            "_kb_preset": EMOTION_KB_PRESET.get(emotion, EMOTION_KB_PRESET["default"]),
        }
