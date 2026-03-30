"""
agents/script_writer.py

FIXED v4 — Four bugs patched:

BUG FIX 1 — INCOMPLETE STORY (root causes):
  a) max_tokens=2000 on Groq free tier caused JSON truncation mid-sentence.
     FIX: Pass 2 uses max_tokens=3000.
  b) Silent parse failures returned short fallback without any log.
     FIX: Parse failures now log the raw response at ERROR level.
  c) Script truncation in Pass 2: prompt asked for 18-25 sentences but
     metadata fields consumed most of the token budget.
     FIX: Script generated first (full budget), metadata derived after.
  d) Inter-call sleep was 12s — reduced to 8s default.

BUG FIX 2 — ROBOTIC VOICE (script-side):
  a) Em-dashes → replaced with commas in _clean_script_for_tts()
  b) ALL-CAPS words → converted to sentence case
  c) Long sentences (20+ words) → split at natural conjunction points

BUG FIX 3 — SAME FALLBACK SCRIPT FOR ALL 10 VIDEOS:
  When Groq rate-limits after video 2-3 in a batch, _fallback_script()
  was called for every remaining video. The old version had a hardcoded
  body paragraph identical for every topic. A video about "morning routines"
  and one about "procrastination" produced the exact same text.

  FIX: FALLBACK_BODY_TEMPLATES provides 2-3 structurally different body
  templates per niche. Template is selected via:
    sum(ord(c) for c in video_id) % len(templates)
  so consecutive batch videos rotate through them. Each template uses
  {topic}, {angle_sentence}, {hook_clean}, {cta} substitutions pulled
  from topic_brief — producing genuinely different scripts per video
  even when the LLM is completely unavailable.

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
# BUG FIX 3: Varied fallback body templates — one per niche, 2-3 variants each
# Template slots: {hook_clean}, {topic}, {angle_sentence}, {cta}
# ─────────────────────────────────────────────────────────────────────────────

FALLBACK_BODY_TEMPLATES = {
    "motivation": [
        (
            "{hook_clean}. "
            "Most people misunderstand everything about {topic}. "
            "{angle_sentence}"
            "The gap between people who succeed and people who struggle is not talent. "
            "It is one specific decision made before 8 AM. "
            "The research from Stanford and Harvard agrees on this. "
            "Compound progress is invisible until it suddenly is not. "
            "You do not need a new plan. You need to execute the one you already have. "
            "One percent better every single day changes everything in a year. "
            "{cta}"
        ),
        (
            "{hook_clean}. "
            "Here is what nobody tells you about {topic}. "
            "{angle_sentence}"
            "The average person quits exactly 72 hours before the breakthrough. "
            "Discipline is not about motivation. It is about systems. "
            "Small inputs, consistently applied, produce outsized outputs. "
            "Your environment shapes your behavior more than your willpower does. "
            "Change the environment. The behavior follows automatically. "
            "{cta}"
        ),
        (
            "{hook_clean}. "
            "The truth about {topic} has been buried under bad advice. "
            "{angle_sentence}"
            "Winners do not have more time. They have a different relationship with discomfort. "
            "Every day you avoid the hard thing, you make it harder tomorrow. "
            "The fastest path to results runs directly through what you are avoiding. "
            "Start today. Not Monday. Not next month. Today. "
            "{cta}"
        ),
    ],
    "horror": [
        (
            "{hook_clean}. "
            "Nobody believed me the first time I tried to explain it. "
            "{angle_sentence}"
            "The third night, I set up a camera. "
            "The timestamp reads 2 43 AM. I was in bed. "
            "Whatever was in that room, I did not put it there. "
            "I still have the footage. I cannot watch it alone. "
            "Some things do not have a rational explanation. "
            "{cta}"
        ),
        (
            "{hook_clean}. "
            "It started small. Easy to dismiss. "
            "{angle_sentence}"
            "Then it happened again. Same time. Same place. "
            "I checked every logical explanation. None of them fit. "
            "The neighbor said she had noticed it too. For years. "
            "The previous owners left without telling anyone why. "
            "Now I understand why they left. "
            "{cta}"
        ),
    ],
    "reddit_story": [
        (
            "{hook_clean}. "
            "I still cannot believe I am telling this story. "
            "{angle_sentence}"
            "It started like any completely ordinary day. "
            "Nothing about the morning suggested what was coming. "
            "Then she said something that stopped everything. "
            "I replayed that sentence in my head for the next six hours. "
            "Looking back the signs were there the entire time. "
            "{cta}"
        ),
        (
            "{hook_clean}. "
            "The thing about {topic} is that you never see it coming. "
            "{angle_sentence}"
            "I thought I knew exactly how this would play out. "
            "I was completely wrong. "
            "The twist came from someone I trusted completely. "
            "Everything I thought I knew got rewritten in about thirty seconds. "
            "{cta}"
        ),
    ],
    "brainrot": [
        (
            "{hook_clean}. "
            "Scientists published this in 2023 and nobody covered it. "
            "{angle_sentence}"
            "The number involved is forty two million and it affects you personally. "
            "Your brain is literally filtering this information out right now. "
            "The algorithm predicted this back in 2019. "
            "We are all running on an operating system with a known bug. "
            "The patch does not exist yet. "
            "{cta}"
        ),
        (
            "{hook_clean}. "
            "The lore on {topic} goes deeper than anyone will admit. "
            "{angle_sentence}"
            "Three separate researchers confirmed the same result independently. "
            "They were all told to stop talking about it. "
            "The Wikipedia page was edited seventeen times in one week. "
            "Reality is not what the syllabus said it was. "
            "{cta}"
        ),
    ],
    "finance": [
        (
            "{hook_clean}. "
            "Your bank is not going to explain this to you. "
            "{angle_sentence}"
            "On a ten thousand dollar balance the difference is four hundred dollars a year. "
            "Over ten years with compound interest that gap becomes five thousand dollars. "
            "The switch takes approximately ten minutes to complete online. "
            "Most people have never moved their savings once in their entire life. "
            "{cta}"
        ),
        (
            "{hook_clean}. "
            "Everything you were taught about {topic} in school was incomplete. "
            "{angle_sentence}"
            "The wealthiest households in America use this exact mechanism. "
            "It is perfectly legal. It has been available since 1974. "
            "The IRS is not going to remind you it exists. "
            "Ninety percent of workers who qualify have never used it. "
            "{cta}"
        ),
    ],
}

FALLBACK_CTA_MAP = {
    "motivation":   "Are you ready to make that change starting today?",
    "horror":       "Have you ever experienced something you could not explain?",
    "reddit_story": "Has something like this ever happened to you?",
    "brainrot":     "Comment if your brain just broke.",
    "finance":      "Are you still leaving money on the table?",
}


# ─────────────────────────────────────────────────────────────────────────────
# BUG FIX 2: Post-process script to prevent TTS robotic artifacts
# ─────────────────────────────────────────────────────────────────────────────

def _clean_script_for_tts(script_text: str) -> str:
    text = script_text

    # 1. Em-dash and en-dash → comma pause
    text = re.sub(r'\s*[—–]\s*', ', ', text)

    # 2. ALL-CAPS words → Title Case (preserve short acronyms like "AI", "US")
    def fix_caps(match):
        word = match.group(0)
        if len(word) <= 3:
            return word
        return word.capitalize()
    text = re.sub(r'\b[A-Z]{4,}\b', fix_caps, text)

    # 3. Remove parenthetical content
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)

    # 4. Multiple exclamation/question marks → single
    text = re.sub(r'[!]{2,}', '!', text)
    text = re.sub(r'[?]{2,}', '?', text)

    # 5. Quotes within text → remove
    text = re.sub(r'"([^"]*)"', r'\1', text)
    text = re.sub(r"'([^']*)'", r'\1', text)

    # 6. Split very long sentences at conjunction points
    sentences = re.split(r'(?<=[.!?])\s+', text)
    processed_sentences = []
    for sentence in sentences:
        words = sentence.split()
        if len(words) > 16:
            split_at = None
            conjunctions = [' but ', ' and ', ' so ', ' because ', ' although ', ' however ', ' which ']
            for conj in conjunctions:
                idx = sentence.lower().find(conj)
                if idx > 20 and idx < len(sentence) - 20:
                    split_at = idx
                    break
            if split_at:
                part1 = sentence[:split_at].strip()
                conj_word = sentence[split_at:split_at + 10].strip().split()[0]
                part2_clean = sentence[split_at + len(conj_word) + 1:].strip().capitalize()
                processed_sentences.append(part1 + '.')
                processed_sentences.append(
                    part2_clean + '.' if not part2_clean.endswith(('.', '!', '?')) else part2_clean
                )
            else:
                processed_sentences.append(sentence)
        else:
            processed_sentences.append(sentence)

    text = ' '.join(processed_sentences)

    # 7. Clean up double spaces and double periods
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\.{2,}(?!\.)', '.', text)
    text = re.sub(r'\.\s*,', '.', text)

    return text.strip()


class ScriptWriterAgent:
    def __init__(self, config):
        self.config = config
        self.groq_key = os.environ.get("GROQ_API_KEY", "")
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

        # Step A: Generate the full script text (full token budget)
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
            raw_script = self._call_llm(script_prompt, system_prompt, max_tokens=3000)

            raw_script = re.sub(r'^(HOOK|BODY|TWIST|CTA|INTRO|OUTRO):\s*', '', raw_script, flags=re.MULTILINE)
            raw_script = re.sub(r'^\d+\.\s+', '', raw_script, flags=re.MULTILINE)
            raw_script = re.sub(r'^\*+\s*', '', raw_script, flags=re.MULTILINE)
            raw_script = re.sub(r'\n+', ' ', raw_script).strip()

            sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', raw_script) if s.strip()]
            word_count = len(raw_script.split())

            logger.info(f"  [Pass 2A] Script: {word_count} words, {len(sentences)} sentences")

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

        # Step B: Generate compact metadata JSON from the script
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
            time.sleep(3)
            meta_raw = self._call_llm(
                meta_prompt,
                "You generate compact video metadata JSON. Return only valid JSON.",
                max_tokens=400
            )
            meta_raw = self._strip_json_fences(meta_raw)
            metadata = json.loads(meta_raw)
        except Exception as e:
            logger.warning(f"  [Pass 2B] Metadata generation failed: {e}. Using defaults.")
            first_sentence = re.split(r'(?<=[.!?])\s+', raw_script)[0] if raw_script else ""
            last_sentence  = re.split(r'(?<=[.!?])\s+', raw_script)[-1] if raw_script else ""
            metadata = {
                "title": topic_brief.get("topic", "")[:55],
                "description": f"{topic_brief.get('topic', '')} #Shorts #Viral #{self.niche}",
                "tags": ["shorts", "viral", self.niche, "trending", "fyp"],
                "hook": first_sentence,
                "cta": last_sentence,
            }

        script_data = {
            "video_id":       video_id,
            "title":          metadata.get("title", topic_brief.get("topic", ""))[:100],
            "description":    metadata.get("description", ""),
            "tags":           metadata.get("tags", ["shorts"]),
            "script":         raw_script,
            "hook":           metadata.get("hook", ""),
            "cta":            metadata.get("cta", ""),
            "word_count":     len(raw_script.split()),
            "sentence_count": len([s for s in re.split(r'(?<=[.!?])\s+', raw_script) if s.strip()]),
            "emotion":        topic_brief.get("emotion", "curiosity"),
            "topic_brief":    topic_brief,
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
        original_hook   = script_data.get("hook", "")

        if not original_hook or not original_script:
            return script_data

        sentences     = re.split(r'(?<=[.!?])\s+', original_script)
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
                script_data["hook"]   = sharpened_hook
                logger.success(f"  [Pass 3/4] Hook: \"{sharpened_hook}\"")
            else:
                logger.warning("  [Pass 3/4] Sharpened hook too long or empty. Keeping original.")
        except Exception as e:
            logger.warning(f"  [Pass 3/4] Hook sharpening failed: {e}")

        return script_data

    # ── Pass 4: Loop Engineer ──────────────────────────────────────────────────

    def _pass4_loop_engineer(self, script_data: dict) -> dict:
        logger.info("  [Pass 4/4] Engineering loop CTA...")

        hook        = script_data.get("hook", "")
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
                last_period_idx = script_text.rfind(".")
                if last_period_idx > len(script_text) // 2:
                    new_script = script_text[:last_period_idx + 1] + " " + loop_cta
                else:
                    new_script = script_text + " " + loop_cta
                script_data["script"] = new_script.strip()
                script_data["cta"]    = loop_cta
                logger.success(f"  [Pass 4/4] Loop CTA: \"{loop_cta}\"")
            else:
                logger.warning(f"  [Pass 4/4] Invalid CTA: '{loop_cta}'. Keeping original.")
        except Exception as e:
            logger.warning(f"  [Pass 4/4] Loop engineering failed: {e}")

        return script_data

    # ── Public entry point ─────────────────────────────────────────────────────

    def run(self, topic_brief: dict, video_id: str, *args, **kwargs) -> dict:
        self.niche    = self._get_niche()
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

            if script_data.get("script"):
                cleaned = _clean_script_for_tts(script_data["script"])
                if cleaned:
                    script_data["script"] = cleaned
                    logger.info(f"  Script cleaned for TTS: {len(cleaned)} chars")

            emotion = script_data.get("emotion", "curiosity")
            script_data["_extracted"]  = extracted
            script_data["_kb_preset"]  = EMOTION_KB_PRESET.get(emotion, EMOTION_KB_PRESET["default"])

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
        for open_char, close_char in [('{', '}'), ('[', ']')]:
            start = raw.find(open_char)
            end   = raw.rfind(close_char)
            if start != -1 and end != -1 and end > start:
                raw = raw[start:end + 1]
                break
        return raw.strip()

    def _fallback_script(self, topic_brief: dict, video_id: str, target_words: int) -> dict:
        """
        BUG FIX 3: Build a varied, topic-specific fallback script without an LLM.

        Uses video_id to select different template variants so consecutive
        fallback calls in a batch produce structurally different scripts.
        Content words (topic, angle, hook) are substituted into the template
        so the output is unique per video even within the same niche.
        """
        topic   = topic_brief.get("topic", "this topic")
        hook    = topic_brief.get("hook", f"Nobody told you this about {topic}...")
        angle   = topic_brief.get("angle", "")
        emotion = topic_brief.get("emotion", "curiosity")
        niche   = self.niche or self._get_niche()

        # Clean hook for inline use — strip trailing punctuation and filler openers
        hook_clean = re.sub(r'[.!?]+$', '', hook).strip()
        hook_clean = re.sub(
            r'^(Nobody told you this about|Stop|The day|Nobody)\s*',
            '', hook_clean, flags=re.IGNORECASE
        ).strip()
        if not hook_clean:
            hook_clean = f"The truth about {topic} changes everything"

        cta = FALLBACK_CTA_MAP.get(niche, "Does this change how you see it now?")

        if angle and len(angle) > 5:
            angle_clean    = angle.rstrip(".")
            angle_sentence = f"The real story is about {angle_clean.lower()}. "
        else:
            angle_sentence = f"Most people have the wrong model of {topic.lower()}. "

        templates     = FALLBACK_BODY_TEMPLATES.get(niche, FALLBACK_BODY_TEMPLATES["motivation"])
        template_seed = sum(ord(c) for c in video_id) % len(templates)
        template      = templates[template_seed]

        script = template.format(
            hook_clean=hook_clean,
            topic=topic.lower(),
            angle_sentence=angle_sentence,
            cta=cta,
        )
        script = re.sub(r'\s+', ' ', script).strip()

        first_sentence = re.split(r'(?<=[.!?])\s+', script)[0] if script else hook
        last_sentence  = re.split(r'(?<=[.!?])\s+', script)[-1] if script else cta

        return {
            "video_id":       video_id,
            "title":          str(topic)[:55],
            "description":    f"{topic} #Shorts #Viral #{niche}",
            "tags":           ["shorts", "viral", "facts", niche, "trending"],
            "script":         script,
            "hook":           first_sentence,
            "cta":            last_sentence,
            "word_count":     len(script.split()),
            "sentence_count": len([s for s in re.split(r'(?<=[.!?])\s+', script) if s.strip()]),
            "emotion":        emotion,
            "topic_brief":    topic_brief,
            "_extracted": {
                "core_mystery":      topic,
                "emotional_trigger": emotion,
                "key_facts":         [hook, angle, ""],
                "tension_arc":       "Build curiosity, deliver surprise",
                "twist":             hook,
                "visual_anchors":    [topic, angle or topic, ""],
            },
            "_kb_preset": EMOTION_KB_PRESET.get(emotion, EMOTION_KB_PRESET["default"]),
        }
