@echo off
chcp 65001 >nul
title Colab CPU 零GPU成本下载 MiniMax-H3 模型到 5TB 云盘
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Invoke-GAM.ps1" download %*
echo.
pause
