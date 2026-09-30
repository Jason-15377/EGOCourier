@echo off
REM EGO Courier 命令行工具
cd /d "%~dp0"
python -m ego_relay.cli %*
