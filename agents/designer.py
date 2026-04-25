"""
Designer agent: turns the approved draft into a detailed image prompt and
generates a hero image via a high-end text-to-image model.

Two stages:
    1. _build_prompt_llm() — `fast` tier LLM (gpt-4o-mini / qwen-turbo) parses
       the draft and emits {"image_prompt": "..."} JSON.
    2. _generate_image()    — calls the actual T2I service (Replicate Flux-2-Pro
       or Vertex AI Imagen-4-Ultra). Stubbed for now.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from prompts.designer_prompt import DESIGNER_SYSTEM_PROMPT, DESIGNER_USER_TEMPLATE


MOCK_IMAGE_URL = (
    "https://dummyimage.com/600x400/000/fff&text=Tsundere+Raccoon"
)


# =============================================================================
# Prompt-generation LLM (cheap/fast tier)
# =============================================================================


def _build_prompt_llm() -> ChatOpenAI:
    """
    Use the `fast` tier (gpt-4o-mini by default) for prompt extraction.
    Falls back to the Qwen DashScope endpoint if no OpenAI key is set, so
    this keeps working in either provider environment.
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        model = os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini")
        return ChatOpenAI(model=model, temperature=0.4, api_key=openai_key)

    qwen_key = os.getenv("QWEN_API_KEY")
    base_url = os.getenv("QWEN_BASE_URL")
    model = os.getenv("QWEN_FAST_MODEL_NAME", "qwen-turbo")
    kwargs: dict[str, Any] = {"model": model, "temperature": 0.4}
    if qwen_key:
        kwargs["api_key"] = qwen_key
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_image_prompt(raw: str, fallback: str) -> str:
    """Parse `{"image_prompt": "..."}` from the LLM output, tolerantly."""
    cleaned = _strip_markdown_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return fallback
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return fallback

    prompt = data.get("image_prompt") if isinstance(data, dict) else None
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    return fallback


# =============================================================================
# Image generation (stubbed — see TODO for real wiring)
# =============================================================================


def _generate_image(prompt: str) -> str:
    """
    Generate an image and return its URL.

    Currently returns a deterministic mock URL so the pipeline runs end-to-end
    without burning provider credits.

    TODO — wire up a real text-to-image model:

    Option A) Replicate (Flux-2-Pro):
        ```
        import requests, time
        token = os.getenv("REPLICATE_API_TOKEN")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = {
            # pin to the model version you want; query Replicate for current id
            "version": "<flux-2-pro-version-id>",
            "input": {
                "prompt": prompt,
                "aspect_ratio": "4:5",
                "output_format": "png",
                "safety_tolerance": 2,
            },
        }
        r = requests.post(
            "https://api.replicate.com/v1/predictions",
            headers=headers, json=body, timeout=30,
        )
        r.raise_for_status()
        prediction = r.json()
        # poll prediction["urls"]["get"] until status == "succeeded",
        # then return prediction["output"][0]
        ```

    Option B) Google Vertex AI (Imagen-4-Ultra):
        ```
        from google.auth import default
        from google.auth.transport.requests import Request
        creds, project = default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(Request())
        location = os.getenv("VERTEX_LOCATION", "us-central1")
        endpoint = (
            f"https://{location}-aiplatform.googleapis.com/v1/projects/"
            f"{project}/locations/{location}/publishers/google/models/"
            f"imagen-4.0-ultra-generate-preview-06-06:predict"
        )
        body = {
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1, "aspectRatio": "4:5"},
        }
        r = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {creds.token}"},
            json=body, timeout=60,
        )
        r.raise_for_status()
        # response contains base64 png in predictions[0].bytesBase64Encoded —
        # upload to your CDN/bucket and return that public URL.
        ```

    Both should be wrapped in try/except and fall back to MOCK_IMAGE_URL on
    failure so the pipeline never hard-stops on a transient provider error.
    """
    return MOCK_IMAGE_URL


# =============================================================================
# Node
# =============================================================================


def designer_node(state: dict[str, Any]) -> dict[str, Any]:
    draft = state.get("draft") or ""
    if not draft:
        # Nothing to visualize — emit a placeholder and move on.
        return {
            "image_url": MOCK_IMAGE_URL,
            "image_prompt": "(no draft available)",
            "history": [{"node": "designer", "skipped": True}],
        }

    app_context = state.get("app_context") or {}
    target_region = state.get("target_region") or {}
    sub_regions = target_region.get("sub_regions") or []
    sub_regions_str = ", ".join(sub_regions) if sub_regions else "general area"

    system_prompt = DESIGNER_SYSTEM_PROMPT.format(
        app_name=app_context.get("app_name", "the app"),
        target_region_label=target_region.get("label", "this region"),
        sub_regions=sub_regions_str,
    )
    user_msg = DESIGNER_USER_TEMPLATE.format(draft=draft)

    fallback = (
        f"A stylish tsundere raccoon mascot wearing sunglasses and a streetwear "
        f"hoodie, standing confidently in {target_region.get('label', 'LA')}, "
        f"warm cinematic dusk light, editorial illustration, 35mm, shallow DOF."
    )

    try:
        llm = _build_prompt_llm()
        response = llm.invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_msg)]
        )
        raw = getattr(response, "content", str(response))
        image_prompt = _parse_image_prompt(raw, fallback=fallback)
    except Exception as e:  # network / auth / quota — keep pipeline alive
        image_prompt = fallback
        return {
            "image_url": MOCK_IMAGE_URL,
            "image_prompt": image_prompt,
            "history": [
                {"node": "designer", "error": f"prompt-llm failed: {e!r}"}
            ],
        }

    try:
        image_url = _generate_image(image_prompt)
    except Exception as e:
        image_url = MOCK_IMAGE_URL
        return {
            "image_url": image_url,
            "image_prompt": image_prompt,
            "history": [
                {"node": "designer", "error": f"image-gen failed: {e!r}"}
            ],
        }

    return {
        "image_url": image_url,
        "image_prompt": image_prompt,
        "history": [
            {
                "node": "designer",
                "image_url": image_url,
                "prompt_chars": len(image_prompt),
            }
        ],
    }
