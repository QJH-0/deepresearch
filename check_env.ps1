Write-Host "=== Current Process Env ==="
Write-Host ("Process TEMP: {0}" -f $env:TEMP)
Write-Host ("Process TMP:  {0}" -f $env:TMP)
Write-Host ""
Write-Host "=== User Level Env ==="
Write-Host ("User TEMP: {0}" -f [Environment]::GetEnvironmentVariable('TEMP','User'))
Write-Host ("User TMP:  {0}" -f [Environment]::GetEnvironmentVariable('TMP','User'))
Write-Host ""
Write-Host "=== Machine Level Env ==="
Write-Host ("Machine TEMP: {0}" -f [Environment]::GetEnvironmentVariable('TEMP','Machine'))
Write-Host ("Machine TMP:  {0}" -f [Environment]::GetEnvironmentVariable('TMP','Machine'))
Write-Host ""
Write-Host "=== Registry User Env ==="
$reg = Get-ItemProperty 'HKCU:\Environment'
Write-Host ("Reg TEMP: {0}" -f $reg.TEMP)
Write-Host ("Reg TMP:  {0}" -f $reg.TMP)
Write-Host ""
Write-Host "=== WSL Status ==="
wsl --list --verbose
Write-Host ""
Write-Host "=== Docker Desktop Running? ==="
Get-Process -Name 'Docker Desktop','com.docker.backend' -ErrorAction SilentlyContinue | Select-Object Name,Id,StartTime | Format-Table -AutoSize
Write-Host ""
Write-Host "=== Search for wsl.cache everywhere ==="
$searchDirs = @(
    'C:\Users\20448\AppData\Local\Temp',
    'D:\WindowsTemp',
    'D:\cache\python-ml\temp',
    'C:\Windows\Temp',
    'C:\Windows\System32\Temp'
)
foreach ($d in $searchDirs) {
    if (Test-Path $d) {
        $found = Get-ChildItem $d -Filter '*wsl*' -Recurse -Force -ErrorAction SilentlyContinue -Depth 1
        if ($found) {
            foreach ($f in $found) {
                Write-Host ("FOUND: {0}" -f $f.FullName)
            }
        }
    }
}
Write-Host ""
Write-Host "=== Check Docker Desktop settings ==="
$settingsFile = 'C:\Users\20448\AppData\Roaming\Docker\settings-store.json'
if (Test-Path $settingsFile) {
    $content = Get-Content $settingsFile -Raw
    Write-Host $content
}
