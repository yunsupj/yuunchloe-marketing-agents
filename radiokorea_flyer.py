"""
radiokorea_flyer.py — 라디오코리아 마트 전단지 → Gemini 2.0 Flash 핫딜 추출기

파이프라인:
    URL 추출 → 이미지 다운로드 → Gemini Vision 분석 → 핫딜 JSON

실행: python radiokorea_flyer.py
필요 환경변수: GEMINI_API_KEY
"""

import os
import re
import sys
import json
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# .env 파일 로드 (GEMINI_API_KEY 등) — 다른 코드보다 먼저 실행
load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MARKET_URL = "https://www.radiokorea.com/market/"

# 자동화 준비 모드 — 영상 렌더링용 고정 자산 경로
UPLOADS_DIR = "uploads"
LOGO_PATH = "uploads/logo.png"

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": "https://www.radiokorea.com/",
}


def get_flyer_image_url(market_name: str) -> str | None:
    """
    라디오코리아 마트 게시판에서 `market_name`에 해당하는 마트의
    최신 전단지 이미지 URL을 반환한다. 못 찾으면 None.

    매칭 전략: <li class="thumb" title="..."> 의 title 속성을
    1차로 정확히, 2차로 부분 일치(substring)로 비교한다.
    """
    try:
        resp = requests.get(MARKET_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[에러] 페이지 접속 실패: {e!r}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # title 속성을 가진 li.thumb 후보 수집
    candidates = soup.find_all("li", class_="thumb")

    target_li = None
    # 1차: title 정확히 일치
    for li in candidates:
        if (li.get("title") or "").strip() == market_name.strip():
            target_li = li
            break
    # 2차: 부분 일치 (예: "H마트 (남가주)" 같은 변형 대비)
    if target_li is None:
        for li in candidates:
            title = (li.get("title") or "").strip()
            if market_name.strip() in title or title in market_name.strip():
                target_li = li
                break

    if target_li is None:
        available = [
            (li.get("title") or "").strip()
            for li in candidates
            if (li.get("title") or "").strip()
        ]
        print(f"[경고] '{market_name}' 마트를 찾지 못함. 페이지 내 마트 목록: {available}")
        return None

    # 해당 li 내부의 img.flyer 에서 이미지 URL 추출
    img = target_li.find("img", class_="flyer")
    if img is None:
        print(f"[경고] '{market_name}' li 안에 img.flyer 태그가 없음.")
        return None

    # lazy-loading 대비: src 우선, 없으면 data-src 폴백
    img_url = img.get("src") or img.get("data-src")
    if not img_url:
        print(f"[경고] '{market_name}' img 태그에 src/data-src 가 없음.")
        return None

    return img_url.strip()


# =============================================================================
# 2. 이미지 다운로드 (메모리)
# =============================================================================


def download_image(image_url: str) -> tuple[bytes, str] | tuple[None, None]:
    """
    전단지 이미지를 메모리로 다운로드한다.
    Returns (image_bytes, mime_type) 또는 실패 시 (None, None).
    """
    try:
        resp = requests.get(image_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[에러] 이미지 다운로드 실패: {e!r}")
        return None, None

    # Content-Type 우선, 없으면 확장자로 추론 (기본 jpeg)
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "png" in content_type:
        mime = "image/png"
    elif "webp" in content_type:
        mime = "image/webp"
    elif "jpeg" in content_type or "jpg" in content_type:
        mime = "image/jpeg"
    else:
        lowered = image_url.lower().split("?")[0]
        if lowered.endswith(".png"):
            mime = "image/png"
        elif lowered.endswith(".webp"):
            mime = "image/webp"
        else:
            mime = "image/jpeg"

    return resp.content, mime


# =============================================================================
# 3. Gemini 2.0 Flash — 핫딜 추출
# =============================================================================

def _build_deal_prompt(market_name: str) -> str:
    """JSON 스키마를 강제하는 핫딜 추출 프롬프트."""
    schema = (
        '{\n'
        f'  "market_name": "{market_name}",\n'
        '  "video_hook": "이번 주 [마트이름], 이 가격 실화인가요? 🛒",\n'
        '  "top_deals": [ {"item": "상품명", "price": "가격"} ],\n'
        '  "hashtags": ["#미국일상", "#LA마트", "#어바인", "#미국물가"],\n'
        '  "call_to_action": "더 많은 우리 동네 마켓정보는 깨알톡에서 확인하세요!"\n'
        '}'
    )
    return (
        "너는 미국 한인 마트 전문 마케터야. 첨부된 마트 전단지 이미지에서 가장 "
        "할인폭이 크고 사람들을 혹하게 할 매력적인 핫딜(고기, 과일, 채소 등) "
        "3~5개를 찾아. 그리고 반드시 아래 JSON 형식으로만 응답해. 절대 "
        "마크다운(```json 등)이나 다른 텍스트를 붙이지 마.\n\n"
        f"마트 이름: {market_name}\n\n"
        f"JSON 형식:\n{schema}"
    )


def _parse_json_safely(raw: str) -> dict | None:
    """
    모델 응답을 json.loads 로 안전하게 파싱한다.

    방어 전략:
      1차 — 응답 전체를 그대로 json.loads
      2차 — 마크다운(```json …```)이나 앞뒤 잡음이 섞여 와도, 정규식으로
            가장 첫 '{' 부터 가장 마지막 '}' 까지(re.DOTALL)만 발라내 재시도
    """
    if not raw:
        print("[경고] 모델 응답이 비어 있음 (max_output_tokens 소진 가능성).")
        return None

    text = raw.strip()

    # 1차: 응답 전체가 순수 JSON 인 경우
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2차: 첫 '{' ~ 마지막 '}' 만 정규식으로 추출 (greedy + DOTALL)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        clean_json = match.group(0)
        try:
            return json.loads(clean_json)
        except json.JSONDecodeError as e:
            print(f"[경고] JSON 파싱 실패: {e}")
            print(f"[디버그] 추출 시도한 구간(앞 300자): {clean_json[:300]!r}")
    else:
        print("[경고] 응답에서 JSON 객체({{ … }})를 찾지 못함.")
        print(f"[디버그] 원본 응답(앞 300자): {text[:300]!r}")

    return None


def extract_deals_from_image(image_bytes: bytes, market_name: str) -> dict | None:
    """
    이미지 바이트를 gemini-2.0-flash 에 전달해 핫딜 JSON 을 추출한다.
    실패 시 None.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[에러] GEMINI_API_KEY 환경변수가 설정되지 않음.")
        return None

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[에러] google-genai 미설치. `pip install google-genai`")
        return None

    client = genai.Client(api_key=api_key)

    prompt = _build_deal_prompt(market_name)
    image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, image_part],
            config=types.GenerateContentConfig(
                temperature=0.4,
                max_output_tokens=4096,
                # 모델이 순수 JSON 만 내도록 강제 (마크다운 방지)
                response_mime_type="application/json",
            ),
        )
        raw = (getattr(response, "text", None) or "").strip()
    except Exception as e:
        print(f"[에러] Gemini 호출 실패: {e!r}")
        return None

    return _parse_json_safely(raw)


# =============================================================================
# 4. 핫딜 JSON → InVideo AI 입력용 틱톡 대본
# =============================================================================


def _build_script_prompt(deals_json: dict) -> str:
    """핫딜 JSON 을 넣어 틱톡 대본을 요청하는 프롬프트."""
    data_str = json.dumps(deals_json, ensure_ascii=False, indent=2)
    return (
        "너는 틱톡 숏폼 대본 작가야. 제공된 마트 세일 JSON 데이터를 바탕으로 "
        "15초 분량의 빠르고 경쾌한 정보성 틱톡 대본을 작성해.\n\n"
        "영상 지시문(B-roll 시각 자료)과 나레이션(Voiceover)을 구분해서 적어줘.\n\n"
        "마지막 클로징(CTA) 나레이션에는 반드시 '애플 앱스토어와 구글 플레이에서 "
        "깨알톡을 검색하세요'라는 멘트가 자연스럽게 들어가야 해.\n\n"
        f"[마트 세일 JSON 데이터]\n{data_str}"
    )


def generate_video_script(
    deals_json: dict, image_bytes: bytes | None = None
) -> str | None:
    """
    핫딜 JSON 을 gemini-2.5-flash 에 전달해 InVideo AI 입력용 틱톡 대본
    (B-roll 지시문 + Voiceover)을 생성한다. 실패 시 None.

    - Brand Identity 가이드를 system_instruction 으로 무조건 주입한다.
    - image_bytes 가 주어지면 전단지를 Evidence Source 로 첨부해, 실제
      가격표/상품 사진을 근거로 대본을 작성하게 만든다.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[에러] GEMINI_API_KEY 환경변수가 설정되지 않음.")
        return None

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[에러] google-genai 미설치. `pip install google-genai`")
        return None

    client = genai.Client(api_key=api_key)
    prompt = _build_script_prompt(deals_json)

    # 전단지 이미지를 Evidence Source 로 첨부 (실제 가격표 기반 대본)
    contents: list = [prompt]
    if image_bytes:
        contents.append(
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        )

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                # Brand Identity 가이드 무조건 주입
                system_instruction=BRAND_IDENTITY_SYSTEM_PROMPT,
                temperature=0.8,
                max_output_tokens=4096,
            ),
        )
        script = (getattr(response, "text", None) or "").strip()
    except Exception as e:
        print(f"[에러] Gemini 대본 생성 실패: {e!r}")
        return None

    if not script:
        print("[경고] 대본 응답이 비어 있음 (max_output_tokens 소진 가능성).")
        return None
    return script


# =============================================================================
# 5. 자동화 준비 — Evidence 이미지 매핑 + 렌더링 요청 패키지
# =============================================================================


def save_evidence_image(image_bytes: bytes, market_name: str) -> str:
    """
    다운로드한 전단지를 uploads/ 폴더에 저장하고 경로를 반환한다.
    이 경로가 영상 렌더링 시 'Evidence Source' 로 지정된다.
    """
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    safe = re.sub(r"[^\w가-힣]+", "_", market_name).strip("_") or "market"
    path = os.path.join(UPLOADS_DIR, f"flyer_{safe}.jpg")
    with open(path, "wb") as f:
        f.write(image_bytes)
    return path


def build_render_request(
    market_name: str,
    deals_json: dict,
    script: str,
    evidence_path: str,
) -> dict:
    """
    영상 렌더링 백엔드(InVideo / Creatomate 등)에 그대로 넘길 수 있는
    '자동화 준비 모드' 요청 패키지를 만든다. Brand Identity 와 Evidence
    Source 가 페이로드에 고정되어 있어 사람이 매번 지시할 필요가 없다.
    """
    return {
        "generated_at": datetime.now().isoformat(),
        "market_name": market_name,
        "brand_identity": {
            "brand_color": "#10b981",
            "logo_path": LOGO_PATH,
            "model_tier": "lite-standard",
        },
        "evidence_source": evidence_path,
        "script": script,
        "deals": deals_json,
    }


if __name__ == "__main__":
    # 'H마트' 를 예시로 전체 파이프라인 실행
    market = sys.argv[1] if len(sys.argv) > 1 else "H마트"

    print(f"=== '{market}' 전단지 핫딜 추출 파이프라인 ===\n")

    print("[1/4] 전단지 이미지 URL 추출 중...")
    url = get_flyer_image_url(market)
    if not url:
        print("[중단] 이미지 URL 추출 실패.")
        sys.exit(1)
    print(f"      → {url}\n")

    print("[2/4] 이미지 다운로드 중...")
    image_bytes, mime = download_image(url)
    if not image_bytes:
        print("[중단] 이미지 다운로드 실패.")
        sys.exit(1)
    print(f"      → {len(image_bytes):,} bytes ({mime})\n")

    print("[3/4] Gemini 2.5 Flash 핫딜 분석 중...")
    deals = extract_deals_from_image(image_bytes, market)
    if not deals:
        print("[중단] 핫딜 추출 실패.")
        sys.exit(1)

    print("\n=== 추출 결과 (JSON) ===")
    print(json.dumps(deals, ensure_ascii=False, indent=2))

    # Evidence Source: 전단지를 uploads/ 에 매핑
    evidence_path = save_evidence_image(image_bytes, market)
    print(f"\n[Evidence] 전단지 저장 (Evidence Source): {evidence_path}")

    print("\n[4/4] Brand Identity 적용 + 틱톡 대본 생성 중...")
    script = generate_video_script(deals, image_bytes=image_bytes)
    if not script:
        print("[중단] 대본 생성 실패.")
        sys.exit(1)

    print("\n=== InVideo AI 입력용 틱톡 대본 (Brand Identity 적용) ===")
    print(script)

    # 자동화 준비 모드: 렌더링 요청 패키지 저장
    render_request = build_render_request(market, deals, script, evidence_path)
    safe = re.sub(r"[^\w가-힣]+", "_", market).strip("_") or "market"
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = f"render_request_{safe}_{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(render_request, f, ensure_ascii=False, indent=2)
    print(f"\n[Render] 렌더링 요청 패키지 저장: {out_path}")
    print("        → 이 JSON 을 영상 렌더링 백엔드(InVideo/Creatomate)에 전달하면 됩니다.")
