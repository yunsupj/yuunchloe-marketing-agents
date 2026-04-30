"""
Designer agent: render every slide of BOTH bilingual carousel tracks.

Per-slide pipeline (run once for KO, once for EN):
    1. Real photo (`source_url` or shared `raw_pool`) -> preferred
       photo_instruction -> AI-generated only if all real photos exhausted
    2. _apply_html_template(image_url, title+description, category)
       calls the external Vercel OG endpoint to compose the final slide PNG.
    3. Upload bytes to the Supabase `marketing-assets` bucket.
    4. Collect the public HTTPS URL.

Returns:
    state["carousel_urls_ko"] — one URL per rendered KO slide
    state["carousel_urls_en"] — one URL per rendered EN slide
    state["carousel_urls"]    — back-compat alias = carousel_urls_ko
"""

from __future__ import annotations

import base64
import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests


MOCK_IMAGE_URL = (
    "https://dummyimage.com/600x400/000/fff&text=Local+Photo+Placeholder"
)
MOCK_MODEL_NAME = "mock"
REAL_PHOTO_TAG = "real_photo"

FLUX_MODEL = "flux-2-pro"
IMAGEN_MODEL = "imagen-4-ultra"

# Fallback for the dynamic orange-pill category badge when a slide is missing
# the field (e.g. legacy carousels or the hardcoded app_promo). Kept in lockstep
# with agents/writer.py::DEFAULT_CONTENT_CATEGORY.
DEFAULT_CONTENT_CATEGORY = "깨알톡 · LOCAL"


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


def _apply_html_template(
    image_url: str,
    text: str,
    category: str,
    *,
    is_cta: bool = False,
) -> bytes:
    """
    Render the final slide image by calling the external Next.js Vercel OG
    image-generation engine. Returns the rendered PNG bytes.

    `is_cta=True` appends `&is_cta_slide=true` so the OG engine can apply
    the fixed Slide-4 lockup layout instead of the Lower-Third layout.

    URL-encoding strategy:
        `image_url`, `text`, and `category` all go through `quote_plus`, which
        percent-encodes EVERY character that's special in a URL query
        string — `?`, `&`, `=`, `:`, `/`, `#`, `*`, space, etc. This is
        critical for Google Places photo URLs (whose own internal
        `&maxwidth=...&key=...` chains would otherwise be misparsed) and for
        the category pill values like "DINING * LOCAL PICK" whose `*` and
        spaces must be encoded so the OG endpoint receives the literal
        string after one decode pass.

    On failure, the exception is logged and re-raised so `_render_slide` can
    fall back to the next image candidate.
    """
    og_base_url = os.getenv("OG_BASE_URL", "http://localhost:3000").rstrip("/")
    encoded_img = quote_plus(image_url)
    encoded_text = quote_plus(text)
    encoded_category = quote_plus(category)
    endpoint = (
        f"{og_base_url}/api/og"
        f"?image={encoded_img}&text={encoded_text}&category={encoded_category}"
    )
    if is_cta:
        endpoint += "&is_cta_slide=true"

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
# Per-slide rendering — photo-first, AI as absolute last resort
# =============================================================================


def _slide_overlay_text(slide: dict[str, Any]) -> str:
    """
    Combine the new bilingual schema's `title` + `description` into the single
    `text` param the OG endpoint expects. Falls back to the legacy
    `overlay_text` key for back-compat with any pre-bilingual carousels.
    """
    title = (slide.get("title") or "").strip()
    description = (slide.get("description") or "").strip()
    if title and description:
        return f"{title}\n{description}"
    if title:
        return title
    if description:
        return description
    return (slide.get("overlay_text") or "").strip()


def _render_slide(
    slide: dict[str, Any],
    index: int,
    raw_pool: list[str],
    *,
    suffix: str = "",
    og_category_tag: str = "",
) -> tuple[str, str]:
    """
    Render one slide via the OG template, with strict photo-first priority:

        1. Writer-curated `source_url` (any slide type that supplies one)
        2. Next unused real photo from `raw_pool` (collector-fetched
           `marketing_hotspots.photo_urls`)
        3. AI generation via `_generate_image(photo_instruction)`  ← LAST RESORT
        4. Mock fallback

    Each candidate is attempted through the OG renderer + Supabase upload.
    On any failure (broken URL, OG 5xx, upload error) we fall through to
    the next candidate. The AI image model is only invoked when no real
    photo candidates remain or every one of them has failed — preserving
    authenticity by default.

    Mutates `raw_pool` in place: a successfully-rendered photo is removed
    so later slides in the same carousel don't reuse it.

    `suffix` disambiguates uploads between KO/EN tracks of the same slide
    so the bucket key doesn't collide.

    `og_category_tag` (from Writer state) overrides the per-slide
    `content_category` so all slides in the carousel share the same pill.

    Slide 4 is the CTA/app-promo slide — `is_cta_slide=true` is appended to
    the OG URL so the design engine renders the fixed lockup layout.
    """
    overlay_text = _slide_overlay_text(slide)
    category = (
        og_category_tag.strip()
        or slide.get("content_category")
        or DEFAULT_CONTENT_CATEGORY
    )
    is_cta = slide.get("slide_number") == 4

    # Ordered candidate list. kind in {"photo", "ai"}.
    # For "photo", payload is a URL; for "ai", payload is the photo_instruction.
    candidates: list[tuple[str, str]] = []

    # 1. Writer-curated source_url (regardless of slide type — even an
    #    "ai_generated" cover gets a real photo if the writer pre-picked one).
    src = (slide.get("source_url") or "").strip()
    if src.startswith(("http://", "https://")):
        candidates.append(("photo", src))

    # 2. Unused real photos from the shared pool, dedup'd against (1).
    seen = {url for kind, url in candidates if kind == "photo"}
    for url in raw_pool:
        if url not in seen:
            candidates.append(("photo", url))
            seen.add(url)

    # 3. AI generation — ABSOLUTE LAST RESORT. Only enqueued if a prompt
    #    exists; never tried before all real photos have been exhausted.
    # New bilingual schema uses `photo_instruction`; older drafts used `image_prompt`.
    image_prompt = (
        slide.get("photo_instruction")
        or slide.get("image_prompt")
        or ""
    ).strip()
    if image_prompt:
        candidates.append(("ai", image_prompt))

    for kind, payload in candidates:
        if kind == "photo":
            candidate_url = payload
            model_tag = REAL_PHOTO_TAG
        else:  # "ai"
            print(
                f"[Designer] slide {index}: real-photo candidates exhausted — "
                "falling back to AI image generation as last resort."
            )
            candidate_url, model_tag = _generate_image(payload)
            if not candidate_url:
                continue

        try:
            png_bytes = _apply_html_template(candidate_url, overlay_text, category, is_cta=is_cta)
        except Exception as e:
            print(
                f"[Designer] slide {index} {model_tag} render failed "
                f"({candidate_url}): {e!r} — trying next candidate."
            )
            continue

        # Mark the photo as consumed so other slides don't reuse it.
        if kind == "photo" and candidate_url in raw_pool:
            raw_pool.remove(candidate_url)

        upload_suffix = f"s{index}{('_' + suffix) if suffix else ''}"
        public_url = _upload_to_supabase(png_bytes, suffix=upload_suffix)
        return (public_url or candidate_url), model_tag

    print(f"[Designer] slide {index}: all candidates exhausted — using mock.")
    return MOCK_IMAGE_URL, MOCK_MODEL_NAME


def _render_carousel(
    carousel: list[dict[str, Any]],
    raw_pool: list[str],
    *,
    locale: str,
    og_category_tag: str = "",
) -> tuple[list[str], list[str]]:
    """
    Render every slide in `carousel`, returning `(urls, model_tags)` aligned
    by index. `raw_pool` is mutated in place — pass a separate copy per
    locale so the KO and EN tracks don't fight over the same photos.
    """
    urls: list[str] = []
    model_tags: list[str] = []
    for i, slide in enumerate(carousel, start=1):
        if not isinstance(slide, dict):
            continue
        final_url, model_tag = _render_slide(
            slide, i, raw_pool, suffix=locale, og_category_tag=og_category_tag
        )
        urls.append(final_url)
        if model_tag:
            model_tags.append(model_tag)
    return urls, model_tags


# =============================================================================
# Node
# =============================================================================


def designer_node(state: dict[str, Any]) -> dict[str, Any]:
    carousel_ko = state.get("carousel_ko") or state.get("carousel_draft") or []
    carousel_en = state.get("carousel_en") or []

    if not carousel_ko and not carousel_en:
        return {
            "carousel_urls_ko": [],
            "carousel_urls_en": [],
            "image_model": MOCK_MODEL_NAME,
            "history": [{"node": "designer", "skipped": "no carousel"}],
        }

    # Each locale gets its OWN copy of the photo pool. The KO and EN tracks
    # render the same story in parallel; we WANT them to land on the same
    # real photos so the visual narrative matches across languages. Sharing
    # one pool would starve whichever locale rendered second.
    base_pool: list[str] = [
        url for url in (state.get("raw_photo_urls") or [])
        if isinstance(url, str) and url.startswith(("http://", "https://"))
    ]
    initial_pool_size = len(base_pool)

    pool_ko = list(base_pool)
    pool_en = list(base_pool)

    og_category_tag = (state.get("og_category_tag") or DEFAULT_CONTENT_CATEGORY).strip()

    carousel_urls_ko, model_tags_ko = _render_carousel(
        carousel_ko, pool_ko, locale="ko", og_category_tag=og_category_tag
    )
    carousel_urls_en, model_tags_en = _render_carousel(
        carousel_en, pool_en, locale="en", og_category_tag=og_category_tag
    )

    all_model_tags = model_tags_ko + model_tags_en

    return {
        "carousel_urls_ko": carousel_urls_ko,
        "carousel_urls_en": carousel_urls_en,
        "image_model": ",".join(sorted(set(all_model_tags))) or MOCK_MODEL_NAME,
        "history": [
            {
                "node": "designer",
                "slide_count_ko": len(carousel_urls_ko),
                "slide_count_en": len(carousel_urls_en),
                "models": all_model_tags,
                "raw_photos_available": initial_pool_size,
                "raw_photos_remaining_ko": len(pool_ko),
                "raw_photos_remaining_en": len(pool_en),
                "ai_fallbacks": sum(
                    1 for tag in all_model_tags
                    if tag in (FLUX_MODEL, IMAGEN_MODEL)
                ),
            }
        ],
    }
