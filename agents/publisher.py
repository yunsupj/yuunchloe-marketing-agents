"""
Human-in-the-loop publisher node.

Replaces the old dry_run / direct-webhook publisher. For each generated
post we:
    1. Insert a row into Supabase `marketing_posts` with status='pending'.
    2. Post an interactive Slack approval message (Block Kit) into the
       configured channel — text + image + Approve/Reject buttons.
    3. A separate Slack-interactivity webhook (out of scope here) flips
       the row's status to 'approved' / 'rejected' and triggers the
       actual publish on approval.

Required env:
    EXPO_PUBLIC_SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY  (preferred)  or  EXPO_PUBLIC_SUPABASE_ANON_KEY
    SLACK_BOT_TOKEN
    SLACK_APPROVAL_CHANNEL_ID
"""

from __future__ import annotations

import os
from typing import Any

import requests


SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

# Slack section text caps at 3000 chars; leave headroom for our wrapper text.
_SLACK_TEXT_BUDGET = 2500


# =============================================================================
# Supabase client (lazy, same shape as auto_scheduler / designer)
# =============================================================================


def _build_supabase_client():
    try:
        from supabase import create_client
    except ImportError:
        print("[Publisher] supabase-py not installed.")
        return None

    url = os.getenv("EXPO_PUBLIC_SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("EXPO_PUBLIC_SUPABASE_ANON_KEY")
    )
    if not url or not key:
        print("[Publisher] Supabase env vars missing.")
        return None

    try:
        return create_client(url, key)
    except Exception as e:
        print(f"[Publisher] Supabase client init failed: {e!r}")
        return None


# =============================================================================
# State helpers
# =============================================================================


def _extract_topic(state: dict[str, Any]) -> str:
    """
    The collector node rewrites research_notes as
        [원래 토픽]\n{query}\n\n[리서치 노트]\n{summary}
    so the user's actual topic is the lines between [원래 토픽] and the next
    blank line. Fall back to the whole research_notes blob, then to the
    first line of the draft.
    """
    notes = (state.get("research_notes") or "").strip()
    if "[원래 토픽]" in notes:
        body = notes.split("[원래 토픽]", 1)[1].lstrip()
        topic = body.split("[리서치 노트]", 1)[0].strip()
        if topic:
            return topic
    if notes:
        return notes
    draft = (state.get("draft") or "").strip()
    return draft.splitlines()[0] if draft else "(no topic)"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# =============================================================================
# Supabase: insert pending row
# =============================================================================


def _insert_pending_row(
    client, topic: str, draft: str, carousel_urls: list[str]
) -> str | None:
    """
    Insert a row into `marketing_posts` and return the new row's id (as str)
    or None on failure. `carousel_urls` is stored as an array (Postgres
    TEXT[] / JSONB) so each slide URL is independently addressable from the
    approval handler and downstream publisher.
    """
    payload = {
        "topic": topic,
        "draft_text": draft,
        "carousel_urls": carousel_urls,
        "status": "pending",
    }
    try:
        resp = client.table("marketing_posts").insert(payload).execute()
    except Exception as e:
        print(f"[Publisher] Supabase insert failed: {e!r}")
        return None

    rows = getattr(resp, "data", None) or []
    if not rows:
        print(f"[Publisher] Supabase insert returned no row: {resp!r}")
        return None
    row_id = rows[0].get("id")
    return str(row_id) if row_id is not None else None


# =============================================================================
# Slack Block Kit
# =============================================================================


_CAROUSEL_PREVIEW_LIMIT = 4


def _build_blocks(
    topic: str, draft: str, carousel_urls: list[str], post_id: str
) -> list[dict[str, Any]]:
    body_text = _truncate(
        f"*New marketing post — pending approval*\n"
        f"*Topic:* {topic}\n\n{draft}",
        _SLACK_TEXT_BUDGET,
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body_text},
        }
    ]

    # One image block per slide so the approver can scan the whole carousel.
    # Slack rejects empty image_url, so each URL is filtered before append.
    for i, url in enumerate(carousel_urls[:_CAROUSEL_PREVIEW_LIMIT], start=1):
        if not url:
            continue
        blocks.append(
            {
                "type": "image",
                "image_url": url,
                "alt_text": f"Carousel slide {i}",
                "title": {"type": "plain_text", "text": f"Slide {i}"},
            }
        )

    blocks.append(
        {
            "type": "actions",
            "block_id": f"marketing_post_{post_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve & Post"},
                    "style": "primary",
                    "value": "approve",
                    "action_id": f"approve_post_{post_id}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "value": "reject",
                    "action_id": f"reject_post_{post_id}",
                },
            ],
        }
    )
    return blocks


def _post_to_slack(
    bot_token: str,
    channel: str,
    topic: str,
    blocks: list[dict[str, Any]],
) -> tuple[bool, str]:
    """
    Returns (ok, info). On success, info is the message ts; on failure it's
    the Slack error code or HTTP error description.
    """
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {
        "channel": channel,
        # Fallback text used in notifications and accessibility contexts.
        "text": f"Pending approval — {topic}",
        "blocks": blocks,
    }
    try:
        resp = requests.post(
            SLACK_POST_MESSAGE_URL, headers=headers, json=body, timeout=15
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return False, f"http error: {e!r}"

    data = resp.json() if resp.content else {}
    if not data.get("ok"):
        return False, f"slack error: {data.get('error', 'unknown')}"
    return True, str(data.get("ts", ""))


# =============================================================================
# Node
# =============================================================================


def publisher_node(state: dict[str, Any]) -> dict[str, Any]:
    draft = (state.get("draft") or "").strip()
    raw_urls = state.get("carousel_urls") or []
    carousel_urls = [
        u.strip() for u in raw_urls
        if isinstance(u, str) and u.strip()
    ]
    topic = _extract_topic(state)

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    channel = os.getenv("SLACK_APPROVAL_CHANNEL_ID")

    # Insert into Supabase first so an approval-button click always has a row
    # to flip. If the insert fails we don't post to Slack — a Slack approval
    # with no backing row is worse than no approval at all.
    client = _build_supabase_client()
    if client is None:
        return {
            "published": False,
            "publish_status": "error: supabase unavailable",
            "history": [
                {"node": "publisher", "error": "supabase unavailable"}
            ],
        }

    post_id = _insert_pending_row(client, topic, draft, carousel_urls)
    if post_id is None:
        return {
            "published": False,
            "publish_status": "error: supabase insert failed",
            "history": [
                {"node": "publisher", "error": "supabase insert failed"}
            ],
        }

    if not bot_token or not channel:
        msg = "SLACK_BOT_TOKEN or SLACK_APPROVAL_CHANNEL_ID not set"
        print(f"[Publisher] {msg} — row {post_id} stays pending (no Slack ping).")
        return {
            "published": False,
            "publish_status": f"pending (no slack): row {post_id}",
            "history": [
                {
                    "node": "publisher",
                    "supabase_id": post_id,
                    "slack_sent": False,
                    "error": msg,
                }
            ],
        }

    blocks = _build_blocks(topic, draft, carousel_urls, post_id)
    ok, info = _post_to_slack(bot_token, channel, topic, blocks)
    if not ok:
        print(f"[Publisher] Slack post failed: {info}")
        return {
            "published": False,
            "publish_status": f"pending (slack failed: {info}): row {post_id}",
            "history": [
                {
                    "node": "publisher",
                    "supabase_id": post_id,
                    "slack_sent": False,
                    "error": info,
                }
            ],
        }

    print(
        f"[Publisher] Slack approval message sent (ts={info}) "
        f"for marketing_posts.id={post_id}."
    )
    return {
        "published": False,  # publishing happens after human approval
        "publish_status": f"pending approval: row {post_id}",
        "history": [
            {
                "node": "publisher",
                "supabase_id": post_id,
                "slack_sent": True,
                "slack_ts": info,
            }
        ],
    }
