@echo off
chcp 65001 >nul
title Colab G4 + MiniMax-H3 一键运行 (含自动关机省CCU)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Invoke-GAM.ps1" drama %*
echo.
pause
