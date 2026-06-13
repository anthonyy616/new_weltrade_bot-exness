@echo off
:: ============================================================
::  Weltrade Bot - Environment Setup Script
::  Run this once on your VPS to configure your credentials.
::  Double-click the file or run it from Command Prompt.
:: ============================================================

echo.
echo  =========================================
echo   Weltrade Bot - Credential Setup
echo  =========================================
echo.
echo  This script will create your .env file with
echo  your broker and Supabase credentials.
echo  Your input will not be shown on screen.
echo.

:: Check if we are in the right folder
if not exist "main.py" (
    echo  ERROR: Please run this script from inside the bot folder.
    echo  Example: cd C:\weltrade-bot  then run setup_env.bat
    pause
    exit /b 1
)

echo  --- MetaTrader 5 Login ---
echo.
set /p MT5_LOGIN= Enter your MT5 account number (digits only): 
set /p MT5_PASSWORD= Enter your MT5 password: 
set /p MT5_SERVER= Enter your MT5 server name (e.g. Weltrade-Live): 

echo.
echo  --- MetaTrader 5 Terminal Path ---
echo  This is the full path to your terminal64.exe file.
echo  Default is usually: C:\Program Files\Weltrade MT5\terminal64.exe
echo.
set /p MT5_PATH= Enter full path to terminal64.exe (or press Enter for default): 

if "%MT5_PATH%"=="" (
    set MT5_PATH=C:\Program Files\Weltrade MT5\terminal64.exe
)

echo.
echo  --- Supabase Credentials ---
echo  These are provided to you by your bot administrator.
echo.
set /p SUPABASE_URL= Enter your Supabase URL: 
set /p SUPABASE_KEY= Enter your Supabase Key: 

echo.
echo  --- Bot Network Settings ---
set /p BOT_PORT= Enter the port number you want the bot to run on (default: 800): 

if "%BOT_PORT%"=="" (
    set BOT_PORT=800
)

set BOT_HOST=0.0.0.0

:: Write the .env file
(
echo MT5_LOGIN=%MT5_LOGIN%
echo MT5_PASSWORD=%MT5_PASSWORD%
echo MT5_SERVER=%MT5_SERVER%
echo MT5_PATH=%MT5_PATH%
echo SUPABASE_URL=%SUPABASE_URL%
echo SUPABASE_KEY=%SUPABASE_KEY%
echo BOT_HOST=%BOT_HOST%
echo BOT_PORT=%BOT_PORT%
) > .env

echo.
echo  =========================================
echo   SUCCESS - .env file created!
echo  =========================================
echo.
echo  Your bot will be accessible at:
echo  http://YOUR-VPS-IP:%BOT_PORT%
echo.
echo  To find your VPS IP address, open a new Command Prompt
echo  and type:  ipconfig
echo  Look for the line that says "IPv4 Address"
echo.
pause