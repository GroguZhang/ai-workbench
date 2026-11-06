@echo off
chcp 65001 >nul
:: Must run as Administrator. Right-click this file -> "Run as administrator"
net session >nul 2>&1
if %errorLevel% neq 0 (
  echo [ERROR] Please right-click this file and choose "Run as administrator".
  pause
  exit /b 1
)

echo Reverting optimizations...
echo.

:: HAGS: delete key -> back to system default (disabled)
reg delete "HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers" /v HwSchMode /f >nul 2>&1
echo [1/4] HAGS reverted to default

:: MPO: delete key -> re-enable Multiplane Overlay
reg delete "HKLM\SOFTWARE\Microsoft\Windows\Dwm" /v OverlayTestMode /f >nul 2>&1
echo [2/4] MPO reverted (re-enabled)

:: SystemResponsiveness: delete -> back to default (20)
reg delete "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile" /v SystemResponsiveness /f >nul 2>&1
echo [3/4] SystemResponsiveness reverted to default

:: Games task: delete whole key -> back to default
reg delete "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games" /f >nul 2>&1
echo [4/4] Games task reverted to default

:: Power plan -> back to High Performance
powercfg -setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c >nul 2>&1
echo [5/5] Power plan set back to High Performance

echo.
echo DONE. Reboot required for HAGS and MPO to fully revert.
pause
