"""
Slack Interactive Webhook Server for the marketing pipeline.

Handles Approve / Reject button clicks sent by Slack's Block Kit actions.

Run:
    uvicorn server:app --host 0.0.0.0 --port 8080

Or directly:
    python server.py

Slack sends a URL-encoded POST whose single field "payload" contains a JSON
string. We parse it, dispatch to a background task so Slack gets a sub-second
200 response, and do the heavy lifting (Supabase writes, Make.com trigger,
auto-regen) asynchronously.

Required env vars:
    EXPO_PUBLIC_SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY   (preferred)  or  EXPO_PUBLIC_SUPABASE_ANON_KEY
    MAKE_WEBHOOK_URL
    SLACK_BOT_TOKEN             (optional — used only for rich error DMs later)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

app = FastAPI(
    title="Marketing Pipeline — Slack Webhook Server",
    version="1.0.0",
    docs_url="/docs",
)


# =============================================================================
# Supabase client (lazy singleton, same pattern as auto_scheduler / publisher)
# =============================================================================


def _build_supabase_client():
    try:
        from supabase import create_client
    except ImportError:
        print("[server] supabase-py not installed.")
        return None

    url = os.getenv("EXPO_PUBLIC_SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("EXPO_PUBLIC_SUPABASE_ANON_KEY")
    )
    if not url or not key:
        print("[server] Supabase env vars missing.")
        return None

    try:
        return create_client(url, key)
    except Exception as e:
        print(f"[server] Supabase client init failed: {e!r}")
        return None


# =============================================================================
# Background tasks
# =============================================================================


def _fetch_post_row(client, post_id: str) -> dict[str, Any]:
    """Return the marketing_posts row for post_id, or {} on any failure."""
    try:
        resp = (
            client.table("marketing_posts")
            .select("*")
            .eq("id", post_id)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        return rows[0] if rows else {}
    except Exception as e:
        print(f"[server] Failed to fetch marketing_posts id={post_id}: {e!r}")
        return {}


def _update_post_status(client, post_id: str, status: str) -> None:
    try:
        client.table("marketing_posts").update({"status": status}).eq("id", post_id).execute()
    except Exception as e:
        print(f"[server] Failed to set marketing_posts id={post_id} status={status}: {e!r}")


def _update_hotspot_status(client, hotspot_id: Any, status: str) -> None:
    if hotspot_id is None:
        print("[server] No hotspot_id on this post — marketing_hotspots not updated.")
        return
    try:
        client.table("marketing_hotspots").update({"status": status}).eq("id", hotspot_id).execute()
    except Exception as e:
        print(f"[server] Failed to set marketing_hotspots id={hotspot_id} status={status}: {e!r}")


def _trigger_make(post_row: dict[str, Any]) -> None:
    """POST the approved post data to Make.com for publishing."""
    make_url = os.getenv("MAKE_WEBHOOK_URL")
    if not make_url:
        print("[server] MAKE_WEBHOOK_URL not set — skipping publish trigger.")
        return

    payload = {
        "post_id":           post_row.get("id"),
        "topic":             post_row.get("topic"),
        "draft_text_ko":     post_row.get("draft_text_ko"),
        "draft_text_en":     post_row.get("draft_text_en"),
        "caption_ko":        post_row.get("caption_ko"),
        "caption_en":        post_row.get("caption_en"),
        "reddit_promo_text": post_row.get("reddit_promo_text"),
        "carousel_urls_ko":  post_row.get("carousel_urls_ko") or [],
        "carousel_urls_en":  post_row.get("carousel_urls_en") or [],
    }
    try:
        resp = requests.post(make_url, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[server] Make.com trigger sent for post id={post_row.get('id')} — HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"[server] Make.com webhook failed: {e!r}")


def _trigger_auto_regen() -> None:
    """
    Fire-and-forget: spawn auto_scheduler.py so a new draft is generated
    immediately after a rejection. Uses sys.executable to stay in the same
    venv and passes --strategy auto so it picks the best available source.
    """
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_scheduler.py")
    try:
        proc = subprocess.Popen(
            [sys.executable, script, "--strategy", "auto"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Detach from this process's signal group so a server restart
            # doesn't kill the still-running pipeline.
            start_new_session=True,
        )
        print(f"[server] Auto-regen spawned (pid={proc.pid}).")
    except Exception as e:
        print(f"[server] Failed to spawn auto_scheduler: {e!r}")


def _handle_approve(post_id: str) -> None:
    client = _build_supabase_client()
    if client is None:
        print(f"[server] approve: Supabase unavailable for post {post_id}.")
        return

    post_row = _fetch_post_row(client, post_id)
    if not post_row:
        print(f"[server] approve: No row found for post_id={post_id}.")
        return

    hotspot_id = post_row.get("hotspot_id")

    _update_post_status(client, post_id, "approved")
    _update_hotspot_status(client, hotspot_id, "completed")
    _trigger_make(post_row)

    print(f"[server] Post {post_id} approved and published. Hotspot {hotspot_id} marked completed.")


def _handle_reject(post_id: str) -> None:
    client = _build_supabase_client()
    if client is None:
        print(f"[server] reject: Supabase unavailable for post {post_id}.")
        return

    post_row = _fetch_post_row(client, post_id)
    hotspot_id = post_row.get("hotspot_id") if post_row else None

    _update_post_status(client, post_id, "rejected")
    _update_hotspot_status(client, hotspot_id, "pending")
    _trigger_auto_regen()

    print(f"[server] Post {post_id} rejected. Hotspot {hotspot_id} re-queued. Auto-regen started.")


# =============================================================================
# Webhook endpoint
# =============================================================================


@app.post("/api/webhooks/slack-interactive")
async def slack_interactive(request: Request, background_tasks: BackgroundTasks):
    """
    Receive Slack Block Kit interactive action callbacks.

    Slack sends application/x-www-form-urlencoded with a single `payload`
    field containing a JSON string. We parse it, enqueue the work as a
    background task, and return 200 immediately so Slack doesn't time out.
    """
    form = await request.form()
    payload_str = form.get("payload")

    if not payload_str:
        return JSONResponse({"status": "ignored", "reason": "no payload"})

    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError as e:
        print(f"[server] Failed to parse Slack payload: {e!r}")
        return JSONResponse({"status": "ignored", "reason": "invalid json"})

    actions = payload.get("actions") or []
    if not actions:
        return JSONResponse({"status": "ignored", "reason": "no actions"})

    action    = actions[0]
    action_id = action.get("action_id", "")
    post_id   = action.get("value", "")

    if not post_id:
        return JSONResponse({"status": "ignored", "reason": "no post_id"})

    if action_id == "approve_post":
        background_tasks.add_task(_handle_approve, post_id)
        return JSONResponse({
            "replace_original": "true",
            "text": f"✅ 카드뉴스(ID: {post_id}) 발행이 승인되었습니다. Make.com으로 전송 중...",
        })

    if action_id == "reject_post":
        background_tasks.add_task(_handle_reject, post_id)
        return JSONResponse({
            "replace_original": "true",
            "text": f"❌ 카드뉴스(ID: {post_id}) 발행이 거절되었습니다. 새 콘텐츠를 자동으로 생성합니다...",
        })

    return JSONResponse({"status": "ignored", "reason": f"unknown action_id: {action_id}"})


# =============================================================================
# Health check
# =============================================================================


@app.get("/")
def health():
    return {"status": "ok", "service": "marketing-pipeline-slack-webhook"}


# =============================================================================
# Entry point
# =============================================================================


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=False)
