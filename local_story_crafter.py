"""
local_story_crafter.py — 2번 기둥: 공감/썰 콘텐츠.

Supabase `posts` 테이블의 인기 만료글(썰/하소연/질문/꿀팁)을 가져와
CapCut 수동 조립용 에셋 패키지로 변환한다.

파이프라인:
    posts(is_expired) → 중복 체크 → Gemini(공감 대본 + 영어 배경 프롬프트)
    → assets/YYYYMMDD_story_<id>/ 에 prompts.json / voiceover_script.txt /
      captions.srt / voiceover.mp3 저장

필요 환경변수:
    EXPO_PUBLIC_SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
    GEMINI_API_KEY
    ELEVENLABS_API_KEY, ELEVENLABS_STORY_VOICE_ID
"""

from __future__ import annotations

import os
import json

from dotenv import load_dotenv

# .env 를 utils 임포트보다 먼저 로드 (utils 가 import 시점에 os.getenv 사용).
load_dotenv()

from utils.gemini import call_gemini, parse_json_safely  # noqa: E402
from utils.assets import (  # noqa: E402
    assets_dir_today,
    build_srt,
    generate_voiceover,
)

PROCESSED_IDS_FILE = "processed_story_ids.txt"
FETCH_LIMIT = 25   # 중복 제외 후 1개를 고르기 위해 인기순 상위 N개를 받아온다.
STORY_VOICE_ID = os.getenv("ELEVENLABS_STORY_VOICE_ID", "")  # 친근한 목소리

# Gemini system instruction — 공감형 썰 대본 작가 페르소나.
STORY_SYSTEM_PROMPT = (
    "너는 미주 한인 커뮤니티의 틱톡/쇼츠 대본 전문 작가야. 유저가 쓴 "
    "글(썰/하소연/질문/꿀팁)을 바탕으로, 15초 분량의 찰진 1인칭 공감형 "
    "나레이션 대본과 그에 맞는 Replicate 배경 영상 프롬프트를 영어로 작성해."
)


# =============================================================================
# 1. Supabase 연동 + 데이터 추출
# =============================================================================


def _build_supabase_client():
    """supabase-py 클라이언트. 패키지/환경변수 없으면 None."""
    try:
        from supabase import create_client
    except ImportError:
        print("[Story] supabase-py 미설치. `pip install supabase`")
        return None

    url = os.getenv("EXPO_PUBLIC_SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("[Story] Supabase 환경변수 미설정 "
              "(EXPO_PUBLIC_SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY).")
        return None

    try:
        return create_client(url, key)
    except Exception as e:
        print(f"[Story] Supabase 클라이언트 생성 실패: {e!r}")
        return None


def fetch_unprocessed_story() -> dict | None:
    """
    posts 에서 is_expired=True 인 글을 likes 내림차순으로 받아,
    아직 처리하지 않은 첫 번째 글 1개를 반환한다. 없으면 None.
    """
    client = _build_supabase_client()
    if client is None:
        return None

    processed = _load_processed_ids()

    try:
        resp = (
            client.table("posts")
            .select("id, title, content, region, likes, views")
            .eq("is_expired", True)
            .neq("category", "핫딜/쇼핑")
            .order("likes", desc=True)
            .limit(FETCH_LIMIT)
            .execute()
        )
    except Exception as e:
        print(f"[Story] posts 조회 실패: {e!r}")
        return None

    rows = getattr(resp, "data", None) or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("id")) not in processed:
            return row

    print("[Story] 처리할 새 글이 없음 (상위 글이 모두 처리됨 또는 빈 결과).")
    return None


# =============================================================================
# 2. 중복 방지 (로컬 캐시 파일)
# =============================================================================


def _load_processed_ids() -> set[str]:
    if not os.path.isfile(PROCESSED_IDS_FILE):
        return set()
    with open(PROCESSED_IDS_FILE, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def _mark_processed(story_id) -> None:
    with open(PROCESSED_IDS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{story_id}\n")


# =============================================================================
# 3. Gemini 프롬프트 + 대본 생성
# =============================================================================


def _build_story_prompt(title: str, content: str, region: str) -> str:
    """게시글 데이터를 넣어 공감 대본 + 영어 배경 프롬프트를 요청한다."""
    schema = (
        '{\n'
        '  "replicate_prompt": "<15초 배경 영상에 어울리는 영어 프롬프트>",\n'
        '  "voiceover_script": "<찰진 1인칭 공감형 한글 나레이션>"\n'
        '}'
    )
    return (
        f"[게시글 지역]\n{region or '미주 한인 커뮤니티'}\n\n"
        f"[게시글 제목]\n{title}\n\n"
        f"[게시글 내용]\n{content}\n\n"
        "위 글을 바탕으로 반드시 아래 JSON 형식으로만 응답해. 마크다운(```json 등)이나 "
        "다른 텍스트는 절대 붙이지 마.\n\n"
        f"{schema}"
    )


def craft_story_script(title: str, content: str, region: str, max_retries: int = 3) -> dict | None:
    """Gemini 호출 → {replicate_prompt, voiceover_script} (자동 재시도 포함)"""
    for attempt in range(1, max_retries + 1):
        raw = call_gemini(
            [_build_story_prompt(title, content, region)],
            system_instruction=STORY_SYSTEM_PROMPT,
            temperature=0.8,
            max_output_tokens=2048,
            # response_mime_type="application/json",  # 주석 처리 유지
        )

        if not raw:
            print(f"[Gemini] 응답이 비어 있습니다. (시도 {attempt}/{max_retries})")
            continue

        parsed = parse_json_safely(raw)
        if parsed:
            return parsed

        print(f"[Gemini] JSON 파싱 실패. (시도 {attempt}/{max_retries}) 다시 요청합니다...")

    print("[중단] 최대 재시도 횟수를 초과했습니다.")
    return None


# =============================================================================
# 4. 출력 — 보이스 + 자막 + 패키지
# =============================================================================


def build_story_package(story: dict) -> str | None:
    """
    썰 1건을 받아 assets/YYYYMMDD_story_<id>/ 에 CapCut 3종 세트 +
    voiceover.mp3 를 생성한다. 나레이션 생성 실패 시 None.
    """
    story_id = str(story.get("id"))
    title = (story.get("title") or "").strip()
    content = (story.get("content") or "").strip()
    region = (story.get("region") or "").strip()

    print(f"[Story] Gemini 대본 생성 중... (id={story_id})")
    crafted = craft_story_script(title, content, region)
    if not crafted:
        print("[중단] 대본 생성 실패.")
        return None

    replicate_prompt = (crafted.get("replicate_prompt") or "").strip()
    narration = (crafted.get("voiceover_script") or "").strip()
    if not narration:
        print("[중단] voiceover_script 가 비어 있음.")
        return None

    # assets/YYYYMMDD 를 만든 뒤 _story_<id> 폴더를 별도로 생성
    out_dir = f"{assets_dir_today()}_story_{story_id}"
    os.makedirs(out_dir, exist_ok=True)
    print(f"[Story] 출력 폴더: {out_dir}")

    # prompts.json — Replicate 프롬프트 + 원본 게시글 메타데이터
    with open(os.path.join(out_dir, "prompts.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "type": "story",
                "replicate_prompt": replicate_prompt,
                "source_post": {
                    "id": story.get("id"),
                    "title": title,
                    "region": region,
                    "likes": story.get("likes"),
                    "views": story.get("views"),
                },
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

    # voiceover.mp3 — ElevenLabs
    print("[Story] 보이스 생성 중...")
    generate_voiceover(
        narration, os.path.join(out_dir, "voiceover.mp3"), STORY_VOICE_ID
    )

    return out_dir


# =============================================================================
# 메인
# =============================================================================


def main() -> int:
    print("=== local_story_crafter — 공감/썰 에셋 생성 ===")

    story = fetch_unprocessed_story()
    if not story:
        print("[종료] 처리할 글이 없습니다.")
        return 0

    print(
        f"[Story] 선택: id={story.get('id')} "
        f"likes={story.get('likes')} '{story.get('title')}'"
    )

    out_dir = build_story_package(story)
    if out_dir is None:
        # 실패 시 processed 에 기록하지 않아 다음 실행에서 재시도된다.
        print("[종료] 패키지 생성 실패 — id 를 처리완료로 기록하지 않음.")
        return 1

    _mark_processed(story.get("id"))
    print(f"\n[완료] 편집 재료 패키지: {out_dir}/")
    print("        → voiceover.mp3 / captions.srt 를 CapCut 에 드래그,")
    print("          prompts.json 의 replicate_prompt 로 배경 영상 생성.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
