@echo off
:: Set working directory to the folder containing this script
cd /d "%~dp0"

:: Check for Administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo  =========================================
echo   Weltrade Bot - Firewall Setup
echo  =========================================
echo.

:: Read the port from .env if it exists, otherwise default to 800
set BOT_PORT=800

if exist ".env" (
    for /f "tokens=2 delims==" %%a in ('findstr /i "BOT_PORT" .env') do set BOT_PORT=%%a
)

:: Ensure BOT_PORT has a value
if "%BOT_PORT%"=="" set BOT_PORT=800
echo  Using port: %BOT_PORT%

echo.
echo  Opening port %BOT_PORT% in Windows Firewall...
echo.

:: Remove any existing rules with the same name to avoid duplicates
netsh advfirewall firewall delete rule name="Weltrade Bot" >nul 2>&1

:: Add inbound rule (allows incoming connections)
netsh advfirewall firewall add rule ^
    name="Weltrade Bot" ^
    dir=in ^
    action=allow ^
    protocol=TCP ^
    localport=%BOT_PORT% ^
    description="Allows incoming connections to the Weltrade trading bot"

if %errorLevel% equ 0 (
    echo.
    echo  SUCCESS - Port %BOT_PORT% is now open!
    echo.
) else (
    echo.
    echo  ERROR: Could not open the port.
    echo.
    pause
    exit /b 1
)

:: Get the machine's public-facing IP to show the user their access URL
echo  Finding your IP address...
echo.

set PUBLIC_IP=
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Invoke-RestMethod -Uri 'https://api.ipify.org').Trim()" 2^>nul`) do set PUBLIC_IP=%%i

if "%PUBLIC_IP%"=="" (
    :: Fallback to local IP if internet check fails
    for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
        set PUBLIC_IP=%%a
        goto :found_ip
    )
)
:found_ip

:: Remove spaces if defined
if defined PUBLIC_IP set PUBLIC_IP=%PUBLIC_IP: =%

echo  =========================================
echo.
echo   Your bot will be accessible at:
echo.
echo   http://%PUBLIC_IP%:%BOT_PORT%
echo.
echo   Save this address - this is what you and
echo   your users will type into their browser.
echo.
echo  =========================================
echo.
pause