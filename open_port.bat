@echo off
:: ============================================================
::  Weltrade Bot - Firewall Port Setup Script
::  Run this ONCE as Administrator to open your bot port.
::  Right-click this file and choose "Run as administrator"
:: ============================================================

:: Check for Administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo.
    echo  How to fix:
    echo  1. Right-click this file (open_port.bat)
    echo  2. Select "Run as administrator"
    echo  3. Click Yes on the permission dialog
    echo.
    pause
    exit /b 1
)

echo.
echo  =========================================
echo   Weltrade Bot - Firewall Setup
echo  =========================================
echo.

:: Read the port from .env if it exists, otherwise ask
set BOT_PORT=800

if exist ".env" (
    for /f "tokens=2 delims==" %%a in ('findstr /i "BOT_PORT" .env') do set BOT_PORT=%%a
    echo  Detected port from .env file: %BOT_PORT%
) else (
    set /p BOT_PORT= Enter the port number your bot runs on (default 800): 
    if "%BOT_PORT%"=="" set BOT_PORT=800
)

echo.
echo  Opening port %BOT_PORT% in Windows Firewall...
echo.

:: Remove any existing rules with the same name to avoid duplicates
netsh advfirewall firewall delete rule name="Weltrade Bot" >nul 2>&1

:: Add inbound rule (allows incoming connections from the internet)
netsh advfirewall firewall add rule ^
    name="Weltrade Bot" ^
    dir=in ^
    action=allow ^
    protocol=TCP ^
    localport=%BOT_PORT% ^
    description="Allows incoming connections to the Weltrade trading bot"

if %errorLevel% equ 0 (
    echo.
    echo  =========================================
    echo   SUCCESS - Port %BOT_PORT% is now open!
    echo  =========================================
    echo.
) else (
    echo.
    echo  ERROR: Could not open the port. Please try again as Administrator.
    echo.
    pause
    exit /b 1
)

:: Get the machine's public-facing IP to show the user their access URL
echo  Finding your VPS IP address...
echo.

:: Try to get external IP
for /f %%i in ('powershell -command "(Invoke-WebRequest -Uri 'https://api.ipify.org' -UseBasicParsing).Content" 2^>nul') do set PUBLIC_IP=%%i

if "%PUBLIC_IP%"=="" (
    :: Fallback to local IP if internet check fails
    for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
        set PUBLIC_IP=%%a
        goto :found_ip
    )
    :found_ip
    set PUBLIC_IP=%PUBLIC_IP: =%
)

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
echo  NOTE: This address is shown every time the
echo  bot starts. You do not need to run this
echo  script again unless you change the port.
echo.
pause