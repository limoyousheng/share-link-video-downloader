# Disable LAN sharing on port 8765: kill the service and remove firewall rule
# Pure ASCII for Windows PowerShell 5.1 compatibility

$ErrorActionPreference = 'Stop'

$Port = 8765

# 1) Kill processes holding the port
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conns) {
  $conns | ForEach-Object {
    try {
      Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
      Write-Output ("[OK] Killed PID {0}" -f $_.OwningProcess)
    } catch {}
  }
} else {
  Write-Output '[INFO] Port 8765 is not in use'
}

# 2) Remove firewall rule
$fw = Get-NetFirewallRule -DisplayName 'Allow TCP 8765 (LAN only)' -ErrorAction SilentlyContinue
if ($fw) {
  Remove-NetFirewallRule -DisplayName 'Allow TCP 8765 (LAN only)' -ErrorAction SilentlyContinue
  Write-Output '[OK] Firewall rule removed'
} else {
  Write-Output '[INFO] No firewall rule to remove'
}

Write-Output '[Done] LAN sharing disabled'