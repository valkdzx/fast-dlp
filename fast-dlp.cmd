@echo off
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
py -3 -m fast_dlp %*
