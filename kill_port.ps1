$proc = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($proc) {
    Stop-Process -Id $proc.OwningProcess -Force
    Write-Host "Killed PID $($proc.OwningProcess)"
} else {
    Write-Host "No process on port 8000"
}
