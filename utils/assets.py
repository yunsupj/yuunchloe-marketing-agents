"""
utils/assets.py — 3대장 공통 에셋 유틸.

날짜별 출력 폴더 생성, 파일 다운로드, SRT 자막 생성, ElevenLabs 보이스
생성처럼 마트/썰/트렌드 스크립트가 똑같이 쓰는 로직을 모은다.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

import requests

ASSETS_DIR = os.getenv("ASSETS_DIR", "assets")
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")


def assets_dir_today() -> str:
    """assets/YYYYMMDD/ 폴더를 만들고 경로를 반환한다."""
    path = os.path.join(ASSETS_DIR, datetime.now().strftime("%Y%m%d"))
    os.makedirs(path, exist_ok=True)
    return path


def download_to(url: str, out_path: str, timeout: int = 180) -> bool:
    """url 을 받아 out_path 에 저장한다. 성공 시 True."""
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
        return True
    except requests.RequestException as e:
        print(f"[Assets] 다운로드 실패 [{url}]: {e!r}")
        return False


def _srt_time(seconds: int) -> str:
    """초 → SRT 타임코드(HH:MM:SS,mmm)."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},000"


def build_srt(narration: str, seconds_per_line: int = 2) -> str:
    """
    나레이션을 문장 단위로 쪼개 씬당 seconds_per_line 초씩 배정한 SRT 생성.
    CapCut 에 드래그하면 바로 자막이 입혀진다 (가상 타임코드).
    """
    parts = re.split(r"(?<=[.!?。])\s+|\n+", narration.strip())
    lines = [p.strip() for p in parts if p.strip()]
    blocks = []
    for i, line in enumerate(lines):
        start = i * seconds_per_line
        end = start + seconds_per_line
        blocks.append(f"{i + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{line}")
    return "\n\n".join(blocks) + "\n"


def generate_voiceover(
    narration: str,
    out_path: str,
    voice_id: str,
    model_id: str | None = None,
) -> str | None:
    """
    ElevenLabs TTS 로 보이스 MP3 를 생성해 out_path 에 저장한다.
    voice_id 는 호출하는 쪽에서 결정한다 (마트=텐션 / 썰=친근 등).
    키/voice_id/패키지가 없으면 건너뛰고 None.
    """
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        print("[Assets] ELEVENLABS_API_KEY 미설정 — 보이스 생성 스킵.")
        return None
    if not voice_id:
        print("[Assets] voice_id 미지정 — 보이스 생성 스킵.")
        return None
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        print("[Assets] elevenlabs 미설치. `pip install elevenlabs`")
        return None

    try:
        client = ElevenLabs(api_key=api_key)
        audio = client.text_to_speech.convert(
            voice_id=voice_id,
            model_id=model_id or ELEVENLABS_MODEL_ID,
            text=narration,
            output_format="mp3_44100_128",
        )
        with open(out_path, "wb") as f:
            for chunk in audio:
                if chunk:
                    f.write(chunk)
        return out_path
    except Exception as e:
        print(f"[Assets] ElevenLabs 보이스 생성 실패: {e!r}")
        return None
