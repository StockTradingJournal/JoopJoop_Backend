@echo off
echo 🚀 ForSale 서버 환경 설정을 시작합니다...

REM 가상환경이 이미 있는지 확인
if exist "venv" (
    echo ⚠️  기존 가상환경이 발견되었습니다. 삭제하고 새로 생성합니다.
    rmdir /s /q venv
)

REM 가상환경 생성
echo 📦 가상환경을 생성합니다...
python -m venv venv

REM 가상환경 활성화
echo 🔧 가상환경을 활성화합니다...
call venv\Scripts\activate.bat

REM pip 업그레이드
echo ⬆️  pip을 업그레이드합니다...
python -m pip install --upgrade pip

REM 의존성 설치
echo 📚 의존성 패키지를 설치합니다...
pip install -r requirements.txt

echo ✅ 설정이 완료되었습니다!
echo.
echo 서버를 실행하려면 다음 명령어를 사용하세요:
echo   start.bat
echo.
echo 또는 수동으로 실행하려면:
echo   venv\Scripts\activate.bat
echo   uvicorn main:socket_app --reload --host 0.0.0.0 --port 8000

pause