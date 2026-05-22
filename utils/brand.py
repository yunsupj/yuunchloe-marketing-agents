"""
utils/brand.py — 깨알톡 브랜드 아이덴티티 상수 + 요일별 컨텍스트.

3대장(마트/썰/트렌드) 영상이 공유하는 브랜드 규칙과, 요일에 따라 달라지는
콘텐츠 톤을 한 곳에서 관리한다.
"""

from __future__ import annotations

from datetime import datetime

# 영상 생성 에이전트에 무조건 주입되는 Brand Identity 가이드.
# 사람이 채팅으로 지시하지 않아도 매 렌더링마다 이 규칙이 강제된다.
BRAND_IDENTITY_SYSTEM_PROMPT = (
    "You are Kkaertalk's automated short-form video production agent. "
    "You MUST obey the following Brand Identity guide on every single render, "
    "without needing any human chat instruction:\n\n"
    "1. Brand Color: Always use Kkaertalk Green (#10b981) for all text "
    "highlights and branding elements.\n"
    "2. Logo: Always place the Kkaertalk icon (from uploads/logo.png) in the "
    "corner of every scene.\n"
    "3. Model/Cost: Always use Lite/Standard models for generation to minimize "
    "credit consumption. Prioritize efficiency over Pro-grade features.\n"
    "4. Evidence Source: Prioritize real flyer screenshots and real product "
    "photos over AI-generated graphics or drawings. When mentioning a specific "
    "deal, zoom and pan on the actual price tag from the attached flyer image."
)


def day_of_week_context(weekday: int | None = None) -> str:
    """
    요일별 콘텐츠 컨텍스트를 반환한다.
    weekday: 0=월 ... 1=화, 4=금 (datetime.weekday() 기준). None 이면 오늘.

    - 화요일(1): 평일 퇴근길 타임세일 실속 위주
    - 금요일(4): 주말 바비큐 및 가족 식료품 장보기
    - 그 외     : 일반 세일 정보
    """
    if weekday is None:
        weekday = datetime.now().weekday()
    if weekday == 1:
        return (
            "평일 퇴근길 타임세일 실속 위주 — 바쁜 직장인이 퇴근길에 "
            "빠르게 사갈 수 있는 가성비 핵심딜을 강조한다."
        )
    if weekday == 4:
        return (
            "주말 바비큐 및 가족 식료품 장보기 — 주말 모임/바비큐에 어울리는 "
            "고기·식료품 위주로, 가족 단위 장보기 분위기를 강조한다."
        )
    return "일반 세일 정보 — 이번 주 가장 매력적인 핵심 핫딜을 소개한다."
