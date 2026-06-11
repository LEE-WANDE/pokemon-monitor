@echo off
chcp 65001 >nul
echo.
echo ==========================================
echo   포켓몬 카드 재고 모니터 - 설치
echo ==========================================
echo.

echo [1/2] Python 패키지 설치 중...
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [오류] 패키지 설치에 실패했습니다.
    echo       Python이 설치되어 있는지 확인하세요.
    pause
    exit /b 1
)

echo.
echo [2/2] Playwright 브라우저(Chromium) 설치 중...
playwright install chromium
if errorlevel 1 (
    echo.
    echo [오류] Playwright 브라우저 설치에 실패했습니다.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   설치 완료!
echo   시작.bat 을 더블클릭해서 실행하세요.
echo ==========================================
echo.
pause
