"""
viral_hacker.py — 3번 기둥: 트렌드 카피 콘텐츠.

Apify 로 틱톡 트렌드를 수집·분석하고, 상위 바이럴 영상의 후킹 스타일을
벤치마킹한 깨알톡/피클 홍보용 CapCut 조립 에셋을 생성한다.

파이프라인:
    Apify(tiktok-scraper) → engagement 스코어링 상위 5개
    → Gemini(트렌드 카피 대본 + 영어 배경 프롬프트)
    → assets/YYYYMMDD_viral_<keyword>/ 에 prompts.json /
      voiceover_script.txt / captions.srt / voiceover.mp3 저장

필요 환경변수:
    APIFY_API_TOKEN
    GEMINI_API_KEY
    ELEVENLABS_API_KEY, ELEVENLABS_MART_VOICE_ID
"""

from __future__ import annotations

import os
import re
import json
import random

from dotenv import load_dotenv

# .env 를 utils 임포트보다 먼저 로드 (utils 가 import 시점에 os.getenv 사용).
load_dotenv()

from utils.gemini import call_gemini, parse_json_safely  # noqa: E402
from utils.assets import (  # noqa: E402
    assets_dir_today,
    build_srt,
    generate_voiceover,
)

APIFY_ACTOR = "clockworks/tiktok-scraper"
RESULTS_PER_PAGE = 20
TOP_N = 5
MART_VOICE_ID = os.getenv("ELEVENLABS_MART_VOICE_ID", "")  # 텐션 높은 목소리

SEARCH_KEYWORDS = ["미국 마트", "한인타운 꿀팁", "미국 물가"]

# 키워드 → ASCII 슬러그 (폴더명용). 미등록 키워드는 정규식 슬러그로 폴백.
KEYWORD_SLUGS = {
    "미국 마트": "us_mart",
    "한인타운 꿀팁": "ktown_tips",
    "미국 물가": "us_prices",
}

# Gemini system instruction — 바이럴 트렌드 카피 마케터 페르소나.
VIRAL_SYSTEM_PROMPT = (
    "너는 틱톡 바이럴 마케터야. 제공된 상위 5개 바이럴 영상의 제목과 후킹 "
    "포인트를 분석해서, 우리 앱(미국 한인 동네 정보 앱 '깨알톡' 또는 로컬 "
    "중고거래 '피클')을 홍보하는 15초짜리 트렌드 카피 대본을 만들어. 후킹 "
    "문구는 원본 바이럴 영상들의 스타일을 그대로 벤치마킹해."
)


# =============================================================================
# 1. Apify 연동 + 데이터 수집
# =============================================================================


def _keyword_slug(keyword: str) -> str:
    """키워드를 ASCII 슬러그로. 미등록이면 정규식 슬러그(소문자)로 폴백."""
    if keyword in KEYWORD_SLUGS:
        return KEYWORD_SLUGS[keyword]
    return (re.sub(r"[^a-zA-Z0-9]+", "_", keyword).strip("_") or "viral").lower()


def fetch_tiktok_videos(keyword: str) -> list[dict]:
    """clockworks/tiktok-scraper 로 키워드 검색 결과(메타데이터)를 수집한다."""
    token = os.getenv("APIFY_API_TOKEN")
    if not token:
        print("[Viral] APIFY_API_TOKEN 미설정.")
        return []
    try:
        from apify_client import ApifyClient
    except ImportError:
        print("[Viral] apify-client 미설치. `pip install apify-client`")
        return []

    try:
        client = ApifyClient(token)
        run = client.actor(APIFY_ACTOR).call(
            run_input={
                "searchQueries": [keyword],
                "resultsPerPage": RESULTS_PER_PAGE,
                "shouldDownloadVideos": False,
                "shouldDownloadCovers": False,
                "shouldDownloadSubtitles": False,
            }
        )
    except Exception as e:
        print(f"[Viral] Apify Actor 실행 실패: {e!r}")
        return []

    dataset_id = (run or {}).get("defaultDatasetId")
    if not dataset_id:
        print("[Viral] Apify 실행 결과에 dataset 이 없음.")
        return []

    try:
        return list(client.dataset(dataset_id).iterate_items())
    except Exception as e:
        print(f"[Viral] Apify dataset 조회 실패: {e!r}")
        return []


# =============================================================================
# 2. 바이럴 데이터 정제 (Scoring)
# =============================================================================


def _engagement_score(item: dict) -> int:
    """engagement = views + likes*3 + comments*5 + shares*10."""
    views = item.get("playCount") or 0
    likes = item.get("diggCount") or 0
    comments = item.get("commentCount") or 0
    shares = item.get("shareCount") or 0
    return views + likes * 3 + comments * 5 + shares * 10


def top_viral_videos(items: list[dict], n: int = TOP_N) -> list[dict]:
    """engagement 상위 n개의 title / hashtags / musicName 을 추출한다."""
    ranked = sorted(items, key=_engagement_score, reverse=True)[:n]
    result = []
    for it in ranked:
        if not isinstance(it, dict):
            continue
        hashtags = [
            h.get("name")
            for h in (it.get("hashtags") or [])
            if isinstance(h, dict) and h.get("name")
        ]
        music = ((it.get("musicMeta") or {}).get("musicName") or "").strip()
        result.append(
            {
                "title": (it.get("text") or "").strip(),
                "hashtags": hashtags,
                "music": music,
                "engagement_score": _engagement_score(it),
            }
        )
    return result


# =============================================================================
# 3. Gemini 프롬프트 + 트렌드 카피 대본
# =============================================================================


def _build_viral_prompt(top_videos: list[dict]) -> str:
    """상위 5개 바이럴 데이터를 넣어 트렌드 카피 대본을 요청한다."""
    data_str = json.dumps(top_videos, ensure_ascii=False, indent=2)
    schema = (
        '{\n'
        '  "replicate_prompt": "<영어 배경 영상 프롬프트>",\n'
        '  "voiceover_script": "<15초 한글 나레이션>",\n'
        '  "strategy_summary": "<어떤 바이럴 요소를 카피했는지 1줄 요약>"\n'
        '}'
    )
    return (
        "아래는 최근 인기 틱톡 영상 상위 5개의 제목/해시태그/음원 데이터야.\n\n"
        f"[상위 5개 바이럴 영상]\n{data_str}\n\n"
        "이 데이터의 후킹 스타일을 벤치마킹해서, 반드시 아래 JSON 형식으로만 "
        "응답해. 마크다운(```json 등)이나 다른 텍스트는 절대 붙이지 마.\n\n"
        f"{schema}"
    )


def craft_viral_script(top_videos: list[dict]) -> dict | None:
    """Gemini 호출 → {replicate_prompt, voiceover_script, strategy_summary} 또는 None."""
    raw = call_gemini(
        [_build_viral_prompt(top_videos)],
        system_instruction=VIRAL_SYSTEM_PROMPT,
        temperature=0.9,
        max_output_tokens=2048,
        response_mime_type="application/json",
    )
    if not raw:
        return None
    return parse_json_safely(raw)


# =============================================================================
# 4. 출력 — 보이스 + 자막 + 패키지
# =============================================================================


def build_viral_package(
    keyword: str, top_videos: list[dict], crafted: dict
) -> str | None:
    """
    트렌드 카피 결과를 assets/YYYYMMDD_viral_<keyword>/ 에 저장한다.
    나레이션이 비어 있으면 None.
    """
    replicate_prompt = (crafted.get("replicate_prompt") or "").strip()
    narration = (crafted.get("voiceover_script") or "").strip()
    strategy = (crafted.get("strategy_summary") or "").strip()
    if not narration:
        print("[중단] voiceover_script 가 비어 있음.")
        return None

    out_dir = f"{assets_dir_today()}_viral_{_keyword_slug(keyword)}"
    os.makedirs(out_dir, exist_ok=True)
    print(f"[Viral] 출력 폴더: {out_dir}")

    # prompts.json — Replicate 프롬프트 + 바이럴 분석 요약 + 벤치마킹한 원본
    with open(os.path.join(out_dir, "prompts.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "type": "viral",
                "keyword": keyword,
                "replicate_prompt": replicate_prompt,
                "strategy_summary": strategy,
                "benchmarked_videos": top_videos,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # voiceover_script.txt — 순수 한글 나레이션
    with open(
        os.path.join(out_dir, "voiceover_script.txt"), "w", encoding="utf-8"
    ) as f:
        f.write(narration)

    # captions.srt — 2초 단위 가상 타임코드 자막
    with open(os.path.join(out_dir, "captions.srt"), "w", encoding="utf-8") as f:
        f.write(build_srt(narration))

    # voiceover.mp3 — ElevenLabs (텐션 높은 보이스)
    print("[Viral] 보이스 생성 중...")
    generate_voiceover(
        narration, os.path.join(out_dir, "voiceover.mp3"), MART_VOICE_ID
    )

    return out_dir


# =============================================================================
# 메인
# =============================================================================


def main() -> int:
    keyword = random.choice(SEARCH_KEYWORDS)
    print(f"=== viral_hacker — 트렌드 카피 에셋 생성 (키워드: '{keyword}') ===")

    print("[1/3] Apify 틱톡 트렌드 수집 중...")
    items = fetch_tiktok_videos(keyword)
    if not items:
        print("[종료] 수집된 영상이 없습니다.")
        return 1
    print(f"      → {len(items)}개 수집")

    print("[2/3] engagement 스코어링 상위 5개 추출...")
    top_videos = top_viral_videos(items)
    if not top_videos:
        print("[종료] 상위 영상 추출 실패.")
        return 1
    for i, v in enumerate(top_videos, 1):
        print(f"      {i}. score={v['engagement_score']:,} | {v['title'][:40]}")

    print("\n[3/3] Gemini 트렌드 카피 대본 생성 중...")
    crafted = craft_viral_script(top_videos)
    if not crafted:
        print("[종료] 대본 생성 실패.")
        return 1

    out_dir = build_viral_package(keyword, top_videos, crafted)
    if out_dir is None:
        return 1

    print(f"\n[완료] 편집 재료 패키지: {out_dir}/")
    print(f"        전략 요약: {crafted.get('strategy_summary', '')}")
    print("        → voiceover.mp3 / captions.srt 를 CapCut 에 드래그,")
    print("          prompts.json 의 replicate_prompt 로 배경 영상 생성.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
