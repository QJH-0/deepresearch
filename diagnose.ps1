# ============================================================
# 全面诊断：缓存位置、Docker 镜像、各种开发工具缓存
# ============================================================

Write-Host "========================================"
Write-Host "  C/D Drive Space"
Write-Host "========================================"
$c = Get-PSDrive C
$d = Get-PSDrive D
Write-Host ("C: Used={0:N2}GB  Free={1:N2}GB  Total={2:N2}GB" -f ($c.Used/1GB), ($c.Free/1GB), (($c.Used+$c.Free)/1GB))
Write-Host ("D: Used={0:N2}GB  Free={1:N2}GB  Total={2:N2}GB" -f ($d.Used/1GB), ($d.Free/1GB), (($d.Used+$d.Free)/1GB))

# ---- Docker vhdx files ----
Write-Host ""
Write-Host "========================================"
Write-Host "  Docker VHDX Files"
Write-Host "========================================"
$vhdxFiles = @(
    'D:\docker\DockerDesktopWSL\disk\docker_data.vhdx',
    'D:\docker\DockerDesktopWSL\main\ext4.vhdx'
)
foreach ($f in $vhdxFiles) {
    if (Test-Path $f) {
        $item = Get-Item $f
        Write-Host ("{0}" -f $f)
        Write-Host ("  Size: {0:N2} GB" -f ($item.Length/1GB))
        Write-Host ("  LastWrite: {0}" -f $item.LastWriteTime)
    }
}

# ---- Docker images & containers ----
Write-Host ""
Write-Host "========================================"
Write-Host "  Docker Images & Containers"
Write-Host "========================================"
Write-Host "Docker images:"
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}" 2>$null
Write-Host ""
Write-Host "Docker containers:"
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Size}}" 2>$null
Write-Host ""
Write-Host "Docker system disk usage:"
docker system df 2>$null

# ---- npm cache ----
Write-Host ""
Write-Host "========================================"
Write-Host "  npm cache"
Write-Host "========================================"
$npmCache = "$env:LOCALAPPDATA\npm-cache"
$npmCache2 = "$env:APPDATA\npm-cache"
$npmCache3 = "$env:USERPROFILE\.npm"
$npmPaths = @($npmCache, $npmCache2, $npmCache3) | Select-Object -Unique
foreach ($p in $npmPaths) {
    if (Test-Path $p) {
        $size = (Get-ChildItem $p -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
        Write-Host ("{0}  ->  {1:N2} MB" -f $p, ($size/1MB))
    }
}
# npm config
$npmConfig = npm config get cache 2>$null
Write-Host ("npm config cache: {0}" -f $npmConfig)

# ---- pip cache ----
Write-Host ""
Write-Host "========================================"
Write-Host "  pip cache"
Write-Host "========================================"
$pipCache = "$env:LOCALAPPDATA\pip\cache"
$pipCache2 = "$env:USERPROFILE\.cache\pip"
$pipPaths = @($pipCache, $pipCache2) | Select-Object -Unique
foreach ($p in $pipPaths) {
    if (Test-Path $p) {
        $size = (Get-ChildItem $p -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
        Write-Host ("{0}  ->  {1:N2} MB" -f $p, ($size/1MB))
    }
}
$pipCacheDir = pip cache dir 2>$null
Write-Host ("pip cache dir: {0}" -f $pipCacheDir)

# ---- Other dev caches ----
Write-Host ""
Write-Host "========================================"
Write-Host "  Other Development Caches"
Write-Host "========================================"

$caches = @(
    @{ Name = "NuGet"; Path = "$env:USERPROFILE\.nuget\packages" },
    @{ Name = "Gradle"; Path = "$env:USERPROFILE\.gradle" },
    @{ Name = "Maven"; Path = "$env:USERPROFILE\.m2" },
    @{ Name = "Cargo (Rust)"; Path = "$env:USERPROFILE\.cargo" },
    @{ Name = "Go cache"; Path = "$env:LOCALAPPDATA\go-build" },
    @{ Name = "Go modules"; Path = "$env:USERPROFILE\go\pkg\mod" },
    @{ Name = "Yarn cache"; Path = "$env:LOCALAPPDATA\Yarn\Cache" },
    @{ Name = "pnpm store"; Path = "$env:LOCALAPPDATA\pnpm\store" },
    @{ Name = "pnpm store v3"; Path = "$env:USERPROFILE\.pnpm-store" },
    @{ Name = "Composer (PHP)"; Path = "$env:USERPROFILE\.composer\cache" },
    @{ Name = "Hugging Face cache"; Path = "$env:USERPROFILE\.cache\huggingface" },
    @{ Name = "Torch home"; Path = "$env:USERPROFILE\.cache\torch" },
    @{ Name = "Jupyter data"; Path = "$env:USERPROFILE\.jupyter" },
    @{ Name = "Conda pkgs"; Path = "$env:USERPROFILE\.conda\pkgs" },
    @{ Name = "Conda envs"; Path = "$env:USERPROFILE\.conda\envs" },
    @{ Name = "VS Code extensions"; Path = "$env:USERPROFILE\.vscode\extensions" },
    @{ Name = "Cursor extensions"; Path = "$env:USERPROFILE\.cursor\extensions" },
    @{ Name = "Claude extensions"; Path = "$env:USERPROFILE\.claude" },
    @{ Name = "WinGet cache"; Path = "$env:LOCALAPPDATA\Microsoft\WinGet\Cache" },
    @{ Name = "Electron cache"; Path = "$env:LOCALAPPDATA\electron\Cache" },
    @{ Name = "MS Playwright"; Path = "$env:USERPROFILE\AppData\Local\ms-playwright" },
    @{ Name = "Playwright"; Path = "$env:LOCALAPPDATA\ms-playwright" },
    @{ Name = "Docker Desktop logs"; Path = "$env:LOCALAPPDATA\Docker\log" }
)

foreach ($c in $caches) {
    if (Test-Path $c.Path) {
        $size = (Get-ChildItem $c.Path -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
        if ($size -gt 0) {
            Write-Host ("{0,-25} {1,10:N2} MB  {2}" -f $c.Name, ($size/1MB), $c.Path)
        }
    }
}

# ---- C盘最大文件夹 ----
Write-Host ""
Write-Host "========================================"
Write-Host "  Top 20 Largest Folders in C:\Users\20448"
Write-Host "========================================"
$userHome = 'C:\Users\20448'
Get-ChildItem $userHome -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $size = (Get-ChildItem $_.FullName -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    [PSCustomObject]@{ Folder = $_.Name; SizeMB = [math]::Round($size/1MB,2) }
} | Sort-Object SizeMB -Descending | Select-Object -First 20 | ForEach-Object {
    Write-Host ("{0,-40} {1,10:N2} MB" -f $_.Folder, $_.SizeMB)
}

# ---- AppData\Local top folders ----
Write-Host ""
Write-Host "========================================"
Write-Host "  Top 15 Largest in C:\Users\20448\AppData\Local"
Write-Host "========================================"
$appLocal = 'C:\Users\20448\AppData\Local'
Get-ChildItem $appLocal -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $size = (Get-ChildItem $_.FullName -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    [PSCustomObject]@{ Folder = $_.Name; SizeMB = [math]::Round($size/1MB,2) }
} | Sort-Object SizeMB -Descending | Select-Object -First 15 | ForEach-Object {
    Write-Host ("{0,-40} {1,10:N2} MB" -f $_.Folder, $_.SizeMB)
}
