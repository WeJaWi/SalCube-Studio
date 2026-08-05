@echo off
REM Double-click entry point. Windows blocks unsigned .ps1 scripts by
REM default, so run.ps1 is invoked with the policy bypassed for this
REM process only - nothing about the machine's policy is changed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
