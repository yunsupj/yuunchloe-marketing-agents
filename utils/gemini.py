"""
utils/gemini.py — 3대장 공통 Gemini 헬퍼.

마트(radiokorea_flyer) · 썰(local_story_crafter) · 트렌드(trend_copy) 가
공유하는 Gemini 로직을 한 곳에 모은다:
    - get_gemini_client() : google-genai Client 생성
    - call_gemini()       : generate_content 호출 래퍼
    - image_part()        : 이미지 바이트 → Gemini Part
    - parse_json_safely() : 마크다운/잡음 섞인 응답에서 JSON만 발라내기
"""

from __future__ import annotations

import os
import re
import json
from typing import Any

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def get_gemini_client():
    """google-genai Client 반환. 키/패키지 없으면 None."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[Gemini] GEMINI_API_KEY 미설정.")
        return None
    try:
        from google import genai
    except ImportError:
        print("[Gemini] google-genai 미설치. `pip install google-genai`")
        return None
    return genai.Client(api_key=api_key)


def image_part(image_bytes: bytes, mime_type: str = "image/jpeg"):
    """이미지 바이트를 Gemini contents 에 넣을 Part 로 변환. 실패 시 None."""
    try:
        from google.genai import types
    except ImportError:
        print("[Gemini] google-genai 미설치.")
        return None
    return types.Part.from_bytes(data=image_bytes, mime_type=mime_type)


def call_gemini(
    contents: list,
    *,
    system_instruction: str | None = None,
    temperature: float = 0.4,
    max_output_tokens: int = 2048,
    response_mime_type: str | None = None,
) -> str | None:
    """
    Gemini generate_content 호출 래퍼. 응답 텍스트(str) 또는 None.

    contents 에는 프롬프트 문자열 + (선택) image_part() 결과를 넣는다.
    response_mime_type="application/json" 을 주면 모델이 순수 JSON 만 내도록 강제.
    """
    client = get_gemini_client()
    if client is None:
        return None
    try:
        from google.genai import types
    except ImportError:
        print("[Gemini] google-genai 미설치.")
        return None

    cfg_kwargs: dict[str, Any] = {
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
    }
    if system_instruction:
        cfg_kwargs["system_instruction"] = system_instruction
    if response_mime_type:
        cfg_kwargs["response_mime_type"] = response_mime_type

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        return (getattr(response, "text", None) or "").strip()
    except Exception as e:
        print(f"[Gemini] 호출 실패: {e!r}")
        return None


def parse_json_safely(raw: str) -> dict | None:
    """
    모델 응답을 json.loads 로 안전하게 파싱한다.

    방어 전략:
      1차 — 응답 전체를 그대로 json.loads
      2차 — 마크다운(```json …```)이나 앞뒤 잡음이 섞여 와도, 정규식으로
            가장 첫 '{' 부터 가장 마지막 '}' 까지(re.DOTALL)만 발라내 재시도
    """
    if not raw:
        print("[Gemini] 응답이 비어 있음 (max_output_tokens 소진 가능성).")
        return None

    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        clean_json = match.group(0)
        try:
            return json.loads(clean_json)
        except json.JSONDecodeError as e:
            print(f"[Gemini] JSON 파싱 실패: {e}")
            print(f"[디버그] 추출 구간(앞 300자): {clean_json[:300]!r}")
    else:
        print("[Gemini] 응답에서 JSON 객체를 찾지 못함.")
        print(f"[디버그] 원본(앞 300자): {text[:300]!r}")

    return None
