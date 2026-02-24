#!/bin/bash

echo "🎮 ForSale 서버를 시작합니다..."

# 가상환경이 있는지 확인
if [ ! -d "venv" ]; then
    echo "❌ 가상환경이 없습니다. 먼저 setup.sh를 실행해주세요."
    echo "   ./setup.sh"
    exit 1
fi

# 가상환경 활성화
echo "🔧 가상환경을 활성화합니다..."
source venv/bin/activate

# 서버 실행
echo "🚀 서버를 실행합니다..."
echo "   서버 주소: http://localhost:8000"
echo "   API 문서: http://localhost:8000/docs"
echo ""
echo "서버를 중지하려면 Ctrl+C를 누르세요."
echo ""

uvicorn main:socket_app --reload --host 0.0.0.0 --port 8000