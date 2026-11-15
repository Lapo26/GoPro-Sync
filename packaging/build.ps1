<#
  Compila GoPro Sync para Windows:
    1. PyInstaller  -> dist\GoProSync\GoProSync.exe (carpeta, arranque rapido)
    2. ZIP portable -> dist\GoProSync-<version>-portable.zip
    3. Inno Setup   -> dist\GoProSync-Setup-<version>.exe  (si ISCC esta instalado)

  Uso (desde la raiz del repo):
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipInstaller
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Python C:\ruta\python.exe

  La version se lee de APP_VERSION en app\gopro_sync\version.py (la fija release.ps1).
#>
param(
    [string]$Python = "python",
    [switch]$SkipInstaller,
    [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$versionPy = Join-Path $Root "app\gopro_sync\version.py"
$m = Select-String -Path $versionPy -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $m) { throw "No se encontro APP_VERSION en app\gopro_sync\version.py" }
$Version = $m.Matches[0].Groups[1].Value
Write-Host "== GoPro Sync $Version ==" -ForegroundColor Cyan

if (-not $SkipDeps) {
    Write-Host "-> Instalando dependencias de build"
    & $Python -m pip install --disable-pip-version-check -q --upgrade -r app\requirements.txt -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw "pip install fallo" }
}

Write-Host "-> PyInstaller"
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
& $Python -m PyInstaller --noconfirm --clean --windowed `
    --name GoProSync `
    --icon "app\assets\app_icon.ico" `
    --add-data "app\assets;assets" `
    --collect-data imageio_ffmpeg `
    --exclude-module tkinter `
    --exclude-module unittest `
    --exclude-module pydoc `
    --exclude-module cv2 `
    --exclude-module numpy `
    --distpath dist --workpath build `
    app\main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fallo" }
if (-not (Test-Path "dist\GoProSync\GoProSync.exe")) { throw "No se genero dist\GoProSync\GoProSync.exe" }

Write-Host "-> Quitando partes de Qt que la app no usa"
# PyInstaller junta todos los plugins de Qt. El de teclado virtual arrastra
# Qt Quick/Qml/OpenGL y el de PDF arrastra Qt Pdf; la app no usa ninguno.
# opengl32sw.dll es OpenGL por software (la app dibuja con widgets / D3D).
$qt = "dist\GoProSync\_internal\PySide6"
$unused = @(
    "opengl32sw.dll", "Qt6OpenGL.dll", "Qt6Pdf.dll", "Qt6VirtualKeyboard.dll",
    "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlMeta.dll", "Qt6QmlModels.dll", "Qt6QmlWorkerScript.dll",
    "plugins\platforminputcontexts", "plugins\imageformats\qpdf.dll",
    "plugins\platforms\qdirect2d.dll", "plugins\platforms\qminimal.dll"
)
$freed = 0
foreach ($rel in $unused) {
    $p = Join-Path $qt $rel
    if (Test-Path $p) {
        $freed += (Get-ChildItem $p -Recurse -File | Measure-Object Length -Sum).Sum
        Remove-Item -Recurse -Force $p
    }
}
# Traducciones de Qt: solo los idiomas de la app (es, en, de), sin las de Qt Help
Get-ChildItem (Join-Path $qt "translations") -Filter *.qm | Where-Object {
    $_.Name -like "qt_help_*" -or $_.BaseName -notmatch '_(es|en|de)$'
} | ForEach-Object { $freed += $_.Length; Remove-Item -Force $_.FullName }
Write-Host ("   {0:N1} MB menos" -f ($freed / 1MB))

Write-Host "-> ZIP portable"
$zip = "dist\GoProSync-$Version-portable.zip"
Compress-Archive -Path "dist\GoProSync\*" -DestinationPath $zip -Force

if (-not $SkipInstaller) {
    $iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                         "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
            if (Test-Path $c) { $iscc = $c; break }
        }
    }
    if (-not $iscc) {
        Write-Warning "Inno Setup (ISCC.exe) no esta instalado: se omite el instalador. Instalalo con: winget install JRSoftware.InnoSetup"
    } else {
        Write-Host "-> Inno Setup"
        & $iscc /Qp "/DAppVersion=$Version" "/DSourceDir=$Root\dist\GoProSync" "/O$Root\dist" "packaging\GoProSync.iss"
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup fallo" }
    }
}

Write-Host ""
Write-Host "Listo. Archivos en dist\:" -ForegroundColor Green
Get-ChildItem dist -File | ForEach-Object { "  {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB) }
