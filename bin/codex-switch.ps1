[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $CommandArguments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:CopyTemp = $null
$script:MarkerTemp = $null

function Write-Usage {
    param([switch] $ToError)

    $text = @'
Usage: codex-switch <profile>
       codex-switch switch <profile>
       codex-switch add <profile>
       codex-switch auth <profile>
       codex-switch remove <profile>
       codex-switch list
       codex-switch --help

Manage saved Codex authentication profiles. The one-argument form is shorthand
for "codex-switch switch <profile>".
'@
    if ($ToError) {
        [Console]::Error.WriteLine($text)
    }
    else {
        [Console]::Out.WriteLine($text)
    }
}

function Stop-WithError {
    param(
        [Parameter(Mandatory = $true)][string] $Message,
        [int] $ExitCode = 1
    )

    [Console]::Error.WriteLine('codex-switch: {0}', $Message)
    exit $ExitCode
}

function Test-ValidProfileName {
    param([AllowNull()][AllowEmptyString()][string] $Name)

    if ([string]::IsNullOrEmpty($Name) -or $Name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
        return $false
    }

    return @('add', 'auth', 'help', 'list', 'remove', 'switch') -notcontains $Name
}

function Assert-ProfileName {
    param([string] $Name)

    if (-not (Test-ValidProfileName $Name)) {
        Stop-WithError "invalid profile name: $Name" 64
    }
}

function Test-UsableAuth {
    param([string] $Path)

    try {
        if (-not [IO.File]::Exists($Path) -or ([IO.FileInfo] $Path).Length -eq 0) {
            return $false
        }
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $stream.Dispose()
        return $true
    }
    catch {
        return $false
    }
}

function Assert-Auth {
    param([string] $Path)

    if (-not (Test-UsableAuth $Path)) {
        Stop-WithError "credentials are missing, unreadable, or empty: $Path"
    }
}

function Set-SecureFileAcl {
    param([string] $Path)

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetOwner($identity.User)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object -TypeName Security.AccessControl.FileSystemAccessRule -ArgumentList @(
        $identity.User,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void] $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function New-TemporaryPath {
    param(
        [string] $Directory,
        [string] $BaseName
    )

    do {
        $candidate = Join-Path $Directory ('.{0}.codex-switch.{1}.tmp' -f $BaseName, [Guid]::NewGuid().ToString('N'))
    } while ([IO.File]::Exists($candidate))
    return $candidate
}

function Move-FileAtomically {
    param(
        [string] $TemporaryPath,
        [string] $DestinationPath
    )

    if ([IO.File]::Exists($DestinationPath)) {
        [IO.File]::Replace($TemporaryPath, $DestinationPath, $null)
    }
    else {
        [IO.File]::Move($TemporaryPath, $DestinationPath)
    }
    Set-SecureFileAcl $DestinationPath
}

function Copy-FileAtomically {
    param(
        [string] $SourcePath,
        [string] $DestinationPath
    )

    $destinationDirectory = Split-Path -Parent $DestinationPath
    $destinationName = Split-Path -Leaf $DestinationPath
    $script:CopyTemp = New-TemporaryPath $destinationDirectory $destinationName
    try {
        [IO.File]::Copy($SourcePath, $script:CopyTemp, $false)
        Set-SecureFileAcl $script:CopyTemp
        Move-FileAtomically $script:CopyTemp $DestinationPath
        $script:CopyTemp = $null
    }
    catch {
        if ($script:CopyTemp -and [IO.File]::Exists($script:CopyTemp)) {
            [IO.File]::Delete($script:CopyTemp)
        }
        $script:CopyTemp = $null
        throw (New-Object -TypeName IO.IOException -ArgumentList @("could not update $DestinationPath", $_.Exception))
    }
}

function Write-ActiveMarker {
    param(
        [string] $Value,
        [string] $ActiveHome,
        [string] $MarkerPath
    )

    $script:MarkerTemp = New-TemporaryPath $ActiveHome '.codex-switch-active'
    try {
        $utf8WithoutBom = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($script:MarkerTemp, $Value + [Environment]::NewLine, $utf8WithoutBom)
        Set-SecureFileAcl $script:MarkerTemp
        Move-FileAtomically $script:MarkerTemp $MarkerPath
        $script:MarkerTemp = $null
    }
    catch {
        if ($script:MarkerTemp -and [IO.File]::Exists($script:MarkerTemp)) {
            [IO.File]::Delete($script:MarkerTemp)
        }
        $script:MarkerTemp = $null
        throw (New-Object -TypeName IO.IOException -ArgumentList @('could not update the active-profile marker', $_.Exception))
    }
}

function Get-NormalizedDirectoryPath {
    param([string] $Path)

    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($fullPath, $root)) {
        $fullPath = $fullPath.TrimEnd([char[]] @('\', '/'))
    }
    return $fullPath
}

function Test-SameProfile {
    param([string] $Left, [string] $Right)
    return [StringComparer]::OrdinalIgnoreCase.Equals($Left, $Right)
}

try {
    $arguments = @($CommandArguments)

    if ($arguments.Count -eq 1 -and @('-h', '--help', 'help') -contains $arguments[0]) {
        Write-Usage
        exit 0
    }

    if ($arguments.Count -eq 0) {
        [Console]::Error.WriteLine('codex-switch: a command or profile is required')
        [Console]::Error.WriteLine()
        Write-Usage -ToError
        exit 64
    }

    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
        $activeHomeInput = $env:CODEX_HOME
    }
    else {
        $activeHomeInput = $env:USERPROFILE
        if ([string]::IsNullOrWhiteSpace($activeHomeInput)) {
            Stop-WithError 'USERPROFILE must be set to an absolute path'
        }
        $activeHomeInput = Join-Path $activeHomeInput '.codex'
    }

    if (-not [IO.Path]::IsPathRooted($activeHomeInput)) {
        Stop-WithError 'CODEX_HOME or USERPROFILE must resolve to an absolute path'
    }

    $activeHome = Get-NormalizedDirectoryPath $activeHomeInput
    $activeAuth = Join-Path $activeHome 'auth.json'
    $activeMarker = Join-Path $activeHome '.codex-switch-active'

    $activeProfile = $null
    if ([IO.File]::Exists($activeMarker)) {
        try {
            $markerProfile = [IO.File]::ReadLines($activeMarker) | Select-Object -First 1
            if (Test-ValidProfileName $markerProfile) {
                $activeProfile = $markerProfile
            }
        }
        catch {
            $activeProfile = $null
        }
    }

    $command = $arguments[0]
    if (@('add', 'auth', 'remove', 'switch') -contains $command) {
        if ($arguments.Count -ne 2) {
            Stop-WithError "$command requires exactly one profile" 64
        }
        $profile = $arguments[1]
        Assert-ProfileName $profile
        $command = $command.ToLowerInvariant()
    }
    elseif ($command -ieq 'list') {
        if ($arguments.Count -ne 1) {
            Stop-WithError 'list does not accept arguments' 64
        }
        $command = 'list'
    }
    else {
        if ($arguments.Count -ne 1) {
            Stop-WithError 'expected exactly one profile argument' 64
        }
        $command = 'switch'
        $profile = $arguments[0]
        Assert-ProfileName $profile
    }

    if ($command -eq 'list') {
        $profileParent = Split-Path -Parent $activeHome
        $profilePrefix = (Split-Path -Leaf $activeHome) + '-'
        $profiles = @()
        if ([IO.Directory]::Exists($profileParent)) {
            foreach ($profileDirectory in Get-ChildItem -LiteralPath $profileParent -Directory -Force) {
                if (-not $profileDirectory.Name.StartsWith($profilePrefix, [StringComparison]::OrdinalIgnoreCase)) {
                    continue
                }
                $candidateProfile = $profileDirectory.Name.Substring($profilePrefix.Length)
                $candidateAuth = Join-Path $profileDirectory.FullName 'auth.json'
                if ((Test-ValidProfileName $candidateProfile) -and (Test-UsableAuth $candidateAuth)) {
                    $profiles += $candidateProfile
                }
            }
        }

        foreach ($savedProfile in $profiles | Sort-Object) {
            if ($activeProfile -and (Test-SameProfile $savedProfile $activeProfile)) {
                [Console]::Out.WriteLine("{0}`t(active)", $savedProfile)
            }
            else {
                [Console]::Out.WriteLine($savedProfile)
            }
        }
        exit 0
    }

    $profileHome = "$activeHome-$profile"
    $profileAuth = Join-Path $profileHome 'auth.json'

    switch ($command) {
        'add' {
            Assert-Auth $activeAuth
            if (Test-Path -LiteralPath $profileHome) {
                Stop-WithError "profile already exists: $profile"
            }
            [void] [IO.Directory]::CreateDirectory($profileHome)
            Copy-FileAtomically $activeAuth $profileAuth
            [void] [IO.Directory]::CreateDirectory($activeHome)
            Write-ActiveMarker $profile $activeHome $activeMarker
            [Console]::Out.WriteLine('Added and activated Codex profile: {0}', $profile)
        }
        'auth' {
            Assert-Auth $profileAuth
            Assert-Auth $activeAuth
            Copy-FileAtomically $activeAuth $profileAuth
            Write-ActiveMarker $profile $activeHome $activeMarker
            [Console]::Out.WriteLine('Updated auth for Codex profile: {0}', $profile)
        }
        'remove' {
            Assert-Auth $profileAuth
            if ($activeProfile -and (Test-SameProfile $profile $activeProfile)) {
                Stop-WithError "cannot remove active profile: $profile"
            }
            [IO.File]::Delete($profileAuth)
            try {
                [IO.Directory]::Delete($profileHome, $false)
            }
            catch {
                # Preserve non-empty profile directories and unrelated files.
            }
            [Console]::Out.WriteLine('Removed Codex profile: {0}', $profile)
        }
        'switch' {
            Assert-Auth $profileAuth
            [void] [IO.Directory]::CreateDirectory($activeHome)
            if ($activeProfile -and (Test-SameProfile $activeProfile $profile) -and [IO.File]::Exists($activeAuth)) {
                Copy-FileAtomically $activeAuth $profileAuth
                [Console]::Out.WriteLine('Active Codex profile: {0}', $profile)
                exit 0
            }
            if ($activeProfile -and [IO.File]::Exists($activeAuth)) {
                $previousHome = "$activeHome-$activeProfile"
                if ([IO.Directory]::Exists($previousHome)) {
                    Copy-FileAtomically $activeAuth (Join-Path $previousHome 'auth.json')
                }
            }
            Copy-FileAtomically $profileAuth $activeAuth
            Write-ActiveMarker $profile $activeHome $activeMarker
            [Console]::Out.WriteLine('Active Codex profile: {0}', $profile)
        }
    }
}
catch {
    [Console]::Error.WriteLine('codex-switch: {0}', $_.Exception.Message)
    exit 1
}
finally {
    foreach ($temporaryPath in @($script:CopyTemp, $script:MarkerTemp)) {
        if ($temporaryPath -and [IO.File]::Exists($temporaryPath)) {
            try { [IO.File]::Delete($temporaryPath) } catch { }
        }
    }
}
