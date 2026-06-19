param(
    [Parameter(Mandatory = $true)]
    [string]$CommandFile
)

$ErrorActionPreference = "Stop"

function Resolve-CommandPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable
    )

    $appData = [Environment]::GetFolderPath("ApplicationData")
    $appDataCmd = Join-Path $appData "npm\$Executable.cmd"
    if (Test-Path -LiteralPath $appDataCmd) {
        return $appDataCmd
    }

    $resolved = Get-Command $Executable -ErrorAction SilentlyContinue
    if ($resolved) {
        return $resolved.Source
    }

    foreach ($suffix in @(".cmd", ".exe")) {
        $resolved = Get-Command "$Executable$suffix" -ErrorAction SilentlyContinue
        if ($resolved) {
            return $resolved.Source
        }
    }

    return $Executable
}

$payload = Get-Content -LiteralPath $CommandFile -Raw | ConvertFrom-Json
$cmd = @($payload.cmd)
$cwd = [string]$payload.cwd

if ($cwd) {
    Set-Location -LiteralPath $cwd
}

if (-not $cmd -or $cmd.Count -eq 0) {
    throw "Saved command file does not contain a command."
}

$cmd[0] = Resolve-CommandPath -Executable ([string]$cmd[0])

& $cmd[0] $cmd[1..($cmd.Count - 1)]
if ($null -ne $LASTEXITCODE) {
    exit $LASTEXITCODE
}

