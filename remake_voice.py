"""
remake_voice.py — 수정된 대본 텍스트로 보이스만 다시 뽑아주는 유틸.
사용법: python remake_voice.py assets/20260521_story_1234...
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()
from utils.assets import generate_voiceover

def main():
    # 1. 터미널에서 폴더 경로를 입력받음
    if len(sys.argv) < 2:
        print("사용법: python remake_voice.py <에셋폴더경로>")
        return

    target_dir = sys.argv[1].rstrip("/")
    script_path = os.path.join(target_dir, "voiceover_script.txt")
    out_mp3_path = os.path.join(target_dir, "voiceover.mp3")

    # 2. 텍스트 파일 존재 확인
    if not os.path.exists(script_path):
        print(f"[오류] 대본 파일을 찾을 수 없습니다: {script_path}")
        return

    # 3. 텍스트 읽기
    with open(script_path, "r", encoding="utf-8") as f:
        narration = f.read().strip()

    if not narration:
        print("[오류] 대본 파일이 비어 있습니다.")
        return

    # 4. 공감/썰 보이스 ID 가져와서 덮어쓰기 (마트용으로 쓰려면 MART_VOICE_ID로 변경)
    voice_id = os.getenv("ELEVENLABS_STORY_VOICE_ID")
    print(f"🎙️ 다음 텍스트로 음성을 재추출합니다:\n{narration}\n")
    print("⏳ 생성 중...")

    result = generate_voiceover(narration, out_mp3_path, voice_id)

    if result:
        print(f"✅ 성공! 새 음성이 저장되었습니다: {out_mp3_path}")
    else:
        print("❌ 음성 생성 실패.")

if __name__ == "__main__":
    main()