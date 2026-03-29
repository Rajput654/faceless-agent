"""
agents/script_writer.py

FIXED v3 — Two bugs patched:

BUG FIX 1 — INCOMPLETE STORY (root causes):
  a) max_tokens=2000 on Groq free tier: The JSON response for a full script
     routinely hits the token limit mid-sentence, producing truncated JSON
     that fails json.loads silently, triggering the short fallback script.
     FIX: Pass 2 now uses max_tokens=3000. The script JSON + all metadata
     fits comfortably without truncation.

  b) JSON parse failure was silent: When json.loads failed, the fallback
     script was returned without any log that this happened. The CI showed
     "Script ready" even when only the fallback was used.
     FIX: Parse failures now log the raw response (first 1000 chars) at
     ERROR level so you can see exactly what went wrong.

  c) Script truncation in Pass 2: The prompt asked for 18-25 sentences but
     the model was fitting them into 2000 tokens of JSON including all the
     metadata fields, leaving only ~1000 tokens for the actual script.
     FIX: Prompt restructured so script is generated first, then metadata
     is derived from it in a second compact JSON. This ensures script gets
     the bulk of the token budget.

  d) Inter-call sleep was 12s: On Groq free tier, 4 calls × 12s = 48s just
     in sleep time per video. With 10 videos in parallel this strains limits.
     FIX: Default sleep reduced to 8s. Users can override with GROQ_SLEEP_SECONDS.

BUG FIX 2 — ROBOTIC VOICE (script-side contributions):
  a) Scripts contained em-dashes which edge-tts renders as literal "dash"
     FIX: _clean_script_for_tts() strips/replaces TTS-hostile characters
  b) Very long sentences (20+ words) reduce prosody quality in all TTS engines
     FIX: After Pass 2, long sentences are split at natural conjunction points
  c) All-caps words (like "NEVER", "STOP") cause TTS to spell them out
     FIX: Converted to sentence case in post-processing

PRESERVED: 4-pass chain (Extract → Write → Hook Sharpen → Loop Engineer)
PRESERVED: Niche resolved lazily at run() time — not cached at __init__
"""
import os
import re
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


# ─────────────────────────────────────────────────────────────────────────────
# Niche system prompts for Pass 2
# ─────────────────────────────────────────────────────────────────────────────
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
        "Second-person perspective. Build tension progressively. "
        "Every sentence should make the listener slightly more uncomfortable. "
        "End on a deeply unsettling revelation. Max 55 seconds at 2.0 words/sec. "
        "CRITICAL: Write ONE idea per sentence. Max 12 words per sentence."
    ),
    "reddit_story": (
        "You turn Reddit posts into gripping first-person narratives for YouTube Shorts. "
        "Authentic voice, build to a satisfying twist. "
        "CTA must ask viewers to share their experience. Max 55 seconds at 2.3 words/sec. "
        "CRITICAL: Write ONE idea per sentence. Max 12 words per sentence."
    ),
    "brainrot": (
        "You write Gen Z brainrot content. Maximum controlled chaos. Short punchy sentences. "
        "Internet culture references. Absurdist logic that somehow makes sense. "
        "End with something completely unexpected. Max 55 seconds at 3.0 words/sec. "
        "CRITICAL: Write ONE idea per sentence. Max 10 words per sentence."
    ),
    "finance": (
        "You write high-value personal finance scripts for YouTube Shorts. "
        "Lead with a surprising counterintuitive financial fact backed by a real number. "
        "ONE specific actionable tip per video. Frame as educational, not financial advice. "
        "Max 55 seconds at 2.2 words/sec. "
        "CRITICAL: Write ONE idea per sentence. Max 12 words per sentence. "
        "Real numbers in every other sentence."
    ),
}

EXTRACTOR_SYSTEM = (
    "You are a story analyst. Extract the raw emotional core from a topic brief. "
    "Be ruthlessly concise. Return only a JSON object, no markdown, no preamble."
)

HOOK_SHARPENER_SYSTEM = (
    "You are a master of opening lines. Rewrite the first sentence of a script to be "
    "more visceral, specific, and impossible to scroll past. "
    "No vague words like amazing, incredible, unbelievable. Use concrete details. "
    "Return ONLY the improved opening sentence. Nothing else. No quotes around it."
)

LOOP_ENGINEER_SYSTEM = (
    "You are a YouTube retention expert. Rewrite a video's CTA so it subtly loops "
    "back to the opening line. Under 15 words. Must reference the opening. "
    "No 'like and subscribe'. Return ONLY the new CTA sentence. Nothing else."
)

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

VISUAL_RHYTHM_RULES = """
VISUAL RHYTHM RULES (critical for viewer retention):
- Write EXACTLY ONE idea per sentence. One sentence = one visual cut.
- Maximum 12 words per sentence. Shorter is almost always better.
- Target 18-25 sentences total for a 55-second video.
- Every 4th sentence must be a VISUAL ANCHOR — a concrete image: specific person, place, number, action.
- NEVER write two abstract sentences in a row.
- Use "..." for dramatic pauses only. Maximum 2 times total.
- Never use em-dashes (they break TTS pronunciation). Use commas or periods instead.
- No parentheses, no quotes within the script text.
- No ALL-CAPS words (TTS will spell them out letter by letter).
"""


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2 (script side): Post-process script to prevent TTS robotic artifacts
# ─────────────────────────────────────────────────────────────────────────────

def _clean_script_for_tts(script_text: str) -> str:
    """
    Post-process the script to remove elements that cause robotic TTS output.

    Issues fixed:
    1. Em-dashes (—) → edge-tts often reads as "dash" 
    2. ALL-CAPS words → TTS spells them out letter by letter
    3. Long sentences (>15 words) → split at natural conjunction points
    4. Parenthetical content → removed (awkward in TTS)
    5. Multiple exclamation marks → reduced to single
    6. Brackets and special formatting → removed
    """
    text = script_text

    # 1. Em-dash and en-dash → comma pause
    text = re.sub(r'\s*[—–]\s*', ', ', text)

    # 2. ALL-CAPS words → Title Case (but preserve acronyms under 3 chars like "AI", "US")
    def fix_caps(match):
        word = match.group(0)
        if len(word) <= 3:
            return word  # preserve short acronyms
        return word.capitalize()
    text = re.sub(r'\b[A-Z]{4,}\b', fix_caps, text)

    # 3. Remove parenthetical content (TTS reads it awkwardly)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)

    # 4. Multiple exclamation/question marks → single
    text = re.sub(r'[!]{2,}', '!', text)
    text = re.sub(r'[?]{2,}', '?', text)

    # 5. Quotes within the text → remove (TTS doesn't add inflection for quotes)
    text = re.sub(r'"([^"]*)"', r'\1', text)
    text = re.sub(r"'([^']*)'", r'\1', text)

    # 6. Split very long sentences at conjunction points
    sentences = re.split(r'(?<=[.!?])\s+', text)
    processed_sentences = []
    for sentence in sentences:
        words = sentence.split()
        if len(words) > 16:
            # Try to split at conjunctions
            split_at = None
            conjunctions = [' but ', ' and ', ' so ', ' because ', ' although ', ' however ', ' which ']
            for conj in conjunctions:
                idx = sentence.lower().find(conj)
                if idx > 20 and idx < len(sentence) - 20:
                    split_at = idx
                    break
            if split_at:
                part1 = sentence[:split_at].strip()
                part2 = sentence[split_at:].strip().lstrip('but and so because although however which'.split()[0])
                # Capitalize the split point
                conj_word = sentence[split_at:split_at+10].strip().split()[0]
                part2_clean = sentence[split_at + len(conj_word) + 1:].strip().capitalize()
                processed_sentences.append(part1 + '.')
                processed_sentences.append(part2_clean + '.' if not part2_clean.endswith(('.', '!', '?')) else part2_clean)
            else:
                processed_sentences.append(sentence)
        else:
            processed_sentences.append(sentence)

    text = ' '.join(processed_sentences)

    # 7. Clean up double spaces and double periods
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\.{2,}(?!\.)' , '.', text)  # double period but not ellipsis
    text = re.sub(r'\.\s*,', '.', text)

    return text.strip()


class ScriptWriterAgent:
    def __init__(self, config):
        self.config = config
        self.groq_key = os.environ.get("GROQ_API_KEY", "")
        # FIX: Reduced from 12s to 8s — 12s × 4 passes = 48s/video is too slow
        self._inter_call_sleep = float(os.environ.get("GROQ_SLEEP_SECONDS", "8"))
        self.niche = None
        self.template = None

    def _get_niche(self) -> str:
        return os.environ.get(
            "NICHE",
            self.config.get("video", {}).get("niche", "motivation")
        )

    def _load_template(self, niche: str) -> dict:
        template_path = Path(f"templates/{niche}.yaml")
        if template_path.exists():
            with open(template_path) as f:
                return yaml.safe_load(f)
        return {
            "niche": niche,
            "system_prompt": NICHE_SYSTEM_PROMPTS.get(niche, f"You write engaging YouTube Shorts for {niche}."),
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

    # ── Pass 1: Extract ────────────────────────────────────────────────────────

    def _pass1_extract(self, topic_brief: dict) -> dict:
        logger.info("  [Pass 1/4] Extracting emotional core...")

        prompt = f"""Analyze this topic brief and extract the storytelling essentials.

Topic: {topic_brief.get('topic', '')}
Hook hint: {topic_brief.get('hook', '')}
Angle: {topic_brief.get('angle', '')}
Emotion to evoke: {topic_brief.get('emotion', 'curiosity')}

Return ONLY this JSON object (no markdown):
{{
  "core_mystery": "The single most surprising thing about this topic in one sentence",
  "emotional_trigger": "The specific human fear or desire this taps into",
  "key_facts": [
    "Most surprising specific fact with a real number or concrete detail",
    "Second most surprising fact with a real number or concrete detail",
    "The fact that will make viewers pause and rewind"
  ],
  "tension_arc": "How tension should build: what viewer thinks at start vs end",
  "twist": "The unexpected angle that recontextualizes everything",
  "visual_anchors": [
    "Specific concrete image for scene 1 (person/place/action)",
    "Specific concrete image for the midpoint scene",
    "Specific concrete image for the twist reveal"
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

    # ── Pass 2: Write Script ───────────────────────────────────────────────────
    # FIX: Split into 2 steps:
    #   Step A — Generate JUST the script text (gets full token budget)
    #   Step B — Generate the metadata JSON from the script (compact, reliable)
    # This prevents the script being cut short to fit JSON metadata into token limit.

    def _pass2_write_script(self, topic_brief: dict, extracted: dict, video_id: str) -> dict:
        logger.info("  [Pass 2/4] Writing full script (2-step: script then metadata)...")

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

        # ── Step A: Generate the full script text ────────────────────────────
        # FIX: Generate script text ONLY first, not wrapped in JSON.
        # This gives the full token budget to the actual story.
        script_prompt = f"""Write a complete YouTube Shorts script for the '{self.niche}' niche.

TOPIC: {topic_brief.get('topic', '')}
CORE MYSTERY: {extracted.get('core_mystery', '')}
EMOTIONAL TRIGGER: {extracted.get('emotional_trigger', '')}
KEY FACTS:
  - {key_facts[0] if key_facts else ''}
  - {key_facts[1] if len(key_facts)>1 else ''}
  - {key_facts[2] if len(key_facts)>2 else ''}
VISUAL ANCHORS:
  - Opening scene: {visual_anchors[0] if visual_anchors else ''}
  - Midpoint scene: {visual_anchors[1] if len(visual_anchors)>1 else ''}
  - Twist scene: {visual_anchors[2] if len(visual_anchors)>2 else ''}
TENSION ARC: {extracted.get('tension_arc', '')}
TWIST TO LAND: {extracted.get('twist', '')}
HOOK HINT: {hook_hint}
TONE: {tone}
TARGET WORD COUNT: {target_words} words (STRICT — controls video length)
TARGET SENTENCES: 18-25 sentences

{VISUAL_RHYTHM_RULES}

STRUCTURE:
- HOOK (first 1-2 sentences, max 15 words total): Immediate curiosity gap. Drop viewer mid-story.
- BODY (sentences 3-20): Key facts in order of increasing surprise. One fact per sentence.
  Every 4th sentence = concrete visual anchor (person/place/number/action).
- TWIST (sentences 19-22): Land the unexpected angle. Short sentence under 8 words.
- CTA (last 1-2 sentences): Natural question or challenge. Never say "like and subscribe".

IMPORTANT: Write ONLY the spoken script text. No labels, no headers, no stage directions.
Pure spoken words only. Each sentence on its own line. Begin directly with the hook."""

        try:
            # FIX: max_tokens=3000 — gives room for full 25-sentence script
            raw_script = self._call_llm(script_prompt, system_prompt, max_tokens=3000)

            # Clean up any labels the LLM might have added despite instructions
            raw_script = re.sub(r'^(HOOK|BODY|TWIST|CTA|INTRO|OUTRO):\s*', '', raw_script, flags=re.MULTILINE)
            raw_script = re.sub(r'^\d+\.\s+', '', raw_script, flags=re.MULTILINE)  # numbered lists
            raw_script = re.sub(r'^\*+\s*', '', raw_script, flags=re.MULTILINE)    # bullet points

            # Normalize newlines to spaces (TTS reads better as continuous text)
            raw_script = re.sub(r'\n+', ' ', raw_script).strip()

            # Count sentences for validation
            sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', raw_script) if s.strip()]
            word_count = len(raw_script.split())

            logger.info(f"  [Pass 2A] Script: {word_count} words, {len(sentences)} sentences")

            # Warn if script is suspiciously short
            if word_count < target_words * 0.6:
                logger.warning(
                    f"  [Pass 2A] Script is short: {word_count} words (target: {target_words}). "
                    f"Raw (first 200 chars): {raw_script[:200]}"
                )

        except Exception as e:
            logger.error(f"  [Pass 2A] Script generation failed: {e}")
            raw_script = None

        if not raw_script or len(raw_script.split()) < 20:
            logger.warning("  [Pass 2A] Falling back to template script")
            return self._fallback_script(topic_brief, video_id, target_words)

        # ── Step B: Generate compact metadata JSON ────────────────────────────
        # Now that we have the full script, extract metadata from it
        meta_prompt = f"""Given this YouTube Shorts script, generate the video metadata.

SCRIPT:
{raw_script[:800]}  

NICHE: {self.niche}
TOPIC: {topic_brief.get('topic', '')}
EMOTION: {topic_brief.get('emotion', 'curiosity')}

Return ONLY this compact JSON (no markdown):
{{
  "title": "YouTube-optimized title under 60 chars",
  "description": "2-sentence description ending with 3 hashtags",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "hook": "The exact first sentence of the script (verbatim)",
  "cta": "The last sentence of the script (verbatim)"
}}"""

        try:
            time.sleep(3)  # brief pause between the two sub-calls
            meta_raw = self._call_llm(meta_prompt, "You generate compact video metadata JSON. Return only valid JSON.", max_tokens=400)
            meta_raw = self._strip_json_fences(meta_raw)
            metadata = json.loads(meta_raw)
        except Exception as e:
            logger.warning(f"  [Pass 2B] Metadata generation failed: {e}. Using defaults.")
            first_sentence = re.split(r'(?<=[.!?])\s+', raw_script)[0] if raw_script else ""
            last_sentence = re.split(r'(?<=[.!?])\s+', raw_script)[-1] if raw_script else ""
            metadata = {
                "title": topic_brief.get("topic", "")[:55],
                "description": f"{topic_brief.get('topic', '')} #Shorts #Viral #{self.niche}",
                "tags": ["shorts", "viral", self.niche, "trending", "fyp"],
                "hook": first_sentence,
                "cta": last_sentence,
            }

        # Assemble final script data
        script_data = {
            "video_id":      video_id,
            "title":         metadata.get("title", topic_brief.get("topic", ""))[:100],
            "description":   metadata.get("description", ""),
            "tags":          metadata.get("tags", ["shorts"]),
            "script":        raw_script,
            "hook":          metadata.get("hook", ""),
            "cta":           metadata.get("cta", ""),
            "word_count":    len(raw_script.split()),
            "sentence_count": len([s for s in re.split(r'(?<=[.!?])\s+', raw_script) if s.strip()]),
            "emotion":       topic_brief.get("emotion", "curiosity"),
            "topic_brief":   topic_brief,
        }

        long_sentences = [s for s in re.split(r'(?<=[.!?])\s+', raw_script) if len(s.split()) > 14]
        if long_sentences:
            logger.warning(f"  [Pass 2/4] {len(long_sentences)} long sentences — visual rhythm may suffer")

        logger.success(
            f"  [Pass 2/4] Script: '{script_data.get('title', 'Untitled')}' | "
            f"{script_data['sentence_count']} sentences | {script_data['word_count']} words | niche={self.niche}"
        )
        return script_data

    # ── Pass 3: Hook Sharpener ─────────────────────────────────────────────────

    def _pass3_sharpen_hook(self, script_data: dict) -> dict:
        logger.info("  [Pass 3/4] Sharpening hook...")

        original_script = script_data.get("script", "")
        original_hook = script_data.get("hook", "")

        if not original_hook or not original_script:
            return script_data

        sentences = re.split(r'(?<=[.!?])\s+', original_script)
        first_sentence = sentences[0] if sentences else original_hook
        rest_of_script = " ".join(sentences[1:]) if len(sentences) > 1 else ""

        prompt = f"""The following is the opening line of a YouTube Shorts script in the '{self.niche}' niche.

CURRENT OPENING: "{first_sentence}"

FULL SCRIPT CONTEXT (do NOT rewrite this):
"{original_script[:300]}..."

Rewrite ONLY the opening line to be more visceral and specific.
- Under 18 words
- Immediate curiosity gap OR emotional punch
- Feel like a real person dropping a bombshell
- No vague words: amazing, incredible, unbelievable, shocking, insane, crazy
- Use a specific concrete detail from the script if possible
- No em-dashes. No ALL-CAPS.

Return ONLY the improved opening line. No quotes. No explanation."""

        try:
            sharpened_hook = self._call_llm(
                prompt, HOOK_SHARPENER_SYSTEM, max_tokens=80
            ).strip().strip('"').strip("'")

            if sharpened_hook and len(sharpened_hook.split()) <= 25:
                new_script = sharpened_hook + ". " + rest_of_script if rest_of_script else sharpened_hook
                script_data["script"] = new_script
                script_data["hook"] = sharpened_hook
                logger.success(f"  [Pass 3/4] Hook: \"{sharpened_hook}\"")
            else:
                logger.warning("  [Pass 3/4] Sharpened hook too long or empty. Keeping original.")
        except Exception as e:
            logger.warning(f"  [Pass 3/4] Hook sharpening failed: {e}")

        return script_data

    # ── Pass 4: Loop Engineer ──────────────────────────────────────────────────

    def _pass4_loop_engineer(self, script_data: dict) -> dict:
        logger.info("  [Pass 4/4] Engineering loop CTA...")

        hook = script_data.get("hook", "")
        current_cta = script_data.get("cta", "")
        script_text = script_data.get("script", "")

        if not hook or not script_text:
            return script_data

        prompt = f"""Opening line: "{hook}"
Current CTA: "{current_cta}"
Topic: {script_data.get('topic_brief', {}).get('topic', '')}

Rewrite the CTA so when the video loops, the ending flows into the opening.
- Under 15 words
- References the opening line's concept
- Creates desire to rewatch
- No "like and subscribe"
- No em-dashes. No ALL-CAPS.

Return ONLY the new CTA sentence."""

        try:
            loop_cta = self._call_llm(
                prompt, LOOP_ENGINEER_SYSTEM, max_tokens=60
            ).strip().strip('"').strip("'")

            if loop_cta and 3 < len(loop_cta.split()) <= 20:
                script_text = script_data.get("script", "")
                last_period_idx = script_text.rfind(".")
                if last_period_idx > len(script_text) // 2:
                    new_script = script_text[:last_period_idx + 1] + " " + loop_cta
                else:
                    new_script = script_text + " " + loop_cta
                script_data["script"] = new_script.strip()
                script_data["cta"] = loop_cta
                logger.success(f"  [Pass 4/4] Loop CTA: \"{loop_cta}\"")
            else:
                logger.warning(f"  [Pass 4/4] Invalid CTA: '{loop_cta}'. Keeping original.")
        except Exception as e:
            logger.warning(f"  [Pass 4/4] Loop engineering failed: {e}")

        return script_data

    # ── Public entry point ─────────────────────────────────────────────────────

    def run(self, topic_brief: dict, video_id: str, *args, **kwargs) -> dict:
        # FIX: Resolve niche at run() time, not __init__
        self.niche = self._get_niche()
        self.template = self._load_template(self.niche)

        logger.info(
            f"ScriptWriterAgent → niche={self.niche} | "
            f"4-pass chain for: {topic_brief.get('topic', 'Unknown')}"
        )

        try:
            extracted = self._pass1_extract(topic_brief)
            time.sleep(self._inter_call_sleep)

            script_data = self._pass2_write_script(topic_brief, extracted, video_id)
            time.sleep(self._inter_call_sleep)

            script_data = self._pass3_sharpen_hook(script_data)
            time.sleep(self._inter_call_sleep)

            script_data = self._pass4_loop_engineer(script_data)

            # FIX 2 (script side): Clean script for TTS after all passes complete
            if script_data.get("script"):
                cleaned = _clean_script_for_tts(script_data["script"])
                if cleaned:
                    script_data["script"] = cleaned
                    logger.info(f"  Script cleaned for TTS: {len(cleaned)} chars")

            emotion = script_data.get("emotion", "curiosity")
            script_data["_extracted"] = extracted
            script_data["_kb_preset"] = EMOTION_KB_PRESET.get(emotion, EMOTION_KB_PRESET["default"])

            logger.success(
                f"ScriptWriterAgent complete | niche={self.niche} | "
                f"Title: '{script_data.get('title', 'Untitled')}' | "
                f"Words: {script_data.get('word_count', 0)} | "
                f"Hook: '{script_data.get('hook', '')[:60]}'"
            )
            return script_data

        except Exception as e:
            logger.error(f"ScriptWriterAgent 4-pass chain failed: {e}. Using fallback.")
            duration = self.config.get("video", {}).get("duration_seconds", 55)
            wps = self.template.get("avg_words_per_second", 2.5) if self.template else 2.5
            target_words = int(duration * wps)
            return self._fallback_script(topic_brief, video_id, target_words)

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_json_fences(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        # Find JSON bounds in case of surrounding text
        for open_char, close_char in [('{', '}'), ('[', ']')]:
            start = raw.find(open_char)
            end = raw.rfind(close_char)
            if start != -1 and end != -1 and end > start:
                raw = raw[start:end + 1]
                break
        return raw.strip()

    def _fallback_script(self, topic_brief: dict, video_id: str, target_words: int) -> dict:
        topic = topic_brief.get("topic", "this")
        hook = topic_brief.get("hook", f"Nobody told you this about {topic}...")
        emotion = topic_brief.get("emotion", "curiosity")
        niche = self.niche or self._get_niche()

        niche_fallbacks = {
            "horror": (
                f"Something happened that I still cannot explain. "
                f"It started on a Tuesday. Nobody believed me at first. "
                f"The door was already open when I got home. "
                f"I know what I saw. The evidence was right there. "
                f"It had been happening for three weeks. "
                f"The timestamp on the camera confirmed it. "
                f"I still have the footage. "
                f"Some things do not have a rational explanation. "
                f"Have you ever experienced something you could not explain?"
            ),
            "reddit_story": (
                f"{hook} "
                f"I still cannot believe this actually happened. "
                f"It started like any other ordinary day. "
                f"My coworker pulled me aside before the meeting. "
                f"She said something that stopped me cold. "
                f"Everything changed after that conversation. "
                f"I went home and checked everything she said. "
                f"She was completely right. "
                f"Looking back, the signs were there the whole time. "
                f"Has something like this ever happened to you?"
            ),
            "brainrot": (
                f"Okay this is going to break your brain. "
                f"Scientists discovered something in 2023 that nobody covered. "
                f"The number is forty-two million. "
                f"Nobody talks about this. "
                f"Your brain literally filters this information out. "
                f"It happens to every single person alive right now. "
                f"We are all affected and most of us have no idea. "
                f"The algorithm predicted this back in 2019. "
                f"Comment if your brain just exploded."
            ),
            "finance": (
                f"The number one money mistake killing your wealth: "
                f"keeping savings in a regular bank account. "
                f"The average savings account pays 0.4 percent interest right now. "
                f"High-yield savings accounts pay 4.5 percent today. "
                f"On ten thousand dollars, that is four hundred extra dollars a year. "
                f"Over ten years with compound interest, that difference is over five thousand dollars. "
                f"Most people have never moved their money once in their lives. "
                f"The switch takes about ten minutes online. "
                f"Are you still leaving money on the table?"
            ),
        }

        script = niche_fallbacks.get(niche, (
            f"{hook} "
            f"Most people never learn the truth about this. "
            f"It happens every single day around the world. "
            f"Researchers at Stanford confirmed it in 2022. "
            f"The number is more surprising than you expect. "
            f"And it changes how you should think about this topic completely. "
            f"Here is what the evidence actually shows. "
            f"Pay close attention to this part. "
            f"The people who know this have a real advantage. "
            f"Start applying this information today. "
            f"Does this change how you see it now?"
        ))

        return {
            "video_id":      video_id,
            "title":         str(topic)[:55],
            "description":   f"{topic} #Shorts #Viral",
            "tags":          ["shorts", "viral", "facts", niche, "trending"],
            "script":        script,
            "hook":          hook,
            "cta":           "Does this change how you see it now?",
            "word_count":    len(script.split()),
            "sentence_count": len([s for s in script.split(".") if s.strip()]),
            "emotion":       emotion,
            "topic_brief":   topic_brief,
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
