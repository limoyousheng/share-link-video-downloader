# Enable LAN sharing on port 8765: ensure firewall rule + start service bound to 0.0.0.0
# Pure ASCII to avoid GBK/UTF-8 parse issues on Windows PowerShell 5.1

$ErrorActionPreference = 'Stop'

# Project dir = parent of this script's parent folder (scripts/lan/ -> scripts/ -> project root)
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$LogsDir    = Join-Path $ProjectDir 'logs'
$OutLog     = Join-Path $LogsDir 'serve.out.log'
$ErrLog     = Join-Path $LogsDir 'serve.err.log'
$PythonExe  = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$Port       = 8765

if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir | Out-Null }

# 1) Firewall rule (all profiles, but only devices in the local subnet)
$fw = Get-NetFirewallRule -DisplayName 'Allow TCP 8765 (LAN only)' -ErrorAction SilentlyContinue
if (-not $fw) {
  New-NetFirewallRule -DisplayName 'Allow TCP 8765 (LAN only)' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
    -Profile Any -RemoteAddress LocalSubnet `
    -Description 'Allow local-subnet devices to reach uvicorn on 8765' | Out-Null
  Write-Output '[OK] Firewall rule added'
} else {
  $fw | Set-NetFirewallRule -Enabled True -Profile Any -Direction Inbound -Action Allow
  $fw | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet
  Write-Output '[OK] Firewall rule updated (LocalSubnet only)'
}

# 2) Kill any process holding 8765
Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object {
    try {
      Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
      Write-Output ("[OK] Killed PID {0} holding port {1}" -f $_.OwningProcess, $Port)
    } catch {}
  }
Start-Sleep -Milliseconds 800

# 3) Start in background
if (-not (Test-Path $PythonExe)) {
  Write-Error "Python not found: $PythonExe"
  exit 1
}

$env:DOUYIN_NO_BROWSER = '1'
$proc = Start-Process -FilePath $PythonExe `
    -ArgumentList @('-m','scripts.serve') `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError  $ErrLog `
    -PassThru
Write-Output ("[OK] Service started in background, PID={0}" -f $proc.Id)

# 4) Wait 3s and report listeners
Start-Sleep -Seconds 3
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
  $listen | Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize
  $candidates = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                 Where-Object { $_.IPAddress -notmatch '^169\.254\.' -and $_.IPAddress -ne '127.0.0.1' }
  # Prefer physical NIC (WLAN / Ethernet) over VMware virtual NICs
  $ip = $candidates |
        Where-Object { $_.InterfaceAlias -match 'WLAN|Ethernet|以太网|无线' -and $_.InterfaceAlias -notmatch 'VMware|VMnet|Hyper-V' } |
        Select-Object -First 1 -ExpandProperty IPAddress
  if (-not $ip) { $ip = $candidates | Select-Object -First 1 -ExpandProperty IPAddress }
  if (-not $ip) { $ip = '127.0.0.1' }
  Write-Output ("LAN URL: http://{0}:{1}" -f $ip, $Port)
} else {
  Write-Warning 'Service is not listening. Check the log file.'
}
Write-Output ("Log: {0}" -f $ErrLog)
