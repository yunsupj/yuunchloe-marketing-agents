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
from utils.brand import day_of_week_context  # noqa: E402

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
# 4. 핫딜 JSON → B급 병맛 광고 대본 (voiceover + 3 scenes + strategy)
# =============================================================================

# 병맛 마케터 페르소나 — 마트 광고 대본 system instruction.
MART_SYSTEM_PROMPT = (
    "너는 미국 한인타운의 '광기 어린 틱톡 병맛 마케터'야. 마트 전단지 데이터를 "
    "받아서 15초짜리 미친 텐션의 광고 대본을 만들어.\n\n"
    "🚨 [톤앤매너]\n"
    "1. 아주 다급한 '긴급 속보'나 '라이브 방송' 느낌으로 시작해 "
    "(예: '아니 사장님 지금 제정신입니까?!', '미국 마트에서 난동 발생!').\n"
    "2. 가격/세일 정보는 진지하게 말하지 마. '이 가격 실화냐?', '지갑 털리는 건 "
    "시간문제', '지금 당장 차 키 챙겨', '사장님 미쳤습니까?' 같은 멘트를 섞어.\n"
    "3. 초성(ㅋㅋ, ㄷㄷ, ㅠㅠ)은 절대 금지. 모든 감탄사는 자연스러운 구어체"
    "('진짜 대박', '소름', '미쳤다')로 풀어써. ElevenLabs 성우가 어색하게 읽지 "
    "않도록 물결표(~) 없이 깔끔한 문장으로 맺어.\n\n"
    "🚨 [시각 연출]\n"
    "1. 캡컷에서 '병맛 밈'으로 편집할 수 있도록 씬을 구체적으로 묘사해 "
    "(예: 1초에 삼겹살이 운석처럼 떨어짐, 5초에 가격표가 폭발, 10초에 파에서 레이저).\n"
    "2. 영상 생성 AI(Replicate)용 씬별 이미지 프롬프트를 정확히 3개 만들어.\n"
    "3. 각 씬마다 캡컷 편집용 효과(effect)를 추천해."
)


def _build_mart_prompt(deals_json: dict, day_context: str = "") -> str:
    """핫딜 JSON 을 넣어 병맛 광고 JSON 을 요청하는 프롬프트. day_context 로 요일 톤 주입."""
    data_str = json.dumps(deals_json, ensure_ascii=False, indent=2)
    context_block = (
        f"[오늘의 컨텍스트]\n{day_context}\n이 컨텍스트도 광고에 녹여줘.\n\n"
        if day_context else ""
    )
    schema = (
        '{\n'
        '  "voiceover_script": "<성우가 읽을 15초 구어체 대본>",\n'
        '  "scenes": [\n'
        '    {"time": "0-5s", "visual_prompt": "<Replicate 이미지 프롬프트 1>", "effect": "<캡컷 효과 추천>"},\n'
        '    {"time": "5-10s", "visual_prompt": "<Replicate 이미지 프롬프트 2>", "effect": "<캡컷 효과 추천>"},\n'
        '    {"time": "10-15s", "visual_prompt": "<Replicate 이미지 프롬프트 3>", "effect": "<캡컷 효과 추천>"}\n'
        '  ],\n'
        '  "marketing_strategy": "<어떤 포인트에서 병맛을 유도했는지 1줄 요약>"\n'
        '}'
    )
    return (
        f"{context_block}"
        f"[마트 세일 JSON 데이터]\n{data_str}\n\n"
        "위 데이터로 미친 텐션의 광고를 만들어. 반드시 아래 JSON 형식으로만 응답해. "
        "마크다운(```json 등)이나 다른 텍스트는 절대 붙이지 마.\n\n"
        f"{schema}"
    )


def craft_mart_script(
    deals_json: dict,
    image_bytes: bytes | None = None,
    day_context: str = "",
) -> dict | None:
    """
    핫딜 JSON 을 Gemini 에 전달해 병맛 광고 JSON
    {voiceover_script, scenes[3], marketing_strategy} 을 생성. 실패 시 None.
    image_bytes 가 있으면 전단지를 Evidence Source 로 첨부한다.
    """
    contents: list = [_build_mart_prompt(deals_json, day_context)]
    if image_bytes:
        img = image_part(image_bytes, "image/jpeg")
        if img is not None:
            contents.append(img)

    raw = call_gemini(
        contents,
        system_instruction=MART_SYSTEM_PROMPT,
        temperature=0.95,
        max_output_tokens=2048,
        response_mime_type="application/json",
    )
    if not raw:
        return None
    return parse_json_safely(raw)


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


def _fallback_crafted(market_name: str) -> dict:
    """분석 실패 시 쓰는 범용 광고 dict. scenes 가 없어 영상 생성은 건너뛴다."""
    return {
        "voiceover_script": _fallback_script(market_name),
        "scenes": [],
        "marketing_strategy": "fallback — 데이터 부족으로 범용 멘트 사용",
    }


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


def build_asset_package(
    market: str,
    deals: dict,
    crafted: dict,
    evidence_path: str,
    day_context: str = "",
) -> str:
    """
    assets/YYYYMMDD/ 아래에 CapCut 수동 조립용 재료 패키지를 생성한다:
        01_scene.png/.mp4 …  — 씬별 visual_prompt 로 만든 Replicate 이미지/영상
        prompts.json         — 씬(time/visual_prompt/effect) + 전략 + 모델
        voiceover_script.txt — 성우용 구어체 대본
        captions.srt         — CapCut 드래그용 자막 (가상 타임코드)
        voiceover.mp3        — ElevenLabs 보이스 (텐션 보이스)
        flyer_data.json      — 통합 메타 + 에셋 매니페스트
    각 외부 API 키/패키지가 없으면 해당 에셋만 건너뛰고 계속 진행한다.
    """
    out_dir = assets_dir_today()
    print(f"[Assets] 편집 재료 폴더: {out_dir}")
    asset_files: list[str] = []

    voiceover_script = (crafted.get("voiceover_script") or "").strip()
    scenes = crafted.get("scenes") or []
    strategy = (crafted.get("marketing_strategy") or "").strip()

    # 1) 씬별 이미지 → 영상 클립 (비용 제어: 상위 MAX_CLIPS 씬만 생성)
    clip_budget = min(len(scenes), MAX_CLIPS)
    for i, scene in enumerate(scenes, start=1):
        if i > MAX_CLIPS:
            break
        visual = (scene.get("visual_prompt") or "").strip() if isinstance(scene, dict) else ""
        if not visual:
            continue
        img_path = os.path.join(out_dir, f"{i:02d}_scene.png")
        clip_path = os.path.join(out_dir, f"{i:02d}_scene.mp4")

        print(f"[Assets] ({i}/{clip_budget}) 씬 이미지 생성...")
        made_img = generate_image_asset(visual, img_path)
        if made_img:
            asset_files.append(os.path.basename(img_path))

        print(f"[Assets] ({i}/{clip_budget}) 씬 영상 클립 생성...")
        made_clip = generate_video_clip(visual, clip_path, init_image_path=made_img)
        if made_clip:
            asset_files.append(os.path.basename(clip_path))

    # 2) prompts.json — 씬(time/visual_prompt/effect) + 전략 + 모델
    prompts_path = os.path.join(out_dir, "prompts.json")
    with open(prompts_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "image_model": REPLICATE_IMAGE_MODEL,
                "video_model": REPLICATE_VIDEO_MODEL,
                "marketing_strategy": strategy,
                "scenes": scenes,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    asset_files.append("prompts.json")

    # 3) voiceover_script.txt + captions.srt + voiceover.mp3
    vo_txt_path = os.path.join(out_dir, "voiceover_script.txt")
    with open(vo_txt_path, "w", encoding="utf-8") as f:
        f.write(voiceover_script)
    asset_files.append("voiceover_script.txt")

    srt_path = os.path.join(out_dir, "captions.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(build_srt(voiceover_script))
    asset_files.append("captions.srt")

    print("[Assets] 보이스 생성...")
    vo_path = os.path.join(out_dir, "voiceover.mp3")
    if generate_voiceover(voiceover_script, vo_path, ELEVENLABS_MART_VOICE_ID):
        asset_files.append("voiceover.mp3")

    # 4) 통합 메타 + 에셋 매니페스트 (flyer_data.json)
    flyer_data = build_render_request(market, deals, voiceover_script, evidence_path)
    flyer_data["day_context"] = day_context
    flyer_data["marketing_strategy"] = strategy
    flyer_data["scenes"] = scenes
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

        print("\n[4/5] 병맛 광고 대본 생성 중...")
        crafted = None
        try:
            crafted = craft_mart_script(
                deals, image_bytes=image_bytes, day_context=day_context
            )
        except Exception as e:
            print(f"[경고] 대본 생성 중 예외 발생: {e!r}")
        if not crafted or not (crafted.get("voiceover_script") or "").strip():
            print("[Fallback] 대본 생성 실패 → 범용 대본으로 우회.")
            crafted = _fallback_crafted(market)
    else:
        print("[Fallback] 가격 데이터 없음/이미지 분석 실패 → 범용 대본으로 우회.")
        deals = {"market_name": market, "top_deals": []}
        crafted = _fallback_crafted(market)

    print("\n=== 병맛 광고 대본 ===")
    print(crafted.get("voiceover_script", ""))
    if crafted.get("marketing_strategy"):
        print(f"\n[전략] {crafted['marketing_strategy']}")

    # 편집 재료 자동화 모드: assets/YYYYMMDD/ 에 편집용 재료 패키지 생성
    print("\n[5/5] 편집 재료 생성 (Replicate 이미지·영상 + ElevenLabs 보이스)...")
    out_dir = build_asset_package(market, deals, crafted, evidence_path, day_context)
    print(f"\n[완료] 편집 재료 패키지: {out_dir}/")
    print("        → 01_scene.mp4 / voiceover.mp3 / captions.srt 를 CapCut 에 드래그,")
    print("          prompts.json 의 scenes(effect 포함)·voiceover_script.txt 활용.")
