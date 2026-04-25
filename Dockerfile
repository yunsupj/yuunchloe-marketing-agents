# 1. Base Image
FROM python:3.11-slim

# 2. 필수 패키지 설치 (Font 관련 처리를 위한 라이브러리 포함)
RUN apt-get update && apt-get install -y \
    fonts-noto-cjk \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 3. 작업 디렉토리 설정
WORKDIR /app

# 4. 의존성 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 소스 코드 복사
COPY . .

# 6. 출력 및 폰트 캐시 디렉토리 생성
RUN mkdir -p output fonts

# 7. 실행 명령 (기본적으로 스케줄러 실행)
# Cloud Run Jobs 환경에서는 환경 변수에 따라 다른 명령을 줄 수 있습니다.
CMD ["python", "auto_scheduler.py"]