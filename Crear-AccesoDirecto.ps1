param(
    [Parameter(Mandatory = $true)]
    [string]$Executable
)

$ErrorActionPreference = 'Stop'
$exe = (Resolve-Path -LiteralPath $Executable).Path
if ((Split-Path -Leaf $exe) -ne 'EDUS Bot.exe') { throw 'Selecciona EDUS Bot.exe.' }
$desktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
if (-not $desktop -or -not (Test-Path -LiteralPath $desktop -PathType Container)) {
    throw 'No se encontró el Escritorio de Windows.'
}
$linkPath = Join-Path $desktop 'EDUS Bot Public.lnk'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $exe
$link.WorkingDirectory = Split-Path -Parent $exe
$link.Description = 'Abrir EDUS Bot'
$link.IconLocation = "$exe,0"
$link.Save()
if ($shell.CreateShortcut($linkPath).TargetPath -ne $exe) {
    throw 'El acceso directo no apunta al ejecutable esperado.'
}
Write-Output $linkPath
