@echo off
title Gemini Live - Multimodal Screen & Vision Assistant
cd /d "%~dp0"
echo Starting Gemini Live Multimodal Assistant...
start http://localhost:8000
python server.py
pause
