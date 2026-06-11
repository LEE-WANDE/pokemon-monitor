@echo off
chcp 65001 >nul
echo.
echo ==========================================
echo   포켓몬 카드 재고 모니터 시작
echo ==========================================
echo.

if exist pokemon_monitor.lock (
    echo [오류] 이미 실행 중입니다.
    echo.
    echo  이미 실행 중인 프로그램이 있거나, 비정상 종료된 경우입니다.
    echo  해결 방법:
    echo    1. 작업 관리자에서 python.exe 를 찾아 종료
    echo    2. pokemon_monitor.lock 파일 삭제
    echo    3. 다시 시작.bat 실행
    echo.
    pause
    exit /b 1
)

echo  서버를 시작합니다...
start "포켓몬 재고 모니터" python main.py

echo  잠시 기다리는 중...
timeout /t 4 /nobreak >nul

echo  브라우저를 엽니다...
start http://localhost:8080

echo.
echo  모니터링이 시작되었습니다!
echo  이 창을 닫아도 백그라운드에서 계속 실행됩니다.
echo.
timeout /t 5 /nobreak >nul
