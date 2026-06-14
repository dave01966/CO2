@echo off
title CO2 Mero - Webszerver (HTTPS - telefon OK)
echo.
echo  ===================================================
echo   CO2 Mero webszerver – HTTPS alapbol (telefon OK)
echo  ===================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [HIBA] Python nem talalhato!
    echo Telepitsd: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/3] Fuggosegek ellenorzese...
pip install -r requirements.txt --quiet

echo [2/3] Halozat ellenorzese...

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
    set IP=%%a
    goto :found
)
:found
set IP=%IP: =%

echo [3/3] Szerver inditasa (HTTPS mod)...
echo.
echo  Helyi eleres:    https://localhost:5000
echo  LAN eleres:      https://%IP%:5000
echo.
echo  FONTOS: A bongeszo biztonsagi figyelmeztetest mutat!
echo    Chrome: 'Speciális' -^> 'Továbblépek a co2app oldalra'
echo    Telefon: ugyanigy, egyszer kell elfogadni
echo.

set MODE=lan
set HTTP_FLAG=

for %%a in (%*) do (
    if "%%a"=="--ngrok"      set MODE=ngrok
    if "%%a"=="--cloudflare" set MODE=cloudflare
    if "%%a"=="--online"     set MODE=auto
    if "%%a"=="--http"       set HTTP_FLAG=--http
)

if "%MODE%"=="lan" (
    echo  Leallitas: Ctrl+C
    echo.
    python app.py --host 0.0.0.0 --port 5000 %HTTP_FLAG%
) else if "%MODE%"=="ngrok" (
    echo  ngrok online URL: kovetkezo sorban jelenik meg...
    echo.
    python app.py --host 0.0.0.0 --port 5000 %HTTP_FLAG% --online ngrok
) else if "%MODE%"=="cloudflare" (
    echo  Cloudflare URL: kovetkezo sorban jelenik meg...
    echo.
    python app.py --host 0.0.0.0 --port 5000 %HTTP_FLAG% --online cloudflare
) else (
    echo  Online URL: kovetkezo sorban jelenik meg...
    echo.
    python app.py --host 0.0.0.0 --port 5000 %HTTP_FLAG% --online auto
)

pause
