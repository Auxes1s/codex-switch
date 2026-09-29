[CmdletBinding()]
param(
    [switch] $NoPathUpdate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-NormalizedPath {
    param([string] $Path)

    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($fullPath, $root)) {
        $fullPath = $fullPath.TrimEnd([char[]] @('\', '/'))
    }
    return $fullPath
}

try {
    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_SWITCH_INSTALL_DIR)) {
        $installDirectory = $env:CODEX_SWITCH_INSTALL_DIR
    }
    else {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw 'LOCALAPPDATA must be set to an absolute path'
        }
        $installDirectory = Join-Path $env:LOCALAPPDATA 'Programs\codex-switch\bin'
    }

    if (-not [IO.Path]::IsPathRooted($installDirectory)) {
        throw 'the install directory must be an absolute path'
    }
    $installDirectory = Get-NormalizedPath $installDirectory
    [void] [IO.Directory]::CreateDirectory($installDirectory)

    $fileNames = @('codex-switch-migrate.py', 'codex-switch.ps1', 'codex-switch.cmd')
    foreach ($fileName in $fileNames) {
        if (-not [IO.File]::Exists((Join-Path (Join-Path $PSScriptRoot 'bin') $fileName))) {
            throw "missing installation source: $fileName"
        }
    }
    foreach ($fileName in $fileNames) {
        $source = Join-Path (Join-Path $PSScriptRoot 'bin') $fileName
        $destination = Join-Path $installDirectory $fileName
        $temporary = Join-Path $installDirectory ('.{0}.install.{1}.tmp' -f $fileName, [Guid]::NewGuid().ToString('N'))
        $backup = Join-Path $installDirectory ('.{0}.install-backup.{1}.tmp' -f $fileName, [Guid]::NewGuid().ToString('N'))
        try {
            [IO.File]::Copy($source, $temporary, $false)
            if ([IO.File]::Exists($destination)) {
                [IO.File]::Replace($temporary, $destination, $backup)
            }
            else {
                [IO.File]::Move($temporary, $destination)
            }
        }
        finally {
            if ([IO.File]::Exists($temporary)) {
                [IO.File]::Delete($temporary)
            }
            if ([IO.File]::Exists($backup)) {
                [IO.File]::Delete($backup)
            }
        }
    }

    $pathChanged = $false
    if (-not $NoPathUpdate) {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $normalizedInstallDirectory = Get-NormalizedPath $installDirectory
        $pathEntries = @($userPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $alreadyPresent = $false
        foreach ($entry in $pathEntries) {
            try {
                if ([StringComparer]::OrdinalIgnoreCase.Equals((Get-NormalizedPath $entry), $normalizedInstallDirectory)) {
                    $alreadyPresent = $true
                    break
                }
            }
            catch {
                # Preserve malformed or expandable existing PATH entries unchanged.
            }
        }
        if (-not $alreadyPresent) {
            $updatedUserPath = (($pathEntries + $installDirectory) -join ';')
            [Environment]::SetEnvironmentVariable('Path', $updatedUserPath, 'User')
            $pathChanged = $true
        }
    }

    [Console]::Out.WriteLine('Installed codex-switch to {0}', $installDirectory)
    if ($pathChanged) {
        [Console]::Out.WriteLine('Added the install directory to your user PATH. Open a new terminal before running codex-switch.')
    }
    elseif ($NoPathUpdate) {
        [Console]::Out.WriteLine('PATH was not changed. Add the install directory to PATH to run codex-switch by name.')
    }
}
catch {
    [Console]::Error.WriteLine('install.ps1: {0}', $_.Exception.Message)
    exit 1
}
