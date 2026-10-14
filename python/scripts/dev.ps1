<#
.SYNOPSIS
    Set up, test, lint and build the dbx-aws-secrets wheel, with an optional package index override.

.DESCRIPTION
    By default the script uses public PyPI instead of the machine's configured pip index
    (e.g. a corporate Nexus that needs credentials). The override only applies to:
      * this script's process ($env:PIP_INDEX_URL), and
      * the project's .venv (.venv\pip.ini), so later manual `pip install` in the venv works too.
    Your user/global pip configuration is never modified.

.EXAMPLE
    .\scripts\dev.ps1                 # setup + lint + test + build
    .\scripts\dev.ps1 test
    .\scripts\dev.ps1 build -IndexUrl https://nexus.example.com/repository/pypi/simple/
#>
[CmdletBinding()]
param(
    [ValidateSet("all", "setup", "lint", "test", "build", "clean")]
    [string]$Task = "all",
    [string]$IndexUrl = "https://pypi.org/simple"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $ProjectDir ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"

# Process-scoped: also reaches the temporary build environments that `python -m build` creates.
$env:PIP_INDEX_URL = $IndexUrl
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

function Invoke-Setup {
    if (-not (Test-Path $Python)) {
        Write-Host "Creating virtual environment in $VenvDir"
        Invoke-Checked "python" @("-m", "venv", $VenvDir)
    }
    # Site-level pip config: overrides the user/global index for this venv only.
    Set-Content -Path (Join-Path $VenvDir "pip.ini") -Value "[global]`nindex-url = $IndexUrl`n"
    Write-Host "Installing dev dependencies from $IndexUrl"
    Invoke-Checked $Python @("-m", "pip", "install", "--quiet", "-e", "$ProjectDir[dev]")
}

function Invoke-Lint {
    Invoke-Checked $Python @("-m", "ruff", "check", $ProjectDir)
    Invoke-Checked $Python @("-m", "ruff", "format", "--check", $ProjectDir)
}

function Invoke-Test {
    Invoke-Checked $Python @("-m", "pytest", "-q", (Join-Path $ProjectDir "tests"))
}

function Invoke-Build {
    $dist = Join-Path $ProjectDir "dist"
    if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
    Invoke-Checked $Python @("-m", "build", "--wheel", "--outdir", $dist, $ProjectDir)
    Get-ChildItem $dist -Filter *.whl | ForEach-Object { Write-Host "Built $($_.FullName)" }
}

function Invoke-Clean {
    foreach ($d in @("dist", "build", ".pytest_cache", ".ruff_cache")) {
        $p = Join-Path $ProjectDir $d
        if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    }
}

Push-Location $ProjectDir
try {
    switch ($Task) {
        "setup" { Invoke-Setup }
        "lint"  { Invoke-Lint }
        "test"  { Invoke-Test }
        "build" { Invoke-Build }
        "clean" { Invoke-Clean }
        "all"   { Invoke-Setup; Invoke-Lint; Invoke-Test; Invoke-Build }
    }
}
finally {
    Pop-Location
}
