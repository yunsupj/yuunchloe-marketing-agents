"""
Pipeline entry point.

    python main.py "이번 주말 Torrance Mitsuwa 세일 정보"
    python main.py --profile pickle_sf "AI shopping agent launch"
    APP_CONFIG_PATH=config/settings.dev.yaml python main.py "..."

Pulls the active profile out of `config/settings.yaml`, builds the initial
GraphState, runs the Writer <-> Critic loop in `core.graph.graph`, and prints
a human-readable summary.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


# Load .env BEFORE importing anything that reads env vars (LangSmith picks up
# LANGCHAIN_TRACING_V2 / LANGCHAIN_API_KEY at import time on its own — we just
# need them present in os.environ when langchain modules initialize).
load_dotenv()

# Imported after load_dotenv so any LLM clients see the keys at construction.
from core.graph import graph  # noqa: E402


DEFAULT_CONFIG_PATH = "config/settings.yaml"


# =============================================================================
# Config loading
# =============================================================================


def load_config(path: str | None = None) -> dict[str, Any]:
    """
    Load the YAML config. Resolution order:
        1. explicit `path` arg
        2. APP_CONFIG_PATH env var
        3. DEFAULT_CONFIG_PATH (./config/settings.yaml)
    """
    config_path = Path(
        path or os.getenv("APP_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    )
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# =============================================================================
# State initialization
# =============================================================================


def build_initial_state(
    config: dict[str, Any],
    research_notes: str,
    profile_override: str | None = None,
    region_override: str | None = None,
) -> dict[str, Any]:
    """
    Resolve the active profile + target region from config and assemble the
    initial GraphState dict the graph expects.
    """
    profile_key = profile_override or config.get("active_profile")
    if not profile_key:
        raise ValueError("No `active_profile` in config and no override given.")

    profiles = config.get("profiles") or {}
    profile = profiles.get(profile_key)
    if not profile:
        raise ValueError(
            f"Profile '{profile_key}' not found. "
            f"Available: {list(profiles.keys())}"
        )

    regions = profile.get("target_regions") or []
    if not regions:
        raise ValueError(f"Profile '{profile_key}' has no target_regions.")

    target_region = (
        next((r for r in regions if r.get("id") == region_override), None)
        if region_override
        else regions[0]
    )
    if target_region is None:
        raise ValueError(
            f"Region '{region_override}' not found in profile '{profile_key}'."
        )

    # Strip target_regions out of app_context — the chosen one lives at the
    # top level so agents don't have to re-resolve it.
    app_context = {k: v for k, v in profile.items() if k != "target_regions"}

    return {
        "app_context": app_context,
        "target_region": target_region,
        "pipeline_config": config.get("pipeline") or {},
        "research_notes": research_notes,
        "revision": 0,
        "history": [],
    }


# =============================================================================
# Output formatting
# =============================================================================


def print_summary(final_state: dict[str, Any]) -> None:
    app_name = (final_state.get("app_context") or {}).get("app_name", "?")
    region_label = (final_state.get("target_region") or {}).get("label", "?")
    revision = final_state.get("revision", 0)
    score = final_state.get("critic_score")
    approved = final_state.get("approved", False)
    feedback = final_state.get("critic_feedback") or ""
    draft = final_state.get("draft") or "(no draft produced)"

    score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "n/a"
    status = "APPROVED ✅" if approved else "NOT APPROVED ❌"

    bar = "=" * 72
    print(f"\n{bar}")
    print(f" {app_name}  |  {region_label}  |  {status}")
    print(f" revisions: {revision}    final critic score: {score_str}")
    print(bar)
    print("\n[Final Draft]\n")
    print(draft)

    image_url = final_state.get("image_url")
    image_prompt = final_state.get("image_prompt")
    if image_url:
        print(f"\n[Image URL]  {image_url}")
    if image_prompt:
        print(f"[Image Prompt]  {image_prompt}")

    publish_status = final_state.get("publish_status")
    if publish_status:
        print(f"[Publish Status]  {publish_status}")

    if feedback and not approved:
        print("\n[Last Critic Feedback]\n")
        print(feedback)
    print(f"\n{bar}\n")


# =============================================================================
# CLI
# =============================================================================


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the marketing-content pipeline for the active profile."
    )
    parser.add_argument(
        "topic",
        nargs="?",
        help="The research notes / topic the Writer should build a draft around.",
    )
    parser.add_argument(
        "--profile",
        help="Override the `active_profile` from the YAML config.",
    )
    parser.add_argument(
        "--region",
        help="Override target region by id (e.g. 'la_oc'). Defaults to first.",
    )
    parser.add_argument(
        "--config",
        help="Path to the YAML config (overrides APP_CONFIG_PATH).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    topic = args.topic or input("Topic / research notes: ").strip()
    if not topic:
        print("No topic provided. Aborting.", file=sys.stderr)
        return 2

    config = load_config(args.config)
    initial_state = build_initial_state(
        config,
        research_notes=topic,
        profile_override=args.profile,
        region_override=args.region,
    )

    print(
        f"▶ Running pipeline: profile="
        f"{(initial_state['app_context'].get('app_name'))}  "
        f"region={initial_state['target_region'].get('label')}"
    )
    final_state = graph.invoke(initial_state)
    print_summary(final_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
