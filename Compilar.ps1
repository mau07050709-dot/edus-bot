param(
    [string]$Python = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    [string]$Distribution = (Join-Path $PSScriptRoot 'dist'),
    [switch]$DesktopShortcut
)

$ErrorActionPreference = 'Stop'
$Python = (Get-Command $Python -ErrorAction Stop).Source
$release = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$buildRoot = Join-Path ([IO.Path]::GetTempPath()) "EDUS-Bot-$release"
$icon = Join-Path $PSScriptRoot 'calendario.ico'
$alarm = Join-Path $PSScriptRoot 'diana.wav'
$releaseRoot = Join-Path $Distribution $release
New-Item -ItemType Directory -Path $buildRoot | Out-Null

Push-Location -LiteralPath $PSScriptRoot
try {
    & $Python -m PyInstaller --noconfirm --windowed --onedir --name 'EDUS Bot' `
        --icon $icon --add-data "$($icon):." --add-data "$($alarm):." --collect-data tzdata --hidden-import keyring.backends.Windows `
        --workpath (Join-Path $buildRoot 'work') --specpath $buildRoot `
        --distpath $releaseRoot (Join-Path $PSScriptRoot 'main.py')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller terminó con código $LASTEXITCODE." }
    $exe = (Resolve-Path -LiteralPath (Join-Path $releaseRoot 'EDUS Bot\EDUS Bot.exe')).Path
    # Qt para Windows usa ICU del sistema. Evita sombrearlo con el ICU
    # de otra aplicación que PyInstaller haya encontrado en PATH.
    $internal = Join-Path (Split-Path -Parent $exe) '_internal'
    Get-ChildItem -LiteralPath $internal -File | Where-Object {
        $_.Name -eq 'icuuc.dll' -or $_.Name -match '^icudt\d+\.dll$'
    } | ForEach-Object { Remove-Item -LiteralPath $_.FullName }
    if ($DesktopShortcut) {
        & (Join-Path $PSScriptRoot 'Crear-AccesoDirecto.ps1') -Executable $exe
    }
    Write-Output $exe
} finally {
    Pop-Location
    $resolvedBuild = (Resolve-Path -LiteralPath $buildRoot).Path
    $temporaryDirectory = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolvedBuild.StartsWith($temporaryDirectory, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolvedBuild) -eq "EDUS-Bot-$release") {
        Remove-Item -LiteralPath $resolvedBuild -Recurse -Force
    }
}
