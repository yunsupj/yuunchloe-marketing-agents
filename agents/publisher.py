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

REGION_TO_SUBREDDITS: dict[str, list[str]] = {
    "la_oc":  ["LosAngeles", "orangecounty", "AskLosAngeles", "FoodLosAngeles", "SouthBayLA", "irvine", "longbeach", "SGV", "Pasadena", "ucla", "USC", "LAlist", "Melrose", "BeverlyHills", "SantaMonica", "Anaheim", "IrvineClassifieds"],
    "sf_bay": ["bayarea", "sanfrancisco", "SFBayArea"],
    "nyc":    ["nyc", "newyorkcity"],
    "seattle": ["Seattle", "seattlewa"],
    "chicago": ["chicago"],
}


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
    client,
    topic: str,
    subreddits: list[str],
    *,
    draft_text_ko: str = "",
    draft_text_en: str = "",
    carousel_urls_ko: list[str] | None = None,
    carousel_urls_en: list[str] | None = None,
    reddit_promo_text: str = "",
    caption_ko: str = "",
    caption_en: str = "",
    hotspot_id: str | None = None,
) -> str | None:
    """
    Insert a row into `marketing_posts` and return the new row's id (as str)
    or None on failure. Each language track and the Reddit-tuned promo are
    stored in their own columns so the Make.com publishing step can dispatch
    each channel independently after human approval.
    """
    payload: dict[str, Any] = {
        "topic": topic,
        "draft_text_ko": draft_text_ko,
        "draft_text_en": draft_text_en,
        "carousel_urls_ko": carousel_urls_ko or [],
        "carousel_urls_en": carousel_urls_en or [],
        "reddit_promo_text": reddit_promo_text,
        "caption_ko": caption_ko,
        "caption_en": caption_en,
        "subreddits": subreddits,
        "status": "pending",
    }
    if hotspot_id is not None:
        payload["hotspot_id"] = hotspot_id
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


_REDDIT_SNIPPET_CHARS = 600


def _build_blocks(
    topic: str,
    post_id: str,
    *,
    draft_text_ko: str = "",
    carousel_urls_ko: list[str] | None = None,
    caption_ko: str = "",
    reddit_promo_text: str = "",
) -> list[dict[str, Any]]:
    """
    Approval card: all 4 KO carousel images + KO draft + IG caption + Reddit snippet.
    """
    carousel_urls_ko = carousel_urls_ko or []

    header_text = _truncate(
        f"*New marketing post — pending approval*\n"
        f"*Topic:* {topic}\n"
        f"*Tracks:* 🇰🇷 KO Carousel + 🇺🇸 EN Reddit Promo",
        _SLACK_TEXT_BUDGET,
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text},
        }
    ]

    # All 4 KO carousel slides so the approver reviews the full set.
    for i, url in enumerate(carousel_urls_ko[:4], start=1):
        if not url:
            continue
        blocks.append(
            {
                "type": "image",
                "image_url": url,
                "alt_text": f"KO slide {i}",
                "title": {"type": "plain_text", "text": f"🇰🇷 KO Slide {i}"},
            }
        )

    if draft_text_ko:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _truncate(
                        f"*🇰🇷 KO Carousel Draft*\n{draft_text_ko}",
                        _SLACK_TEXT_BUDGET,
                    ),
                },
            }
        )

    if caption_ko:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _truncate(
                        f"*📸 IG Caption (KO)*\n{caption_ko}",
                        _SLACK_TEXT_BUDGET,
                    ),
                },
            }
        )

    if reddit_promo_text:
        blocks.append({"type": "divider"})
        snippet = _truncate(reddit_promo_text, _REDDIT_SNIPPET_CHARS)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _truncate(
                        f"*🇺🇸 EN Reddit Promo (snippet)*\n{snippet}",
                        _SLACK_TEXT_BUDGET,
                    ),
                },
            }
        )

    # Interactivity handler dispatches on the constant action_id and reads
    # the marketing_posts row id from `value` — the conventional Slack pattern.
    # block_id carries the id too as a defensive backup.
    blocks.append(
        {
            "type": "actions",
            "block_id": f"marketing_post_{post_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve & Publish"},
                    "style": "primary",
                    "action_id": "approve_post",
                    "value": post_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "reject_post",
                    "value": post_id,
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


def _clean_url_list(raw: Any) -> list[str]:
    return [
        u.strip() for u in (raw or [])
        if isinstance(u, str) and u.strip()
    ]


def publisher_node(state: dict[str, Any]) -> dict[str, Any]:
    draft_text_ko = (state.get("draft_text_ko") or state.get("draft") or "").strip()
    draft_text_en = (state.get("draft_text_en") or "").strip()
    carousel_urls_ko = _clean_url_list(state.get("carousel_urls_ko"))
    carousel_urls_en = _clean_url_list(state.get("carousel_urls_en"))
    reddit_promo_text = (state.get("reddit_promo_text") or "").strip()
    caption_ko = (state.get("caption_ko") or "").strip()
    topic = _extract_topic(state)

    target_region = state.get("target_region") or {}
    region_id = target_region.get("id") or ""
    subreddits = REGION_TO_SUBREDDITS.get(region_id) or REGION_TO_SUBREDDITS.get("la_oc", [])

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

    caption_en = (state.get("caption_en") or "").strip()
    hotspot_id = state.get("hotspot_id") or None

    post_id = _insert_pending_row(
        client,
        topic,
        subreddits,
        draft_text_ko=draft_text_ko,
        draft_text_en=draft_text_en,
        carousel_urls_ko=carousel_urls_ko,
        carousel_urls_en=carousel_urls_en,
        reddit_promo_text=reddit_promo_text,
        caption_ko=caption_ko,
        caption_en=caption_en,
        hotspot_id=hotspot_id,
    )
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

    blocks = _build_blocks(
        topic,
        post_id,
        draft_text_ko=draft_text_ko,
        carousel_urls_ko=carousel_urls_ko,
        caption_ko=caption_ko,
        reddit_promo_text=reddit_promo_text,
    )
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
