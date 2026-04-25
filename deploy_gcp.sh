#!/bin/bash

# 변수 설정
PROJECT_ID="kkaetalk-batch"
REGION="us-west1"  # <-- 여기를 us-west1 로 변경!
IMAGE_NAME="kkaetalk-marketing-bot"

# 1. 아티팩트 레지스트리에 이미지 빌드 및 푸시
gcloud builds submit --tag gcr.io/$PROJECT_ID/$IMAGE_NAME

# 2. Cloud Run Job 생성 또는 업데이트
# (메모리 2Gi 이상 권장 - Pillow 이미지 처리 및 LLM 호출용)
gcloud run jobs deploy kkaetalk-daily-marketing \
    --image gcr.io/$PROJECT_ID/$IMAGE_NAME \
    --region $REGION \
    --memory 2Gi \
    --set-env-vars="AUTO_EMIT_JSON=1" \
    --max-retries 1

echo "✅ Cloud Run Job 배포 완료!"