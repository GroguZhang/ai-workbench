@echo off
chcp 65001 >nul
:: Disable Oray/ToDesk virtual DISPLAY adapters only (removes phantom 4K@59 screen causing multi-monitor stutter)
:: Uses pnputil (Disable-PnpDevice cmdlet is not implemented on this machine)
:: Must run as Administrator. Right-click -> "Run as administrator"
net session >nul 2>&1
if %errorLevel% neq 0 (
  echo [ERROR] Please right-click and choose "Run as administrator".
  pause
  exit /b 1
)
echo Disabling Oray / ToDesk virtual display adapters (gaming mode)...
powershell -NoProfile -Command "$devs = Get-PnpDevice -Class Display -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -match 'Oray|ToDesk' }; if($devs.Count -eq 0){ Write-Host 'No virtual display found.' } else { foreach($d in $devs){ Write-Host ('Disabling: ' + $d.FriendlyName + ' [' + $d.InstanceId + ']'); pnputil.exe /disable-device $d.InstanceId } }"
echo Done. Re-enable later with enable_virtual_displays.bat
pause
