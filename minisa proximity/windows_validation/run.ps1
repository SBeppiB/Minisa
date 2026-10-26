param(
    [ValidateSet('Discover','FlashScanner','FlashBeacon','Validate','Verify','Tests')]
    [string]$Action = 'Discover',
    [string]$Scanner = '',
    [string]$Beacon = '',
    [string]$Python = '',
    [int]$SoakSeconds = 600
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $Python) { $Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) { throw 'Run setup.ps1 first, or supply -Python.' }
switch ($Action) {
    'Discover' { & $Python validate_hardware.py --discover }
    'Verify' { & $Python verify_package.py }
    'Tests' { & $Python -m unittest discover -v }
    'FlashScanner' {
        if (-not $Scanner) { throw 'Specify the positively identified -Scanner COMx. No port is guessed.' }
        & $Python flash_firmware.py --role scanner --port $Scanner
    }
    'FlashBeacon' {
        if (-not $Beacon) { throw 'Specify the positively identified -Beacon COMy. No port is guessed.' }
        & $Python flash_firmware.py --role beacon --port $Beacon
    }
    'Validate' {
        if (-not $Scanner) { throw 'Specify -Scanner COMx.' }
        $validationArgs = @('validate_hardware.py', '--scanner', $Scanner, '--soak-seconds', "$SoakSeconds")
        if ($Beacon) { $validationArgs += @('--beacon', $Beacon) }
        & $Python @validationArgs
    }
}
exit $LASTEXITCODE
