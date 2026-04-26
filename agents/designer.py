"""
Designer agent: render each slide of the carousel storyboard.

Pipeline per slide:
    1. ai_generated -> _generate_image(slide["image_prompt"])
       real_photo   -> use slide["source_url"]
    2. _apply_html_template(image_url, overlay_text) -> bytes
       (placeholder; later this calls Bannerbear or a similar HTML→image
       renderer so overlay_text is laid out by a designed template instead
       of PIL.)
    3. Upload bytes to the Supabase `marketing-assets` bucket.
    4. Collect the public HTTPS URL.

Returns `state["carousel_urls"]` — one URL per rendered slide, in the same
order as `state["carousel_draft"]`.
"""

from __future__ import annotations

import base64
import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


MOCK_IMAGE_URL = (
    "https://dummyimage.com/600x400/000/fff&text=Local+Photo+Placeholder"
)
MOCK_MODEL_NAME = "mock"
REAL_PHOTO_TAG = "real_photo"

FLUX_MODEL = "flux-2-pro"
IMAGEN_MODEL = "imagen-4-ultra"


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
# A/B selection (unchanged from single-image era)
# =============================================================================


_MODEL_CALLERS = {
    FLUX_MODEL: (_has_replicate, _call_replicate_flux),
    IMAGEN_MODEL: (_has_vertex, _call_vertex_imagen),
}


def _generate_image(prompt: str) -> tuple[str, str]:
    """Returns (image_url, model_name). 50/50 A/B with credential-aware fallback."""
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
# HTML template renderer (PLACEHOLDER — Bannerbear later)
# =============================================================================


def _apply_html_template(image_url: str, text: str) -> bytes:
    """
    Render the final slide image by calling the external Next.js Vercel OG
    image-generation engine. Returns the rendered PNG bytes.

    On failure, the exception is logged and re-raised so `_render_slide` can
    fall back to the raw image URL.
    """
    og_base_url = os.getenv("OG_BASE_URL", "http://localhost:3000")
    encoded_img = quote(image_url, safe="")
    encoded_text = quote(text, safe="")
    endpoint = f"{og_base_url}/api/og?image={encoded_img}&text={encoded_text}"

    try:
        resp = requests.get(endpoint, timeout=20)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"[Designer] OG render failed ({endpoint}): {e!r}")
        raise


# =============================================================================
# Supabase Storage upload
# =============================================================================


SUPABASE_BUCKET = "marketing-assets"


def _build_storage_client():
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


def _upload_to_supabase(png_bytes: bytes, suffix: str = "") -> str | None:
    """
    Upload PNG bytes to the bucket, returning the public HTTPS URL.
    `suffix` disambiguates within-second uploads (slide index, etc.).
    """
    client = _build_storage_client()
    if client is None:
        return None

    bucket_name = os.getenv("SUPABASE_MARKETING_BUCKET", SUPABASE_BUCKET)
    tail = f"_{suffix}" if suffix else ""
    filename = f"slide_{int(time.time() * 1000)}{tail}.png"

    try:
        bucket = client.storage.from_(bucket_name)
        bucket.upload(
            path=filename,
            file=png_bytes,
            file_options={"content-type": "image/png", "upsert": "true"},
        )
        public_url = bucket.get_public_url(filename)
    except Exception as e:
        print(f"[Designer] Supabase upload failed: {e!r}")
        return None

    if not isinstance(public_url, str) or not public_url.startswith("http"):
        print(f"[Designer] Unexpected public_url shape: {public_url!r}")
        return None
    return public_url


# =============================================================================
# Per-slide rendering
# =============================================================================


def _resolve_raw_image(slide: dict[str, Any]) -> tuple[str, str]:
    """
    Returns (raw_image_url, model_tag) for a single storyboard slide, or
    ("", "") when the slide is malformed and should be skipped.
    """
    slide_type = slide.get("type")
    if slide_type == "ai_generated":
        prompt = (slide.get("image_prompt") or "").strip()
        if not prompt:
            return "", ""
        return _generate_image(prompt)

    if slide_type == "real_photo":
        url = (slide.get("source_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return "", ""
        return url, REAL_PHOTO_TAG

    return "", ""


def _render_slide(
    slide: dict[str, Any], index: int
) -> tuple[str, str]:
    """
    Returns (final_url, model_tag). Falls back to the raw URL (or mock) on
    any rendering / upload failure so the carousel always has the right
    number of entries.
    """
    raw_url, model_tag = _resolve_raw_image(slide)
    if not raw_url:
        return MOCK_IMAGE_URL, MOCK_MODEL_NAME

    overlay_text = (slide.get("overlay_text") or "").strip()
    try:
        png_bytes = _apply_html_template(raw_url, overlay_text)
    except Exception as e:
        print(f"[Designer] template apply failed for slide {index}: {e!r}")
        return raw_url, model_tag

    public_url = _upload_to_supabase(png_bytes, suffix=f"s{index}")
    if not public_url:
        return raw_url, model_tag
    return public_url, model_tag


# =============================================================================
# Node
# =============================================================================


def designer_node(state: dict[str, Any]) -> dict[str, Any]:
    carousel = state.get("carousel_draft") or []
    if not carousel:
        return {
            "carousel_urls": [],
            "image_url": "",
            "image_model": MOCK_MODEL_NAME,
            "history": [{"node": "designer", "skipped": "no carousel_draft"}],
        }

    carousel_urls: list[str] = []
    model_tags: list[str] = []
    for i, slide in enumerate(carousel, start=1):
        if not isinstance(slide, dict):
            continue
        final_url, model_tag = _render_slide(slide, i)
        carousel_urls.append(final_url)
        if model_tag:
            model_tags.append(model_tag)

    return {
        "carousel_urls": carousel_urls,
        # Cover URL alias for the existing summary printouts.
        "image_url": carousel_urls[0] if carousel_urls else "",
        "image_model": ",".join(sorted(set(model_tags))) or MOCK_MODEL_NAME,
        "history": [
            {
                "node": "designer",
                "slide_count": len(carousel_urls),
                "models": model_tags,
            }
        ],
    }
