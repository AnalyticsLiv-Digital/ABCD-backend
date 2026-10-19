# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Python port of GenerationHelper.generateVariants() from
# google-marketing-solutions/vigenair ui/src/generation.ts @ 3d70141 (shortening mode only),
# with the model settings from ui/src/config.ts.
"""Ask Gemini for shorter cuts of an analysed video."""
import logging
import re
import time
from typing import Dict, List

from config import settings
from services.vigenair.prompts import ABCD_OBJECTIVES, GENERATION_PROMPT

_log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
DEFAULT_VIDEO_LANGUAGE = "English"

_VARIANT_RE = re.compile(
    r".*Title\s?:\**\s*(?P<title>.*?)\n+\**Scenes\s?:\**\s*(?P<scenes>.*?)\n+"
    r"\**Reasoning\s?:\**\s*(?P<description>.*?)\n+\**Score\s?:\**\s*(?P<score>.*?)\n+"
    r"\**Duration\s?:\**\s*(?P<duration>.*?)\n+\**ABCD\s?:\**\s*\n*(?P<reasoning>.*)",
    re.I | re.M | re.S,
)

_genai_client = None


class GenerationError(RuntimeError):
    """Gemini didn't return any usable variant."""


def _client():
    global _genai_client
    if _genai_client is None:
        from google import genai

        _genai_client = genai.Client(
            vertexai=True, project=settings.VIGENAIR_PROJECT_ID, location=settings.VIGENAIR_LOCATION
        )
    return _genai_client


def _generate(prompt: str) -> str:
    from google.genai import types

    config = types.GenerateContentConfig(
        temperature=1,
        top_p=1,
        max_output_tokens=8192,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        safety_settings=[
            types.SafetySetting(category=c, threshold="BLOCK_ONLY_HIGH")
            for c in (
                "HARM_CATEGORY_DANGEROUS_CONTENT",
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            )
        ],
    )
    for attempt in range(4):
        try:
            resp = _client().models.generate_content(
                model=settings.VIGENAIR_GEMINI_MODEL, contents=prompt, config=config
            )
            return resp.text or ""
        except Exception as exc:
            # Quota (429) → wait and retry, as upstream does (10 s)
            if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                time.sleep(10 * (attempt + 1))
                continue
            raise
    return ""


def _video_script(segments: List[dict], max_scene_s: float) -> str:
    lines: List[str] = []
    for seg in segments:
        if seg["duration_s"] > max_scene_s:
            continue
        lines.append(f"Scene {seg['av_segment_id']}")
        lines.append(f"{seg['start_s']} --> {seg['end_s']}")
        lines.append(f"Duration: {seg['end_s'] - seg['start_s']:.2f}s")
        if seg.get("description"):
            lines.append(f"Description: {seg['description'].strip()}")
        lines.append(f"Number of visual shots: {len(seg.get('visual_segment_ids') or [])}")
        details = (seg.get("labels") or []) + (seg.get("objects") or [])
        text = [f'"{t}"' for t in (seg.get("text") or [])]
        lines.append(f'Off-screen speech: "{" ".join(seg.get("transcript") or [])}"')
        lines.append(f"On-screen details: {', '.join(details)}")
        lines.append(f"On-screen text: {', '.join(text)}")
        lines.append(f"Logos: {', '.join(seg.get('logos') or [])}")
        if seg.get("keywords"):
            lines.append(f"Keywords: {seg['keywords'].strip()}")
        lines.append("")
    return "\n".join(lines)


def _js_num(x: float) -> str:
    """Format like JavaScript's String(number): 24.0 → '24', 19.2 → '19.2'."""
    return str(int(x)) if float(x).is_integer() else repr(float(x))


def build_prompt(segments: List[dict], language: str, target_s: float, brief: str, objective: str) -> str:
    duration_range = f"{_js_num(target_s - target_s * 0.2)}-{_js_num(target_s + target_s * 0.2)}"
    # JS String.replace with a string pattern replaces the first occurrence only — keep that.
    return (
        GENERATION_PROMPT
        .replace("{{{{userPrompt}}}}", brief or "", 1)
        .replace("{{{{generationEvalPromptPart}}}}", ABCD_OBJECTIVES[objective]["prompt_part"], 1)
        .replace("{{{{desiredDuration}}}}", _js_num(target_s), 1)
        .replace("{{{{expectedDurationRange}}}}", duration_range, 1)
        .replace("{{{{videoLanguage}}}}", language or DEFAULT_VIDEO_LANGUAGE, 1)
        .replace("{{{{videoScript}}}}", _video_script(segments, target_s), 1)
    )


def _parse(response: str, by_id: Dict[str, dict], all_ids: List[str]) -> List[dict]:
    variants: List[dict] = []
    for index, chunk in enumerate(c for c in response.split("## Combination") if c):
        m = _VARIANT_RE.match(chunk)
        if not m:
            _log.info("Variant #%d didn't match the expected format", index + 1)
            continue
        raw = [s.strip().lower().replace("scene ", "").replace(".0", "") for s in m["scenes"].strip().split(",")]
        scenes = sorted({s for s in raw if s in by_id}, key=lambda s: float(s))
        if len(scenes) < 1 or scenes == all_ids:
            continue  # empty, or the full video (not a shorter cut)
        try:
            score = float(re.findall(r"[\d.]+", m["score"])[0])
        except (IndexError, ValueError):
            score = 0.0
        variants.append({
            "title": m["title"].strip().strip("*").strip(),
            "scenes": scenes,
            "description": m["description"].strip(),
            "score": score,
            "abcd": m["reasoning"].strip(),
            "duration_s": round(sum(by_id[s]["end_s"] - by_id[s]["start_s"] for s in scenes), 2),
        })
    return variants


def generate_variants(segments: List[dict], language: str, target_s: float, brief: str, objective: str) -> List[dict]:
    """
    Returns Gemini's suggested cuts, closest to the target length first, then by score.
    Each item: {title, scenes: [ids], description, score, max_score, abcd, duration_s}.
    """
    if objective not in ABCD_OBJECTIVES:
        objective = "awareness"
    by_id = {s["av_segment_id"]: s for s in segments}
    all_ids = sorted(by_id, key=lambda s: float(s))
    prompt = build_prompt(segments, language, target_s, brief, objective)

    variants: List[dict] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = _generate(prompt)
        variants = _parse(response, by_id, all_ids)
        _log.info("Variant generation attempt %d/%d: %d usable", attempt, MAX_ATTEMPTS, len(variants))
        if variants:
            break
    if not variants:
        raise GenerationError("Couldn't generate suggestions for this video. Try again or change the brief.")

    max_score = ABCD_OBJECTIVES[objective]["max_score"]
    for v in variants:
        v["max_score"] = max_score
    variants.sort(key=lambda v: (abs(target_s - v["duration_s"]), -v["score"]))
    return variants
