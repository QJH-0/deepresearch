@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONPATH=D:\Code\LLMdev\deepresearch\app
cd /d D:\Code\LLMdev\deepresearch\app
D:\develop_tools\miniconda3\envs\llmdev\python.exe -m uvicorn app_main:app --host 0.0.0.0 --port 8000
