@echo off
chcp 65001 >nul
:: Re-enable Oray/ToDesk virtual DISPLAY adapters (use when you need remote desktop again)
:: Uses pnputil. Must run as Administrator. Right-click -> "Run as administrator"
net session >nul 2>&1
if %errorLevel% neq 0 (
  echo [ERROR] Please right-click and choose "Run as administrator".
  pause
  exit /b 1
)
echo Re-enabling Oray / ToDesk virtual display adapters...
powershell -NoProfile -Command "$devs = Get-PnpDevice -Class Display -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -match 'Oray|ToDesk' }; if($devs.Count -eq 0){ Write-Host 'No virtual display found.' } else { foreach($d in $devs){ Write-Host ('Enabling: ' + $d.FriendlyName + ' [' + $d.InstanceId + ']'); pnputil.exe /enable-device $d.InstanceId } }"
echo Done.
pause
