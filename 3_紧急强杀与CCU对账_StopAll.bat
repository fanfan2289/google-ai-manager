@echo off
chcp 65001 >nul
title Colab 紧急强杀全部实例 + CCU 对账
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Invoke-ColabH3.ps1" stop %*
echo.
pause
