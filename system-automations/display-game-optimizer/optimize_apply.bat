@echo off
chcp 65001 >nul
:: Must run as Administrator. Right-click this file -> "Run as administrator"
net session >nul 2>&1
if %errorLevel% neq 0 (
  echo [ERROR] Please right-click this file and choose "Run as administrator".
  pause
  exit /b 1
)

echo Applying optimizations for Delta Force / dual-monitor...
echo.

:: 1) Enable Hardware-accelerated GPU Scheduling (HAGS) - fixes dual-monitor video stutter
reg add "HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers" /v HwSchMode /t REG_DWORD /d 2 /f
echo [1/5] HAGS enabled (HwSchMode=2)

:: 2) Disable Multiplane Overlay (MPO) - classic NVIDIA multi-monitor stutter/flicker fix
reg add "HKLM\SOFTWARE\Microsoft\Windows\Dwm" /v OverlayTestMode /t REG_DWORD /d 5 /f
echo [2/5] MPO disabled (OverlayTestMode=5)

:: 3) Multimedia system responsiveness -> 0 (foreground game/video priority)
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile" /v SystemResponsiveness /t REG_DWORD /d 0 /f
echo [3/5] SystemResponsiveness=0

:: 4) Games task high priority
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games" /v GPU /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games" /v Priority /t REG_DWORD /d 6 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games" /v SchedulingCategory /t REG_SZ /d High /f
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games" /v SFIOPriority /t REG_SZ /d High /f
echo [4/5] Games task -> GPU=0 Priority=6 High

:: 5) Power plan -> Ultimate Performance
powercfg -duplicatescheme e9a42b02-d5df-448d-aa00-03f14749eb61 >nul 2>&1
for /f "tokens=4" %%i in ('powercfg -list ^| findstr /C:"卓越性能"') do powercfg -setactive %%i
echo [5/5] Power plan set to Ultimate Performance

echo.
echo DONE. Reboot required for HAGS and MPO to take effect.
pause
