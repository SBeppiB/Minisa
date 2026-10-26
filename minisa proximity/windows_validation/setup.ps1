param([string]$Python = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if ($Python) {
    $candidates = @($Python)
} else {
    $candidates = @('C:\Espressif\tools\python\python.exe', 'python', 'py')
}
$bootstrap = $null
foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
    & $candidate -c "import sys,sqlite3,venv; sys.exit(0 if sys.version_info >= (3,10) else 1)"
    if ($LASTEXITCODE -eq 0) { $bootstrap = $candidate; break }
}
if (-not $bootstrap) { throw 'Select Python 3.10+ with sqlite3 and venv using -Python path\to\python.exe' }

& $bootstrap verify_package.py
if ($LASTEXITCODE -ne 0) { throw 'Package integrity verification failed' }
$venvPath = Join-Path $PSScriptRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $bootstrap -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
}
& $venvPython -m pip --disable-pip-version-check install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; check pip output/network access' }
& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
& $venvPython -m esptool version
if ($LASTEXITCODE -ne 0) { throw 'esptool is not ready' }
Write-Host 'Setup complete. No ports were opened and no boards were flashed.'
Write-Host 'Next: .\run.ps1 -Action Discover'
