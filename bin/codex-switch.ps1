[CmdletBinding()]
param([Parameter(Position = 0, ValueFromRemainingArguments = $true)][string[]] $CommandArguments)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Usage {
@'
Usage: codex-switch <profile>
       codex-switch switch <profile>
       codex-switch run [<profile>] [--] [codex arguments...]
       codex-switch add <profile>
       codex-switch login <profile> [codex login arguments...]
       codex-switch auth <profile> [codex login arguments...]
       codex-switch logout <profile>
       codex-switch status [<profile>]
       codex-switch remove <profile>
       codex-switch list
       codex-switch current
       codex-switch shell-init
       codex-switch --help
'@
}

function Stop-WithError([string] $Message, [int] $ExitCode = 1) {
    [Console]::Error.WriteLine('codex-switch: {0}', $Message)
    exit $ExitCode
}

function Test-ValidProfileName([AllowNull()][AllowEmptyString()][string] $Name) {
    if ([string]::IsNullOrEmpty($Name) -or $Name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') { return $false }
    return @('add','auth','current','help','list','login','logout','remove','run','shell-init','status','switch') -notcontains $Name.ToLowerInvariant()
}

function Assert-ProfileName([string] $Name) {
    if (-not (Test-ValidProfileName $Name)) { Stop-WithError "invalid profile name: $Name" 64 }
}

function Test-UsableAuth([string] $Path) {
    try { return [IO.File]::Exists($Path) -and ([IO.FileInfo] $Path).Length -gt 0 } catch { return $false }
}

function Assert-Auth([string] $Path, [string] $Profile) {
    if (-not (Test-UsableAuth $Path)) { Stop-WithError "profile is not signed in: $Profile (run: codex-switch login $Profile)" }
}

function Set-SecureFileAcl([string] $Path) {
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

function Copy-FileAtomically([string] $Source, [string] $Destination) {
    $temporary = "$Destination.codex-switch.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::Copy($Source, $temporary, $false)
        Set-SecureFileAcl $temporary
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    }
    finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Write-ActiveMarker([string] $Value) {
    [void] [IO.Directory]::CreateDirectory($script:ActiveHome)
    $temporary = "$script:ActiveMarker.codex-switch.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temporary, $Value + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
        Set-SecureFileAcl $temporary
        Move-Item -LiteralPath $temporary -Destination $script:ActiveMarker -Force
    }
    finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Get-ProfileDirectories {
    $parent = Split-Path -Parent $script:ActiveHome
    $leaf = Split-Path -Leaf $script:ActiveHome
    $prefix = $leaf + '-'
    if (-not [IO.Directory]::Exists($parent)) { return @() }
    return @(Get-ChildItem -LiteralPath $parent -Directory -Force | Where-Object {
        $_.Name.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-ValidProfileName $_.Name.Substring($prefix.Length))
    })
}

function Merge-Directory([string] $Source, [string] $Destination) {
    if (-not [IO.Directory]::Exists($Source)) { return }
    foreach ($file in Get-ChildItem -LiteralPath $Source -File -Recurse -Force) {
        $relative = $file.FullName.Substring($Source.Length).TrimStart([char[]] @('\','/'))
        $target = Join-Path $Destination $relative
        [void] [IO.Directory]::CreateDirectory((Split-Path -Parent $target))
        if (-not [IO.File]::Exists($target) -or $file.LastWriteTimeUtc -gt ([IO.FileInfo] $target).LastWriteTimeUtc) {
            [IO.File]::Copy($file.FullName, $target, $true)
        }
    }
}

function Add-DirectoryJunction([string] $Path, [string] $Target, [string] $Profile) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            if (@($item.Target) -contains $Target) { return }
            Stop-WithError "managed session path points somewhere unexpected: $Path"
        }
        $legacy = Join-Path $script:ActiveHome ".codex-switch-legacy-state\$Profile\$($item.Name)"
        [void] [IO.Directory]::CreateDirectory((Split-Path -Parent $legacy))
        Merge-Directory $Path $Target
        if (-not (Test-Path -LiteralPath $legacy)) { Move-Item -LiteralPath $Path -Destination $legacy }
        else { Remove-Item -LiteralPath $Path -Recurse }
    }
    [void] [IO.Directory]::CreateDirectory($Target)
    [void] (New-Item -ItemType Junction -Path $Path -Target $Target)
}

function Add-SharedFile([string] $Path, [string] $Target, [string] $Profile) {
    if (-not [IO.File]::Exists($Target)) {
        [IO.File]::WriteAllText($Target, '', (New-Object Text.UTF8Encoding($false)))
        Set-SecureFileAcl $Target
    }
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        $linkType = $item.PSObject.Properties['LinkType']
        if ($linkType -and @('HardLink','SymbolicLink') -contains $linkType.Value) { return }
        $legacy = Join-Path $script:ActiveHome ".codex-switch-legacy-state\$Profile\$($item.Name)"
        [void] [IO.Directory]::CreateDirectory((Split-Path -Parent $legacy))
        $existing = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        foreach ($line in [IO.File]::ReadLines($Target)) { [void] $existing.Add($line) }
        $writer = [IO.File]::AppendText($Target)
        try { foreach ($line in [IO.File]::ReadLines($Path)) { if ($existing.Add($line)) { $writer.WriteLine($line) } } }
        finally { $writer.Dispose() }
        if (-not (Test-Path -LiteralPath $legacy)) { Move-Item -LiteralPath $Path -Destination $legacy }
        else { Remove-Item -LiteralPath $Path }
    }
    try { [void] (New-Item -ItemType HardLink -Path $Path -Target $Target) }
    catch { [void] (New-Item -ItemType SymbolicLink -Path $Path -Target $Target) }
}

function Invoke-SqliteMigration {
    $needed = $false
    foreach ($directory in Get-ProfileDirectories) {
        $profile = $directory.Name.Substring((Split-Path -Leaf $script:ActiveHome).Length + 1)
        $marker = Join-Path $script:ActiveHome ".codex-switch-legacy-state\$profile\sqlite\.merged-v1"
        if (Test-Path -LiteralPath $marker) { continue }
        foreach ($name in @('state_5.sqlite','thread_history_1.sqlite','goals_1.sqlite','queue_1.sqlite','memories_1.sqlite','logs_2.sqlite')) {
            if ([IO.File]::Exists((Join-Path $directory.FullName $name))) { $needed = $true; break }
        }
    }
    if (-not $needed) { return }
    $helper = if ($env:CODEX_SWITCH_MIGRATION_HELPER) { $env:CODEX_SWITCH_MIGRATION_HELPER } else { Join-Path $PSScriptRoot 'codex-switch-migrate.py' }
    if (-not [IO.File]::Exists($helper)) { Stop-WithError "could not find SQLite migration helper: $helper" }
    if ($env:CODEX_SWITCH_PYTHON) { $python = $env:CODEX_SWITCH_PYTHON; $prefix = @() }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { $python = 'py'; $prefix = @('-3') }
    elseif (Get-Command python3 -ErrorAction SilentlyContinue) { $python = 'python3'; $prefix = @() }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $python = 'python'; $prefix = @() }
    else { Stop-WithError 'Python 3 is required once to merge existing profile session metadata' }
    & $python @prefix $helper '--active-home' $script:ActiveHome
    if ($LASTEXITCODE -ne 0) { Stop-WithError 'legacy profile session metadata could not be migrated; close running Codex sessions and retry' }
}

function Migrate-LegacyAuth {
    $marker = Join-Path $script:ActiveHome '.codex-switch-auth-isolation-v1'
    if ([IO.File]::Exists($marker)) { return }
    if ($script:ActiveProfile) {
        $centralAuth = Join-Path $script:ActiveHome 'auth.json'
        $profileHome = "$($script:ActiveHome)-$($script:ActiveProfile)"
        $profileAuth = Join-Path $profileHome 'auth.json'
        $hasOldLayout =
            -not (Test-Path -LiteralPath (Join-Path $profileHome 'sessions')) -and
            -not (Test-Path -LiteralPath (Join-Path $profileHome 'config.toml'))
        if ($hasOldLayout -and (Test-UsableAuth $centralAuth) -and [IO.Directory]::Exists($profileHome)) {
            $backup = Join-Path $script:ActiveHome ".codex-switch-legacy-state\$($script:ActiveProfile)\auth.json.pre-isolation"
            [void] [IO.Directory]::CreateDirectory((Split-Path -Parent $backup))
            if ([IO.File]::Exists($profileAuth) -and -not [IO.File]::Exists($backup)) { Copy-FileAtomically $profileAuth $backup }
            Copy-FileAtomically $centralAuth $profileAuth
        }
    }
    [IO.File]::WriteAllText($marker, "complete`n", (New-Object Text.UTF8Encoding($false)))
    Set-SecureFileAcl $marker
}

function Prepare-Profile([string] $Profile) {
    $profileHome = "$($script:ActiveHome)-$Profile"
    [void] [IO.Directory]::CreateDirectory($profileHome)
    Invoke-SqliteMigration
    foreach ($name in @('sessions','archived_sessions','thread-writer-locks')) {
        Add-DirectoryJunction (Join-Path $profileHome $name) (Join-Path $script:ActiveHome $name) $Profile
    }
    foreach ($name in @('history.jsonl','session_index.jsonl','transcription-history.jsonl')) {
        Add-SharedFile (Join-Path $profileHome $name) (Join-Path $script:ActiveHome $name) $Profile
    }
    foreach ($name in @('skills','plugins','packages')) {
        $target = Join-Path $script:ActiveHome $name
        if (Test-Path -LiteralPath $target) { Add-DirectoryJunction (Join-Path $profileHome $name) $target $Profile }
    }
    $config = Join-Path $script:ActiveHome 'config.toml'
    if ([IO.File]::Exists($config) -and -not (Test-Path -LiteralPath (Join-Path $profileHome 'config.toml'))) {
        try { [void] (New-Item -ItemType HardLink -Path (Join-Path $profileHome 'config.toml') -Target $config) }
        catch { [void] (New-Item -ItemType SymbolicLink -Path (Join-Path $profileHome 'config.toml') -Target $config) }
    }
    return $profileHome
}

function Invoke-Codex([string] $Profile, [string[]] $Arguments) {
    $profileHome = Prepare-Profile $Profile
    $codex = if ($env:CODEX_SWITCH_CODEX_BIN) { $env:CODEX_SWITCH_CODEX_BIN } else { 'codex' }
    if (-not (Get-Command $codex -ErrorAction SilentlyContinue)) { Stop-WithError "could not find Codex executable: $codex" }
    $oldHome = $env:CODEX_HOME; $oldSqliteHome = $env:CODEX_SQLITE_HOME
    try {
        $env:CODEX_HOME = $profileHome
        $env:CODEX_SQLITE_HOME = $script:ActiveHome
        $global:LASTEXITCODE = 0
        & $codex '-c' 'cli_auth_credentials_store="file"' @Arguments
        exit $LASTEXITCODE
    }
    finally { $env:CODEX_HOME = $oldHome; $env:CODEX_SQLITE_HOME = $oldSqliteHome }
}

try {
    $arguments = @($CommandArguments)
    if ($arguments.Count -eq 1 -and @('-h','--help','help') -contains $arguments[0]) { Write-Usage; exit 0 }
    if ($arguments.Count -eq 0) { Stop-WithError 'a command or profile is required' 64 }
    $homeInput = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    if (-not [IO.Path]::IsPathRooted($homeInput)) { Stop-WithError 'CODEX_HOME or USERPROFILE must resolve to an absolute path' }
    $script:ActiveHome = [IO.Path]::GetFullPath($homeInput).TrimEnd([char[]] @('\','/'))
    [void] [IO.Directory]::CreateDirectory($script:ActiveHome)
    $script:ActiveMarker = Join-Path $script:ActiveHome '.codex-switch-active'
    $script:ActiveProfile = $null
    if ([IO.File]::Exists($script:ActiveMarker)) {
        $candidate = [IO.File]::ReadLines($script:ActiveMarker) | Select-Object -First 1
        if (Test-ValidProfileName $candidate) { $script:ActiveProfile = $candidate }
    }

    $command = $arguments[0].ToLowerInvariant()
    if (@('add','logout','remove','switch') -contains $command) {
        if ($arguments.Count -ne 2) { Stop-WithError "$command requires exactly one profile" 64 }
        $profile = $arguments[1]; Assert-ProfileName $profile
    } elseif (@('auth','login') -contains $command) {
        if ($arguments.Count -lt 2) { Stop-WithError "$command requires a profile" 64 }
        $profile = $arguments[1]; Assert-ProfileName $profile
    } elseif (@('current','list','shell-init') -contains $command) {
        if ($arguments.Count -ne 1) { Stop-WithError "$command does not accept arguments" 64 }
    } elseif ($command -eq 'status') {
        if ($arguments.Count -gt 2) { Stop-WithError 'status accepts at most one profile' 64 }
    } elseif ($command -ne 'run') {
        if ($arguments.Count -ne 1) { Stop-WithError 'expected exactly one profile argument' 64 }
        $profile = $arguments[0]; Assert-ProfileName $profile; $command = 'switch'
    }

    if (@('add','auth','login','logout','remove','run','status','switch') -contains $command) { Migrate-LegacyAuth }
    switch ($command) {
        'current' { if (-not $script:ActiveProfile) { Stop-WithError 'no active profile' }; $script:ActiveProfile; break }
        'shell-init' { 'function global:codex { codex-switch run -- @args }'; break }
        'list' {
            foreach ($directory in Get-ProfileDirectories) {
                $name = $directory.Name.Substring((Split-Path -Leaf $script:ActiveHome).Length + 1)
                $suffix = if (-not (Test-UsableAuth (Join-Path $directory.FullName 'auth.json'))) { "`t(signed out)" } else { '' }
                if ($script:ActiveProfile -and $name -ieq $script:ActiveProfile) { $suffix += "`t(active)" }
                "$name$suffix"
            }; break
        }
        'add' {
            $profileHome = "$($script:ActiveHome)-$profile"
            if (Test-Path -LiteralPath $profileHome) { Stop-WithError "profile already exists: $profile" }
            [void] (Prepare-Profile $profile); Write-ActiveMarker $profile
            "Added and selected Codex profile: $profile"; "Sign in with: codex-switch login $profile"; break
        }
        { $_ -in @('auth','login') } {
            [void] (Prepare-Profile $profile); Write-ActiveMarker $profile
            $loginArguments = @('login')
            if ($arguments.Count -gt 2) { $loginArguments += @($arguments | Select-Object -Skip 2) }
            Invoke-Codex $profile $loginArguments
        }
        'logout' {
            $profileHome = "$($script:ActiveHome)-$profile"
            if (-not [IO.Directory]::Exists($profileHome)) { Stop-WithError "profile does not exist: $profile" }
            Invoke-Codex $profile @('logout')
        }
        'remove' {
            $profileHome = "$($script:ActiveHome)-$profile"
            if (-not [IO.Directory]::Exists($profileHome)) { Stop-WithError "profile does not exist: $profile" }
            if ($script:ActiveProfile -and $profile -ieq $script:ActiveProfile) { Stop-WithError "cannot remove active profile: $profile" }
            $auth = Join-Path $profileHome 'auth.json'; if ([IO.File]::Exists($auth)) { [IO.File]::Delete($auth) }
            foreach ($name in @('config.toml','skills','plugins','packages','sessions','archived_sessions','thread-writer-locks','history.jsonl','session_index.jsonl','transcription-history.jsonl')) {
                $path = Join-Path $profileHome $name; if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
            }
            try { [IO.Directory]::Delete($profileHome, $false) } catch { }
            "Removed Codex profile: $profile"; break
        }
        'switch' {
            $profileHome = "$($script:ActiveHome)-$profile"
            if (-not [IO.Directory]::Exists($profileHome)) { Stop-WithError "profile does not exist: $profile" }
            Assert-Auth (Join-Path $profileHome 'auth.json') $profile
            [void] (Prepare-Profile $profile); Write-ActiveMarker $profile
            "Selected Codex profile: $profile"; break
        }
        'status' {
            $profile = if ($arguments.Count -eq 2) { $arguments[1] } else { $script:ActiveProfile }
            if (-not $profile) { Stop-WithError 'no active profile' }
            Assert-ProfileName $profile; Invoke-Codex $profile @('login','status')
        }
        'run' {
            $rest = @($arguments | Select-Object -Skip 1)
            if ($rest.Count -gt 0 -and $rest[0] -eq '--') { $rest = @($rest | Select-Object -Skip 1); $profile = $script:ActiveProfile }
            elseif ($rest.Count -gt 0 -and (Test-ValidProfileName $rest[0]) -and [IO.Directory]::Exists("$($script:ActiveHome)-$($rest[0])")) {
                $profile = $rest[0]; $rest = @($rest | Select-Object -Skip 1); Write-ActiveMarker $profile
            } else { $profile = $script:ActiveProfile }
            if (-not $profile) { Stop-WithError 'run requires a profile when none is active' 64 }
            Invoke-Codex $profile $rest
        }
    }
}
catch { [Console]::Error.WriteLine('codex-switch: {0}', $_.Exception.Message); exit 1 }
