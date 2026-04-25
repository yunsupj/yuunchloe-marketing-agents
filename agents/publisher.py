"""
Publisher agent: ships the approved draft + image to a downstream webhook
(e.g. Zapier, Make, n8n, or our own dispatcher service that fans out to
Instagram / Kakao Channel / Reddit).

Honors `pipeline.publishing.dry_run` from settings.yaml — when true, prints
the would-be payload and skips the network call. The pipeline config is
expected to be threaded into graph state as `pipeline_config` by main.py.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests


def _is_dry_run(state: dict[str, Any]) -> bool:
    """
    Default is True (safe) when the pipeline_config is missing or malformed —
    the publisher should never accidentally post in an under-configured run.
    """
    pipeline_config = state.get("pipeline_config") or {}
    publishing = pipeline_config.get("publishing") or {}
    dry_run = publishing.get("dry_run", True)
    return bool(dry_run)


def _build_payload(state: dict[str, Any]) -> dict[str, Any]:
    app_context = state.get("app_context") or {}
    target_region = state.get("target_region") or {}
    return {
        "text": state.get("draft") or "",
        "image_url": state.get("image_url") or "",
        "image_model": state.get("image_model") or "",
        "overlay_text": state.get("overlay_text") or "",
        "channels": app_context.get("distribution_channels") or [],
        "app_name": app_context.get("app_name"),
        "region": target_region.get("label"),
        "critic_score": state.get("critic_score"),
    }


def publisher_node(state: dict[str, Any]) -> dict[str, Any]:
    payload = _build_payload(state)

    if _is_dry_run(state):
        print("\n[Publisher · DRY RUN — payload that would be POSTed]")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return {
            "published": False,
            "publish_status": "dry_run",
            "history": [{"node": "publisher", "dry_run": True}],
        }

    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        msg = "WEBHOOK_URL env var is not set — cannot publish."
        print(f"[Publisher · ERROR] {msg}")
        return {
            "published": False,
            "publish_status": f"error: {msg}",
            "history": [{"node": "publisher", "error": msg}],
        }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[Publisher · ERROR] Webhook POST failed: {e!r}")
        return {
            "published": False,
            "publish_status": f"error: {e!r}",
            "history": [{"node": "publisher", "error": repr(e)}],
        }

    print(f"[Publisher] Posted to webhook ({resp.status_code}).")
    return {
        "published": True,
        "publish_status": f"ok ({resp.status_code})",
        "history": [
            {
                "node": "publisher",
                "status_code": resp.status_code,
                "channels": payload["channels"],
            }
        ],
    }
