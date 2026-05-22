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

# .env 를 utils 임포트보다 먼저 로드 — utils 모듈이 import 시점에 os.getenv 로
# 모델명/경로 기본값을 읽기 때문에 순서가 중요하다.
load_dotenv()

from utils.gemini import call_gemini, image_part, parse_json_safely  # noqa: E402
from utils.assets import (  # noqa: E402
    assets_dir_today,
    build_srt,
    download_to,
    generate_voiceover,
)
from utils.brand import (  # noqa: E402
    BRAND_IDENTITY_SYSTEM_PROMPT,
    day_of_week_context,
)

MARKET_URL = "https://www.radiokorea.com/market/"

# 자동화 준비 모드 — 영상 렌더링용 고정 자산 경로
UPLOADS_DIR = "uploads"
LOGO_PATH = "uploads/logo.png"

# 마트명 → ASCII 슬러그. Make.com/Zapier 가 한글 인코딩 없이 '오늘 날짜' 파일을
# 정확히 타겟팅할 수 있도록 고정 매핑한다. 미등록 마트는 정규식 슬러그로 폴백.
MARKET_SLUGS = {
    "H마트": "hmart",
    "가주마켓": "gaju",
    "시온마켓": "zion",
    "갤러리아": "galleria",
    "갤러리아/HK마켓": "galleria",
}

# 편집 재료 자동화 모드 — 에셋 출력 폴더 + 외부 생성 모델
ASSETS_DIR = "assets"
REPLICATE_IMAGE_MODEL = os.getenv(
    "REPLICATE_IMAGE_MODEL", "black-forest-labs/flux-schnell"
)
REPLICATE_VIDEO_MODEL = os.getenv(
    "REPLICATE_VIDEO_MODEL", "stability-ai/stable-video-diffusion"
)
ELEVENLABS_MART_VOICE_ID = os.getenv("ELEVENLABS_MART_VOICE_ID", "")
MAX_CLIPS = int(os.getenv("MAX_CLIPS", "2"))   # 핫딜 영상 클립 개수 (비용 제어)

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

def _build_deal_prompt(market_name: str, day_context: str = "") -> str:
    """JSON 스키마를 강제하는 핫딜 추출 프롬프트. day_context 로 요일 톤 주입."""
    schema = (
        '{\n'
        f'  "market_name": "{market_name}",\n'
        '  "video_hook": "이번 주 [마트이름], 이 가격 실화인가요? 🛒",\n'
        '  "top_deals": [ {"item": "상품명", "price": "가격"} ],\n'
        '  "hashtags": ["#미국일상", "#LA마트", "#어바인", "#미국물가"],\n'
        '  "call_to_action": "더 많은 우리 동네 마켓정보는 깨알톡에서 확인하세요!"\n'
        '}'
    )
    context_block = (
        f"\n[오늘의 컨텍스트]\n{day_context}\n위 컨텍스트에 어울리는 상품을 "
        "우선적으로 골라줘.\n" if day_context else ""
    )
    return (
        "너는 미국 한인 마트 전문 마케터야. 첨부된 마트 전단지 이미지에서 가장 "
        "할인폭이 크고 사람들을 혹하게 할 매력적인 핫딜(고기, 과일, 채소 등) "
        "3~5개를 찾아. 그리고 반드시 아래 JSON 형식으로만 응답해. 절대 "
        "마크다운(```json 등)이나 다른 텍스트를 붙이지 마.\n"
        f"{context_block}\n"
        f"마트 이름: {market_name}\n\n"
        f"JSON 형식:\n{schema}"
    )


def extract_deals_from_image(
    image_bytes: bytes, market_name: str, day_context: str = ""
) -> dict | None:
    """
    이미지 바이트를 Gemini 에 전달해 핫딜 JSON 을 추출한다. 실패 시 None.
    Gemini 호출/파싱은 utils.gemini 로 위임한다.
    """
    img = image_part(image_bytes, "image/jpeg")
    if img is None:
        return None

    raw = call_gemini(
        [_build_deal_prompt(market_name, day_context), img],
        temperature=0.4,
        max_output_tokens=4096,
        response_mime_type="application/json",
    )
    if not raw:
        return None
    return parse_json_safely(raw)


# =============================================================================
# 4. 핫딜 JSON → InVideo AI 입력용 틱톡 대본
# =============================================================================


def _build_script_prompt(deals_json: dict, day_context: str = "") -> str:
    """핫딜 JSON 을 넣어 틱톡 대본을 요청하는 프롬프트. day_context 로 요일 톤 주입."""
    data_str = json.dumps(deals_json, ensure_ascii=False, indent=2)
    context_block = (
        f"[오늘의 컨텍스트]\n{day_context}\n이 컨텍스트에 맞는 톤과 소구점으로 "
        "대본을 작성해.\n\n" if day_context else ""
    )
    return (
        "너는 틱톡 숏폼 대본 작가야. 제공된 마트 세일 JSON 데이터를 바탕으로 "
        "15초 분량의 빠르고 경쾌한 정보성 틱톡 대본을 작성해.\n\n"
        f"{context_block}"
        "영상 지시문(B-roll 시각 자료)과 나레이션(Voiceover)을 구분해서 적어줘.\n\n"
        "마지막 클로징(CTA) 나레이션에는 반드시 '애플 앱스토어와 구글 플레이에서 "
        "깨알톡을 검색하세요'라는 멘트가 자연스럽게 들어가야 해.\n\n"
        "대본은 TTS 엔진이 읽기 편한 구어체로 작성할 것. 가격은 '9달러 99센트'처럼 "
        "읽기 쉽게 풀어쓰고($9.99 → 9달러 99센트), 복잡한 특수문자(#, *, /, ~, $ 등)는 "
        "나레이션에서 제거해.\n\n"
        f"[마트 세일 JSON 데이터]\n{data_str}"
    )


def generate_video_script(
    deals_json: dict,
    image_bytes: bytes | None = None,
    day_context: str = "",
) -> str | None:
    """
    핫딜 JSON 을 Gemini 에 전달해 틱톡 대본(B-roll 지시문 + Voiceover)을 생성.
    실패 시 None.

    - Brand Identity 가이드를 system_instruction 으로 무조건 주입한다.
    - image_bytes 가 주어지면 전단지를 Evidence Source 로 첨부한다.
    """
    contents: list = [_build_script_prompt(deals_json, day_context)]
    if image_bytes:
        img = image_part(image_bytes, "image/jpeg")
        if img is not None:
            contents.append(img)

    script = call_gemini(
        contents,
        system_instruction=BRAND_IDENTITY_SYSTEM_PROMPT,
        temperature=0.8,
        max_output_tokens=4096,
    )
    if not script:
        print("[경고] 대본 생성 실패 또는 빈 응답.")
        return None
    return script


# =============================================================================
# 5. 자동화 준비 — Evidence 이미지 매핑 + 렌더링 요청 패키지
# =============================================================================


def _market_slug(market_name: str) -> str:
    """마트명을 ASCII 슬러그로. 미등록 마트는 정규식 슬러그(소문자)로 폴백."""
    if market_name in MARKET_SLUGS:
        return MARKET_SLUGS[market_name]
    return (re.sub(r"[^\w가-힣]+", "_", market_name).strip("_") or "market").lower()


def _fallback_script(market_name: str) -> str:
    """
    이미지 분석 실패 또는 가격 데이터가 비어있을 때 쓰는 범용 대본.
    특정 상품 가격을 말하려 애쓰지 않고 앱 방문만 유도한다.
    """
    return (
        f"오늘 {market_name} 세일 정보, 깨알톡 앱에서 지금 바로 확인해보세요! "
        "놓치면 후회할 득템 기회, 지금 앱을 켜세요!"
    )


def save_evidence_image(image_bytes: bytes, market_name: str) -> str:
    """
    다운로드한 전단지를 uploads/ 폴더에 저장하고 경로를 반환한다.
    이 경로가 영상 렌더링 시 'Evidence Source' 로 지정된다.

    파일명: flyer_<슬러그>_<YYYYMMDD>.jpg (예: flyer_hmart_20260521.jpg)
    외부 자동화 툴이 '오늘 날짜' 파일을 정확히 타겟팅할 수 있게 한다.
    """
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    path = os.path.join(UPLOADS_DIR, f"flyer_{_market_slug(market_name)}_{today}.jpg")
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


# =============================================================================
# 6. 편집 재료 자동화 (Replicate 이미지/영상 + ElevenLabs 보이스)
# =============================================================================


def _item_slug(name: str, idx: int) -> str:
    """핫딜 상품명을 ASCII 슬러그로. 한글 등 비ASCII면 deal{idx} 로 폴백."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name or "").strip("_").lower()
    return slug or f"deal{idx}"


def _save_replicate_output(output, out_path: str) -> bool:
    """
    Replicate 응답은 SDK 버전에 따라 (a) URL 문자열, (b) URL 리스트,
    (c) FileOutput 객체(.read()/.url) 로 올 수 있어 모두 방어 처리한다.
    """
    item = output[0] if isinstance(output, (list, tuple)) and output else output
    if item is None:
        print("[에러] Replicate 출력이 비어 있음.")
        return False

    read = getattr(item, "read", None)
    if callable(read):
        try:
            with open(out_path, "wb") as f:
                f.write(item.read())
            return True
        except Exception as e:
            print(f"[에러] Replicate read 실패: {e!r}")
            return False

    url_attr = getattr(item, "url", None)
    if callable(url_attr):
        url = url_attr()
    elif isinstance(url_attr, str):
        url = url_attr
    elif isinstance(item, str):
        url = item
    else:
        url = str(item)
    return download_to(url, out_path)


def generate_image_asset(prompt: str, out_path: str) -> str | None:
    """Replicate 이미지 모델로 핫딜 씬 이미지를 생성해 out_path 에 저장."""
    if not os.getenv("REPLICATE_API_TOKEN"):
        print("[건너뜀] REPLICATE_API_TOKEN 미설정 — 이미지 생성 스킵.")
        return None
    try:
        import replicate
    except ImportError:
        print("[에러] replicate 미설치. `pip install replicate`")
        return None

    try:
        output = replicate.run(
            REPLICATE_IMAGE_MODEL,
            input={"prompt": prompt, "aspect_ratio": "9:16"},
        )
    except Exception as e:
        print(f"[에러] Replicate 이미지 생성 실패: {e!r}")
        return None

    return out_path if _save_replicate_output(output, out_path) else None


def generate_video_clip(
    prompt: str, out_path: str, init_image_path: str | None = None
) -> str | None:
    """
    Replicate 영상 모델(Luma / Stable Video 등)로 9:16 클립을 생성.
    init_image_path 가 있으면 image-to-video 로 첨부한다.
    주의: 모델별 input 키가 달라(SVD=input_image, Luma=prompt) 필요시 조정.
    """
    if not os.getenv("REPLICATE_API_TOKEN"):
        print("[건너뜀] REPLICATE_API_TOKEN 미설정 — 영상 생성 스킵.")
        return None
    try:
        import replicate
    except ImportError:
        print("[에러] replicate 미설치. `pip install replicate`")
        return None

    model_input: dict = {"prompt": prompt}
    fh = None
    if init_image_path and os.path.isfile(init_image_path):
        fh = open(init_image_path, "rb")
        model_input["input_image"] = fh

    try:
        output = replicate.run(REPLICATE_VIDEO_MODEL, input=model_input)
    except Exception as e:
        print(f"[에러] Replicate 영상 생성 실패: {e!r}")
        return None
    finally:
        if fh:
            fh.close()

    return out_path if _save_replicate_output(output, out_path) else None


def extract_narration(script: str) -> str:
    """대본에서 순수 나레이션(Voiceover)만 추출 — TTS 입력용. 실패 시 원본 반환."""
    text = call_gemini(
        [
            "다음 틱톡 대본에서 성우가 실제로 읽을 '나레이션(Voiceover)' 문장만 "
            "순서대로 이어 붙여서 출력해. B-roll 영상 지시문, 장면 번호, 대괄호 "
            "표시는 모두 제거하고 순수하게 말할 텍스트만 한 덩어리로 줘.\n\n"
            f"[대본]\n{script}"
        ],
        temperature=0.2,
        max_output_tokens=2048,
    )
    return text or script


def build_asset_package(
    market: str,
    deals: dict,
    script: str,
    evidence_path: str,
    day_context: str = "",
) -> str:
    """
    assets/YYYYMMDD/ 아래에 CapCut 수동 조립용 재료 패키지를 생성한다:
        01_<item>.png/.mp4   — Replicate 이미지/영상
        prompts.json         — 씬별 Replicate 프롬프트 (재생성용)
        voiceover_script.txt — ElevenLabs 에 던질 순수 한글 나레이션
        captions.srt         — CapCut 드래그용 자막 (가상 타임코드)
        voiceover.mp3        — ElevenLabs 보이스
        flyer_data.json      — 통합 메타 + 에셋 매니페스트
    각 외부 API 키/패키지가 없으면 해당 에셋만 건너뛰고 계속 진행한다.
    """
    out_dir = assets_dir_today()
    print(f"[Assets] 편집 재료 폴더: {out_dir}")
    asset_files: list[str] = []
    scene_prompts: list[dict] = []

    # 1) 핫딜별 이미지 → 영상 클립 (상위 MAX_CLIPS 개)
    top_deals = (deals.get("top_deals") or [])[:MAX_CLIPS]
    for i, deal in enumerate(top_deals, start=1):
        item = (deal.get("item") or "").strip()
        price = (deal.get("price") or "").strip()
        slug = _item_slug(item, i)
        scene_prompt = (
            f"Photorealistic 9:16 short-form video scene for a Korean grocery deal: "
            f"{item} ({price}). Bright, appetizing, real product photography style. "
            f"No text overlays."
        )
        scene_prompts.append(
            {"scene": i, "item": item, "price": price, "prompt": scene_prompt}
        )
        img_path = os.path.join(out_dir, f"{i:02d}_{slug}.png")
        clip_path = os.path.join(out_dir, f"{i:02d}_{slug}.mp4")

        print(f"[Assets] ({i}/{len(top_deals)}) '{item}' 이미지 생성...")
        made_img = generate_image_asset(scene_prompt, img_path)
        if made_img:
            asset_files.append(os.path.basename(img_path))

        print(f"[Assets] ({i}/{len(top_deals)}) '{item}' 영상 클립 생성...")
        made_clip = generate_video_clip(
            scene_prompt, clip_path, init_image_path=made_img
        )
        if made_clip:
            asset_files.append(os.path.basename(clip_path))

    # 2) prompts.json — 씬별 Replicate 프롬프트 (재생성용)
    prompts_path = os.path.join(out_dir, "prompts.json")
    with open(prompts_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "image_model": REPLICATE_IMAGE_MODEL,
                "video_model": REPLICATE_VIDEO_MODEL,
                "scenes": scene_prompts,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    asset_files.append("prompts.json")

    # 3) 나레이션 → voiceover_script.txt + captions.srt + voiceover.mp3
    print("[Assets] 나레이션 추출...")
    narration = extract_narration(script)

    vo_txt_path = os.path.join(out_dir, "voiceover_script.txt")
    with open(vo_txt_path, "w", encoding="utf-8") as f:
        f.write(narration)
    asset_files.append("voiceover_script.txt")

    srt_path = os.path.join(out_dir, "captions.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(build_srt(narration))
    asset_files.append("captions.srt")

    print("[Assets] 보이스 생성...")
    vo_path = os.path.join(out_dir, "voiceover.mp3")
    if generate_voiceover(narration, vo_path, ELEVENLABS_MART_VOICE_ID):
        asset_files.append("voiceover.mp3")

    # 4) 통합 메타 + 에셋 매니페스트 (flyer_data.json)
    flyer_data = build_render_request(market, deals, script, evidence_path)
    flyer_data["day_context"] = day_context
    flyer_data["narration"] = narration
    flyer_data["asset_files"] = asset_files
    data_path = os.path.join(out_dir, "flyer_data.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(flyer_data, f, ensure_ascii=False, indent=2)

    return out_dir


if __name__ == "__main__":
    # 'H마트' 를 예시로 전체 파이프라인 실행
    market = sys.argv[1] if len(sys.argv) > 1 else "H마트"

    # 요일 분기: 화요일=평일 타임세일, 금요일=주말 바비큐, 그 외=일반
    day_context = day_of_week_context(datetime.now().weekday())

    print(f"=== '{market}' 전단지 핫딜 추출 파이프라인 ===")
    print(f"[Context] 오늘 컨텍스트: {day_context}\n")

    print("[1/5] 전단지 이미지 URL 추출 중...")
    url = get_flyer_image_url(market)
    if not url:
        print("[중단] 이미지 URL 추출 실패.")
        sys.exit(1)
    print(f"      → {url}\n")

    print("[2/5] 이미지 다운로드 중...")
    image_bytes, mime = download_image(url)
    if not image_bytes:
        print("[중단] 이미지 다운로드 실패.")
        sys.exit(1)
    print(f"      → {len(image_bytes):,} bytes ({mime})\n")

    # Evidence Source: 전단지를 uploads/ 에 먼저 매핑 (분석 실패와 무관하게 확보)
    evidence_path = save_evidence_image(image_bytes, market)
    print(f"[Evidence] 전단지 저장 (Evidence Source): {evidence_path}\n")

    print("[3/5] Gemini 2.5 Flash 핫딜 분석 중...")
    deals = None
    try:
        deals = extract_deals_from_image(image_bytes, market, day_context)
    except Exception as e:
        print(f"[경고] 핫딜 분석 중 예외 발생: {e!r}")

    has_deals = bool(deals and deals.get("top_deals"))

    if has_deals:
        print("\n=== 추출 결과 (JSON) ===")
        print(json.dumps(deals, ensure_ascii=False, indent=2))

        print("\n[4/5] Brand Identity 적용 + 틱톡 대본 생성 중...")
        script = None
        try:
            script = generate_video_script(
                deals, image_bytes=image_bytes, day_context=day_context
            )
        except Exception as e:
            print(f"[경고] 대본 생성 중 예외 발생: {e!r}")
        if not script:
            print("[Fallback] 대본 생성 실패 → 범용 대본으로 우회.")
            script = _fallback_script(market)
    else:
        print("[Fallback] 가격 데이터 없음/이미지 분석 실패 → 범용 대본으로 우회.")
        deals = {"market_name": market, "top_deals": []}
        script = _fallback_script(market)

    print("\n=== 틱톡 대본 (Brand Identity 적용) ===")
    print(script)

    # 편집 재료 자동화 모드: assets/YYYYMMDD/ 에 편집용 재료 패키지 생성
    print("\n[5/5] 편집 재료 생성 (Replicate 이미지·영상 + ElevenLabs 보이스)...")
    out_dir = build_asset_package(market, deals, script, evidence_path, day_context)
    print(f"\n[완료] 편집 재료 패키지: {out_dir}/")
    print("        → 01_*.mp4 / voiceover.mp3 / captions.srt 를 CapCut 에 드래그,")
    print("          prompts.json·voiceover_script.txt 로 재생성/재녹음 가능.")
