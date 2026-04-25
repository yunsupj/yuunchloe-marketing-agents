"""
Designer agent: turns the approved draft into (a) a detailed image prompt and
(b) a short Korean overlay text. Generates a hero photo via Replicate
(Flux-2-Pro) or Vertex AI (Imagen-4-Ultra) on a 50/50 A/B split, then
composites the overlay text onto the photo with PIL — card-news style.

Stages:
    1. _build_prompt_llm()    — `fast` LLM extracts {image_prompt, overlay_text}.
    2. _generate_image()      — A/B between Flux-2-Pro and Imagen-4-Ultra.
    3. _apply_text_overlay()  — PIL composites Korean text on negative space.
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import requests
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from prompts.designer_prompt import DESIGNER_SYSTEM_PROMPT, DESIGNER_USER_TEMPLATE


MOCK_IMAGE_URL = (
    "https://dummyimage.com/600x400/000/fff&text=Local+Photo+Placeholder"
)
MOCK_MODEL_NAME = "mock"

FLUX_MODEL = "flux-2-pro"
IMAGEN_MODEL = "imagen-4-ultra"

# Korean-supporting font. Default is Noto Sans KR (variable TTF) from the
# google/fonts repo — a reliable public mirror. Both the URL and the local
# cache path are env-overridable.
DEFAULT_KOREAN_FONT_URL = (
    "https://github.com/google/fonts/raw/main/ofl/notosanskr/"
    "NotoSansKR%5Bwght%5D.ttf"
)
DEFAULT_FONT_CACHE_DIR = Path.home() / ".cache" / "yc-marketing"
DEFAULT_FONT_FILENAME = "NotoSansKR.ttf"


# =============================================================================
# Prompt-generation LLM (cheap/fast tier)
# =============================================================================


def _build_prompt_llm() -> ChatOpenAI:
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


def _parse_designer_output(
    raw: str, fallback_prompt: str, fallback_overlay: str
) -> tuple[str, str]:
    """Parse `{image_prompt, overlay_text}` tolerantly."""
    cleaned = _strip_markdown_fences(raw)
    data: Any = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None

    if not isinstance(data, dict):
        return fallback_prompt, fallback_overlay

    prompt = data.get("image_prompt")
    overlay = data.get("overlay_text")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = fallback_prompt
    if not isinstance(overlay, str) or not overlay.strip():
        overlay = fallback_overlay
    return prompt.strip(), overlay.strip()


# =============================================================================
# Provider availability
# =============================================================================


def _has_replicate() -> bool:
    return bool(os.getenv("REPLICATE_API_TOKEN"))


def _has_vertex() -> bool:
    has_token = bool(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or os.getenv("VERTEX_ACCESS_TOKEN")
    )
    return has_token and bool(os.getenv("VERTEX_PROJECT_ID"))


# =============================================================================
# Replicate — Flux-2-Pro
# =============================================================================


def _call_replicate_flux(prompt: str) -> str:
    token = os.environ["REPLICATE_API_TOKEN"]
    model_slug = os.getenv("REPLICATE_FLUX_MODEL", "black-forest-labs/flux-2-pro")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "input": {
            "prompt": prompt,
            "aspect_ratio": "4:5",
            "output_format": "png",
            "safety_tolerance": 2,
        }
    }

    create = requests.post(
        f"https://api.replicate.com/v1/models/{model_slug}/predictions",
        headers=headers,
        json=body,
        timeout=30,
    )
    create.raise_for_status()
    prediction = create.json()
    get_url = prediction.get("urls", {}).get("get")
    if not get_url:
        raise RuntimeError(f"Replicate response missing urls.get: {prediction}")

    deadline = time.time() + 120
    while time.time() < deadline:
        poll = requests.get(get_url, headers=headers, timeout=15)
        poll.raise_for_status()
        data = poll.json()
        status = data.get("status")
        if status == "succeeded":
            output = data.get("output")
            if isinstance(output, list) and output:
                return output[0]
            if isinstance(output, str):
                return output
            raise RuntimeError(f"Replicate succeeded but no output: {data}")
        if status in ("failed", "canceled"):
            raise RuntimeError(
                f"Replicate prediction {status}: {data.get('error')}"
            )
        time.sleep(2)

    raise TimeoutError("Replicate prediction timed out after 120s")


# =============================================================================
# Vertex AI — Imagen-4-Ultra
# =============================================================================


def _vertex_access_token() -> str:
    explicit = os.getenv("VERTEX_ACCESS_TOKEN")
    if explicit:
        return explicit
    from google.auth import default
    from google.auth.transport.requests import Request

    creds, _ = default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    return creds.token


def _persist_vertex_image(b64_png: str) -> str:
    """
    For local dev we write to ./output/ and return a file:// URL — small,
    runnable, no extra deps. In production swap this for an upload to GCS
    (or S3 / Cloudflare R2) and return the public CDN URL.
    """
    png_bytes = base64.b64decode(b64_png)
    out_dir = Path(os.getenv("IMAGE_OUTPUT_DIR", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"imagen-{int(time.time() * 1000)}.png"
    path = out_dir / filename
    path.write_bytes(png_bytes)
    return path.resolve().as_uri()


def _call_vertex_imagen(prompt: str) -> str:
    project = os.environ["VERTEX_PROJECT_ID"]
    location = os.getenv("VERTEX_LOCATION", "us-central1")
    model_id = os.getenv("VERTEX_IMAGEN_MODEL", "imagen-4.0-ultra-generate-001")

    endpoint = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/"
        f"{project}/locations/{location}/publishers/google/models/"
        f"{model_id}:predict"
    )
    body = {
        "instances": [{"prompt": prompt}],
        "parameters": {
            "sampleCount": 1,
            "aspectRatio": "4:5",
            "safetySetting": "block_only_high",
        },
    }
    headers = {
        "Authorization": f"Bearer {_vertex_access_token()}",
        "Content-Type": "application/json",
    }

    resp = requests.post(endpoint, headers=headers, json=body, timeout=90)
    resp.raise_for_status()
    data = resp.json()

    predictions = data.get("predictions") or []
    if not predictions:
        raise RuntimeError(f"Vertex returned no predictions: {data}")
    b64 = predictions[0].get("bytesBase64Encoded")
    if not b64:
        raise RuntimeError(f"Vertex prediction missing bytes: {predictions[0]}")

    return _persist_vertex_image(b64)


# =============================================================================
# A/B selection
# =============================================================================


_MODEL_CALLERS = {
    FLUX_MODEL: (_has_replicate, _call_replicate_flux),
    IMAGEN_MODEL: (_has_vertex, _call_vertex_imagen),
}


def _generate_image(prompt: str) -> tuple[str, str]:
    """
    Returns `(image_url, model_name)`.

    50/50 A/B between the two providers, with credential-aware and
    runtime-error fallback to the other arm. Returns the mock URL only if
    both arms are unavailable.
    """
    primary = random.choice([FLUX_MODEL, IMAGEN_MODEL])
    secondary = IMAGEN_MODEL if primary == FLUX_MODEL else FLUX_MODEL

    last_error: Exception | None = None
    for model in (primary, secondary):
        has_creds, caller = _MODEL_CALLERS[model]
        if not has_creds():
            continue
        try:
            url = caller(prompt)
            return url, model
        except Exception as e:
            last_error = e
            print(f"[Designer] {model} failed: {e!r} — trying fallback.")
            continue

    if last_error is not None:
        print(
            f"[Designer] Both T2I models unavailable. Last error: {last_error!r}"
        )
    else:
        print("[Designer] No T2I credentials configured. Using mock image.")
    return MOCK_IMAGE_URL, MOCK_MODEL_NAME


# =============================================================================
# Korean font download (cached)
# =============================================================================


def _resolve_font_path() -> Path:
    explicit = os.getenv("KOREAN_FONT_PATH")
    if explicit:
        return Path(explicit)
    cache_dir = Path(os.getenv("FONT_CACHE_DIR", str(DEFAULT_FONT_CACHE_DIR)))
    return cache_dir / DEFAULT_FONT_FILENAME


def _download_font(font_path: Path) -> Path:
    """
    Ensure a Korean-supporting TTF lives at `font_path`. Download Noto Sans KR
    from a public mirror if it's not already cached. Returns the path.

    PIL's default fonts do NOT support Korean Hangul, so this step is required
    before any text overlay can render correctly.
    """
    if font_path.is_file() and font_path.stat().st_size > 0:
        return font_path

    url = os.getenv("KOREAN_FONT_URL", DEFAULT_KOREAN_FONT_URL)
    font_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[Designer] Downloading Korean font: {url}")
    resp = requests.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    with font_path.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
    return font_path


# =============================================================================
# Supabase Storage upload (final hosting for the overlaid image)
# =============================================================================


SUPABASE_BUCKET = "marketing-assets"


def _build_storage_client():
    """Lazy supabase client. Returns None when package or env vars missing."""
    try:
        from supabase import create_client
    except ImportError:
        print("[Designer] supabase-py not installed — skipping upload.")
        return None

    url = os.getenv("EXPO_PUBLIC_SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("EXPO_PUBLIC_SUPABASE_ANON_KEY")
    )
    if not url or not key:
        print("[Designer] Supabase env vars missing — skipping upload.")
        return None

    try:
        return create_client(url, key)
    except Exception as e:
        print(f"[Designer] Supabase client init failed: {e!r}")
        return None


def _upload_to_supabase(png_bytes: bytes) -> str | None:
    """
    Upload PNG bytes to the `marketing-assets` bucket and return the public
    HTTPS URL. Returns None on any failure so the caller can fall back to
    the raw image URL.
    """
    client = _build_storage_client()
    if client is None:
        return None

    bucket_name = os.getenv("SUPABASE_MARKETING_BUCKET", SUPABASE_BUCKET)
    filename = f"post_{int(time.time())}.png"

    try:
        bucket = client.storage.from_(bucket_name)
        bucket.upload(
            path=filename,
            file=png_bytes,
            file_options={"content-type": "image/png", "upsert": "true"},
        )
        public_url = bucket.get_public_url(filename)
    except Exception as e:
        print(f"[Designer] Supabase upload failed: {e!r} — using raw image URL.")
        return None

    if not isinstance(public_url, str) or not public_url.startswith("http"):
        print(f"[Designer] Unexpected public_url shape: {public_url!r}")
        return None
    return public_url


# =============================================================================
# Text overlay
# =============================================================================


def _load_image_bytes(image_url_or_path: str) -> bytes:
    """Read image bytes from an http(s) URL, file:// URI, or local path."""
    if image_url_or_path.startswith(("http://", "https://")):
        resp = requests.get(image_url_or_path, timeout=30)
        resp.raise_for_status()
        return resp.content

    if image_url_or_path.startswith("file://"):
        parsed = urlparse(image_url_or_path)
        local_path = Path(url2pathname(parsed.path))
    else:
        local_path = Path(image_url_or_path)

    return local_path.read_bytes()


def _wrap_text_to_width(
    text: str, font, max_width: int
) -> list[str]:
    """
    Greedy width-based wrapper. Korean wraps cleanly per-character (no spaces
    required), so we accumulate characters until they'd exceed `max_width`
    when measured by the font, then break.
    """
    if not text:
        return []

    lines: list[str] = []
    current = ""
    for ch in text:
        candidate = current + ch
        if ch == "\n":
            lines.append(current)
            current = ""
            continue
        # font.getlength is the modern PIL API; falls back gracefully.
        try:
            width = font.getlength(candidate)
        except AttributeError:
            width = font.getsize(candidate)[0]  # type: ignore[attr-defined]
        if width <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines


def _apply_text_overlay(image_url_or_path: str, text: str) -> str:
    """
    Composite `text` onto the image with a semi-transparent dark band, save
    to ./output/overlay-{timestamp}.png, and return the resulting file:// URI.

    On any failure (Pillow missing, font download blocked, malformed image,
    etc.) returns the original `image_url_or_path` unchanged so the pipeline
    never hard-fails on the cosmetic step.
    """
    if not text or not text.strip():
        return image_url_or_path

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[Designer] Pillow not installed — skipping text overlay.")
        return image_url_or_path

    try:
        img_bytes = _load_image_bytes(image_url_or_path)
        base = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

        font_path = _download_font(_resolve_font_path())
        # Font size scales with image width; clamped to a sensible range.
        font_size = max(28, min(96, base.width // 16))
        font = ImageFont.truetype(str(font_path), size=font_size)

        # Wrap text to ~85% of image width.
        max_text_width = int(base.width * 0.85)
        lines = _wrap_text_to_width(text.strip(), font, max_text_width)
        if not lines:
            return image_url_or_path

        # Measure block height.
        line_heights: list[int] = []
        for line in lines:
            bbox = font.getbbox(line)
            line_heights.append(bbox[3] - bbox[1])
        line_spacing = int(font_size * 0.35)
        text_block_h = sum(line_heights) + line_spacing * (len(lines) - 1)

        # Bottom band: tall enough for the text block + generous padding.
        padding_v = int(font_size * 0.9)
        band_h = text_block_h + padding_v * 2
        band_top = base.height - band_h

        # Semi-transparent dark band on its own RGBA layer, then composited.
        overlay_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay_layer)
        draw.rectangle(
            [(0, band_top), (base.width, base.height)],
            fill=(0, 0, 0, int(255 * 0.4)),  # 40% opacity black
        )

        # Center each line horizontally inside the band.
        y = band_top + padding_v
        for line, h in zip(lines, line_heights):
            try:
                line_w = font.getlength(line)
            except AttributeError:
                line_w = font.getsize(line)[0]  # type: ignore[attr-defined]
            x = (base.width - line_w) / 2
            draw.text(
                (x, y),
                line,
                font=font,
                fill=(255, 255, 255, 255),
            )
            y += h + line_spacing

        composed = Image.alpha_composite(base, overlay_layer).convert("RGB")

        # Compose to memory, then upload to Supabase Storage. Returning a
        # public HTTPS URL lets the publisher webhook + downstream channels
        # fetch the asset without any local-filesystem dependency.
        buf = io.BytesIO()
        composed.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        public_url = _upload_to_supabase(png_bytes)
        if public_url:
            return public_url

        # Upload unavailable — fall back to the raw image URL we started with
        # so the pipeline still produces *something* fetchable downstream.
        return image_url_or_path

    except Exception as e:
        print(f"[Designer] Text overlay failed: {e!r} — using raw image.")
        return image_url_or_path


# =============================================================================
# Node
# =============================================================================


def designer_node(state: dict[str, Any]) -> dict[str, Any]:
    draft = state.get("draft") or ""
    if not draft:
        return {
            "image_url": MOCK_IMAGE_URL,
            "image_model": MOCK_MODEL_NAME,
            "image_prompt": "(no draft available)",
            "overlay_text": "",
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

    fallback_prompt = (
        f"Cinematic editorial photography of an empty {target_region.get('label', 'LA')} "
        f"strip-mall plaza at golden hour, palm tree silhouettes, large negative "
        f"space across the upper sky and lower asphalt, 35mm, shallow depth of field, "
        f"warm color grading, soft natural light, lifestyle magazine aesthetic."
    )
    fallback_overlay = "동네 사람만 아는 그곳"

    try:
        llm = _build_prompt_llm()
        response = llm.invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_msg)]
        )
        raw = getattr(response, "content", str(response))
        image_prompt, overlay_text = _parse_designer_output(
            raw, fallback_prompt=fallback_prompt, fallback_overlay=fallback_overlay
        )
    except Exception as e:
        return {
            "image_url": MOCK_IMAGE_URL,
            "image_model": MOCK_MODEL_NAME,
            "image_prompt": fallback_prompt,
            "overlay_text": fallback_overlay,
            "history": [
                {"node": "designer", "error": f"prompt-llm failed: {e!r}"}
            ],
        }

    raw_image_url, image_model = _generate_image(image_prompt)
    final_image_url = _apply_text_overlay(raw_image_url, overlay_text)

    return {
        "image_url": final_image_url,
        "image_model": image_model,
        "image_prompt": image_prompt,
        "overlay_text": overlay_text,
        "history": [
            {
                "node": "designer",
                "image_model": image_model,
                "raw_image_url": raw_image_url,
                "image_url": final_image_url,
                "overlay_chars": len(overlay_text),
            }
        ],
    }
