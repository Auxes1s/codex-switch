[CmdletBinding()]
param([Parameter(Position = 0, ValueFromRemainingArguments = $true)][string[]] $CommandArguments)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Usage {
@'
Usage: codex-switch <profile>
       codex-switch switch <profile>
       codex-switch run [<profile>] [codex arguments...]
       codex-switch add <profile>
       codex-switch login <profile> [codex login arguments...]
       codex-switch login <profile> --expect-domain <domain> [codex login arguments...]
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

function Enable-CodexInCurrentPowerShell {
    # A PowerShell process opened before shell-init was installed will still
    # resolve `codex` to the npm shim. A direct .ps1 switch can repair that
    # process immediately without changing another running Codex session.
    $launcher = (Join-Path $PSScriptRoot 'codex-switch.ps1').Replace("'", "''")
    Set-Item -Path Function:\global:codex -Value ([scriptblock]::Create("& '$launcher' run @args"))
}

function Stop-WithError([string] $Message, [int] $ExitCode = 1) {
    [Console]::Error.WriteLine('codex-switch: {0}', $Message)
    exit $ExitCode
}

function Test-ValidProfileName([AllowNull()][AllowEmptyString()][string] $Name) {
    if ([string]::IsNullOrEmpty($Name) -or $Name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$' -or $Name.EndsWith('.')) { return $false }
    return @('add','auth','current','help','list','login','logout','remove','run','shell-init','status','switch') -notcontains $Name.ToLowerInvariant()
}

function Assert-ProfileName([string] $Name) {
    if (-not (Test-ValidProfileName $Name)) { Stop-WithError "invalid profile name: $Name" 64 }
}

function Test-UsableAuth([string] $Path) {
    try { return [IO.File]::Exists($Path) -and ([IO.FileInfo] $Path).Length -gt 0 } catch { return $false }
}

function Read-FirstLine([string] $Path) {
    $reader = [IO.File]::OpenText($Path)
    try { return $reader.ReadLine() }
    finally { $reader.Dispose() }
}

function Assert-Auth([string] $Path, [string] $Profile) {
    if (-not (Test-UsableAuth $Path)) { Stop-WithError "profile is not signed in: $Profile (run: codex-switch login $Profile)" }
}

function Get-AuthMetadata([string] $Path) {
    try {
        $auth = [IO.File]::ReadAllText($Path) | ConvertFrom-Json
        $mode = [string] $auth.auth_mode
        if ([string]::IsNullOrWhiteSpace($mode)) { throw 'missing auth mode' }
        $identity = $null; $domain = $null; $refreshed = $null
        if ($mode -eq 'chatgpt') {
            $token = [string] $auth.tokens.id_token
            if ([string]::IsNullOrWhiteSpace($token) -or [string]::IsNullOrWhiteSpace([string] $auth.tokens.refresh_token)) { throw 'missing ChatGPT tokens' }
            $parts = $token.Split('.')
            if ($parts.Count -lt 2) { throw 'invalid ID token' }
            $payload = $parts[1].Replace('-', '+').Replace('_', '/')
            $payload = $payload.PadRight($payload.Length + ((4 - $payload.Length % 4) % 4), '=')
            $claims = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload)) | ConvertFrom-Json
            if ([string]::IsNullOrWhiteSpace([string] $claims.sub)) { throw 'missing account identity' }
            $identity = ([string] $claims.sub) + ':' + ([string] $auth.tokens.account_id)
            $email = [string] $claims.email
            if ($email -match '@([^@]+)$') { $domain = $Matches[1].ToLowerInvariant() }
            $refreshed = [DateTimeOffset]::Parse([string] $auth.last_refresh, [Globalization.CultureInfo]::InvariantCulture)
        }
        return [pscustomobject]@{ Mode = $mode; Identity = $identity; Domain = $domain; Refreshed = $refreshed; Hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }
    }
    catch { throw "invalid Codex credentials at $Path" }
}

function Get-ExpectedDomain([string] $ProfileHome) {
    $path = Join-Path $ProfileHome '.codex-switch-expected-domain'
    if (-not [IO.File]::Exists($path)) { return $null }
    $domain = (Read-FirstLine $path).Trim().ToLowerInvariant()
    if ($domain -notmatch '^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$') { throw "invalid expected domain for profile $(Split-Path -Leaf $ProfileHome)" }
    return $domain
}

function Assert-ExpectedDomain([string] $ProfileHome, [string] $AuthPath) {
    $expected = Get-ExpectedDomain $ProfileHome
    if (-not $expected) { return }
    $actual = (Get-AuthMetadata $AuthPath).Domain
    if ($actual -cne $expected) { throw "profile $(Split-Path -Leaf $ProfileHome) has a different account domain; sign in to $expected" }
}

function Set-SecureFileAcl([string] $Path) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetOwner($identity.User)
    $acl.SetGroup($identity.User)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object -TypeName Security.AccessControl.FileSystemAccessRule -ArgumentList @(
        $identity.User,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void] $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Test-SecureFileAcl([string] $Path) {
    try {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = Get-Acl -LiteralPath $Path
        if (-not $acl.AreAccessRulesProtected -or $acl.GetOwner([Security.Principal.SecurityIdentifier]) -ne $sid) { return $false }
        $rules = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
        return $rules.Count -eq 1 -and $rules[0].IdentityReference -eq $sid -and
            $rules[0].AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            ($rules[0].FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl
    }
    catch { return $false }
}

function Move-FileAtomically([string] $Temporary, [string] $Destination) {
    if (-not [IO.File]::Exists($Destination)) { [IO.File]::Move($Temporary, $Destination); return }
    if (-not (Test-SecureFileAcl $Destination)) { Set-SecureFileAcl $Destination }
    $backup = "$Destination.codex-switch.$([Guid]::NewGuid().ToString('N')).backup"
    try {
        for ($attempt = 0; $attempt -lt 5; $attempt++) {
            try { [IO.File]::Replace($Temporary, $Destination, $backup); return }
            catch [IO.IOException] {
                if ($attempt -eq 4) { throw "could not replace $Destination after retries: $($_.Exception.Message)" }
                Start-Sleep -Milliseconds 100
            }
        }
    }
    finally { if ([IO.File]::Exists($backup)) { [IO.File]::Delete($backup) } }
}

function Copy-FileAtomically([string] $Source, [string] $Destination) {
    $temporary = "$Destination.codex-switch.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::Copy($Source, $temporary, $false)
        Set-SecureFileAcl $temporary
        Move-FileAtomically $temporary $Destination
    }
    finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Restore-FileAtomically([string] $Destination, [byte[]] $Original) {
    if ($null -eq $Original) {
        if ([IO.File]::Exists($Destination)) { [IO.File]::Delete($Destination) }
        return
    }
    $temporary = "$Destination.codex-switch.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllBytes($temporary, $Original)
        Set-SecureFileAcl $temporary
        Move-FileAtomically $temporary $Destination
    }
    finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Write-ActiveMarker([string] $Value) {
    [void] [IO.Directory]::CreateDirectory($script:ActiveHome)
    $temporary = "$script:ActiveMarker.codex-switch.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temporary, $Value + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
        Set-SecureFileAcl $temporary
        Move-FileAtomically $temporary $script:ActiveMarker
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
        else { Move-Item -LiteralPath $Path -Destination ($legacy + '.duplicate.' + [Guid]::NewGuid().ToString('N')) }
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
        else { Move-Item -LiteralPath $Path -Destination ($legacy + '.duplicate.' + [Guid]::NewGuid().ToString('N')) }
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
        foreach ($name in @('state_5.sqlite','thread_history_1.sqlite','goals_1.sqlite','queue_1.sqlite','memories_1.sqlite','logs_2.sqlite','agent_message_board_1.sqlite')) {
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

function Backup-UnassignedLegacyAuth([string] $CentralAuth) {
    $unassigned = Join-Path $script:ActiveHome '.codex-switch-legacy-state\unassigned\auth.json.pre-isolation'
    [void] [IO.Directory]::CreateDirectory((Split-Path -Parent $unassigned))
    if ([IO.File]::Exists($unassigned)) { $unassigned += '.' + [Guid]::NewGuid().ToString('N') }
    Copy-FileAtomically $CentralAuth $unassigned
    [Console]::Error.WriteLine('codex-switch: saved the unattributed legacy login at {0}', $unassigned)
}

function Migrate-LegacyAuth {
    $marker = Join-Path $script:ActiveHome '.codex-switch-auth-isolation-v1'
    if ([IO.File]::Exists($marker)) { return }
    $centralAuth = Join-Path $script:ActiveHome 'auth.json'
    if ((Test-UsableAuth $centralAuth) -and -not $script:ActiveProfile) {
        Backup-UnassignedLegacyAuth $centralAuth
    }
    elseif ((Test-UsableAuth $centralAuth) -and $script:ActiveProfile) {
        $profileHome = "$($script:ActiveHome)-$($script:ActiveProfile)"
        $profileAuth = Join-Path $profileHome 'auth.json'
        if (-not (Test-UsableAuth $profileAuth)) {
            Backup-UnassignedLegacyAuth $centralAuth
            [Console]::Error.WriteLine('codex-switch: sign in to the selected profile explicitly')
        }
        else {
            $backup = Join-Path $script:ActiveHome ".codex-switch-legacy-state\$($script:ActiveProfile)\auth.json.pre-isolation"
            [void] [IO.Directory]::CreateDirectory((Split-Path -Parent $backup))
            if (-not [IO.File]::Exists($backup)) { Copy-FileAtomically $profileAuth $backup }
            $live = Get-AuthMetadata $centralAuth
            $saved = Get-AuthMetadata $profileAuth
            if ($live.Mode -cne $saved.Mode -or $live.Identity -cne $saved.Identity) {
                throw 'the live account differs from its marked profile; recovery requires an explicit account assignment'
            }
            if ($live.Hash -cne $saved.Hash) {
                if ($live.Mode -eq 'chatgpt' -and $live.Refreshed -gt $saved.Refreshed) { Copy-FileAtomically $centralAuth $profileAuth }
                elseif ($live.Mode -eq 'chatgpt' -and $saved.Refreshed -gt $live.Refreshed) { }
                else { throw 'live and saved credentials conflict at the same refresh time' }
            }
        }
    }
    [IO.File]::WriteAllText($marker, "complete`n", (New-Object Text.UTF8Encoding($false)))
    Set-SecureFileAcl $marker
}

function Sync-SharedConfig([string] $ProfileHome, [string] $Profile) {
    $source = Join-Path $script:ActiveHome 'config.toml'
    if (-not [IO.File]::Exists($source)) { return }
    $destination = Join-Path $ProfileHome 'config.toml'
    $marker = Join-Path $ProfileHome '.codex-switch-config-shared-v1'
    if (-not [IO.File]::Exists($marker)) {
        if ([IO.File]::Exists($destination)) {
            $legacy = Join-Path $script:ActiveHome ".codex-switch-legacy-state\$Profile\config.toml"
            [void] [IO.Directory]::CreateDirectory((Split-Path -Parent $legacy))
            if (-not [IO.File]::Exists($legacy)) { [IO.File]::Copy($destination, $legacy) }
        }
        [IO.File]::WriteAllText($marker, "shared`n", (New-Object Text.UTF8Encoding($false)))
        Set-SecureFileAcl $marker
    }
    if (-not [IO.File]::Exists($destination) -or (Get-FileHash -LiteralPath $source).Hash -cne (Get-FileHash -LiteralPath $destination).Hash) {
        Copy-FileAtomically $source $destination
    }
}

function Prepare-Profile([string] $Profile) {
    $profileHome = "$($script:ActiveHome)-$Profile"
    [void] [IO.Directory]::CreateDirectory($profileHome)
    Invoke-SqliteMigration
    foreach ($directory in Get-ProfileDirectories) {
        $nameForDirectory = $directory.Name.Substring((Split-Path -Leaf $script:ActiveHome).Length + 1)
        foreach ($name in @('sessions','archived_sessions','thread-writer-locks')) {
            Add-DirectoryJunction (Join-Path $directory.FullName $name) (Join-Path $script:ActiveHome $name) $nameForDirectory
        }
        foreach ($name in @('history.jsonl','session_index.jsonl','transcription-history.jsonl')) {
            Add-SharedFile (Join-Path $directory.FullName $name) (Join-Path $script:ActiveHome $name) $nameForDirectory
        }
        foreach ($name in @('skills','plugins','packages')) {
            $target = Join-Path $script:ActiveHome $name
            if (Test-Path -LiteralPath $target) { Add-DirectoryJunction (Join-Path $directory.FullName $name) $target $nameForDirectory }
        }
        Sync-SharedConfig $directory.FullName $nameForDirectory
    }
    return $profileHome
}

function Invoke-CodexAtHome([string] $CodexHome, [string[]] $Arguments, [switch] $NoDaemon, [switch] $DisableHooks) {
    $codex = if ($env:CODEX_SWITCH_CODEX_BIN) { $env:CODEX_SWITCH_CODEX_BIN } else { 'codex' }
    $codexCommand = Get-Command $codex -All -ErrorAction SilentlyContinue | Where-Object { $_.CommandType -in @('Application','ExternalScript') } | Select-Object -First 1
    if (-not $codexCommand) { Stop-WithError "could not find Codex executable: $codex" }
    $oldHome = $env:CODEX_HOME; $oldSqliteHome = $env:CODEX_SQLITE_HOME
    $authVariables = @('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN','OPENAI_IDENTITY_TOKEN_FILE','OPENAI_FEDERATION_RULE_ID')
    $oldAuthVariables = @{}
    try {
        $env:CODEX_HOME = $CodexHome
        $env:CODEX_SQLITE_HOME = $script:ActiveHome
        foreach ($name in $authVariables) {
            $oldAuthVariables[$name] = [Environment]::GetEnvironmentVariable($name)
            Remove-Item -Path ('Env:' + $name) -ErrorAction SilentlyContinue
        }
        $flags = @()
        if ($NoDaemon) { $flags += '--no-daemon' }
        if ($DisableHooks) { $flags += @('--disable','hooks') }
        $global:LASTEXITCODE = 0
        & $codexCommand.Source '-c' 'cli_auth_credentials_store="file"' @flags @Arguments
        $script:LastCodexExitCode = $LASTEXITCODE
    }
    finally {
        $env:CODEX_HOME = $oldHome; $env:CODEX_SQLITE_HOME = $oldSqliteHome
        foreach ($name in $authVariables) {
            if ($null -eq $oldAuthVariables[$name]) { Remove-Item -Path ('Env:' + $name) -ErrorAction SilentlyContinue }
            else { [Environment]::SetEnvironmentVariable($name, $oldAuthVariables[$name]) }
        }
        $authPath = Join-Path $CodexHome 'auth.json'
        if ([IO.File]::Exists($authPath) -and -not (Test-SecureFileAcl $authPath)) { Set-SecureFileAcl $authPath }
    }
}

function Invoke-Codex([string] $Profile, [string[]] $Arguments, [switch] $NoDaemon, [switch] $DisableHooks) {
    $profileHome = Prepare-Profile $Profile
    Invoke-CodexAtHome $profileHome $Arguments -NoDaemon:$NoDaemon -DisableHooks:$DisableHooks
}

function Write-ExpectedDomain([string] $ProfileHome, [string] $Domain) {
    $path = Join-Path $ProfileHome '.codex-switch-expected-domain'
    $temporary = "$path.codex-switch.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temporary, $Domain + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
        Set-SecureFileAcl $temporary
        Move-FileAtomically $temporary $path
    }
    finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Invoke-StagedLogin([string] $Profile, [string[]] $LoginArguments, [string] $ExpectedDomain) {
    $stageRoot = Join-Path $(if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { [IO.Path]::GetTempPath() }) 'codex-switch\login-staging'
    $stageHome = Join-Path $stageRoot ([Guid]::NewGuid().ToString('N'))
    $published = $false
    $discardStage = $false
    try {
        [void] [IO.Directory]::CreateDirectory($stageHome)
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        & icacls.exe $stageHome '/inheritance:r' '/grant:r' ('*' + $sid + ':(OI)(CI)F') > $null
        if ($LASTEXITCODE -ne 0) { throw 'could not secure temporary login home' }
        $config = Join-Path $script:ActiveHome 'config.toml'
        if ([IO.File]::Exists($config)) { Copy-FileAtomically $config (Join-Path $stageHome 'config.toml') }
        Invoke-CodexAtHome $stageHome (@('login') + $LoginArguments)
        if ($script:LastCodexExitCode -ne 0) { throw "Codex login exited with code $($script:LastCodexExitCode)" }
        $stageAuth = Join-Path $stageHome 'auth.json'
        Assert-Auth $stageAuth $Profile
        $metadata = Get-AuthMetadata $stageAuth
        if ($ExpectedDomain -and $metadata.Domain -cne $ExpectedDomain) {
            $discardStage = $true
            throw "login used a different account domain; expected $ExpectedDomain"
        }
        $profileHome = Prepare-Profile $Profile
        $profileAuth = Join-Path $profileHome 'auth.json'
        $domainPath = Join-Path $profileHome '.codex-switch-expected-domain'
        $authBefore = if ([IO.File]::Exists($profileAuth)) { [IO.File]::ReadAllBytes($profileAuth) } else { $null }
        $domainBefore = if ([IO.File]::Exists($domainPath)) { [IO.File]::ReadAllBytes($domainPath) } else { $null }
        $markerBefore = if ([IO.File]::Exists($script:ActiveMarker)) { [IO.File]::ReadAllBytes($script:ActiveMarker) } else { $null }
        $authPublished = $false; $domainPublished = $false; $markerPublished = $false
        try {
            Copy-FileAtomically $stageAuth $profileAuth
            $authPublished = $true
            if ($ExpectedDomain) { Write-ExpectedDomain $profileHome $ExpectedDomain; $domainPublished = $true }
            Write-ActiveMarker $Profile
            $markerPublished = $true
        }
        catch {
            $publicationError = $_
            try {
                if ($markerPublished) { Restore-FileAtomically $script:ActiveMarker $markerBefore }
                if ($domainPublished) { Restore-FileAtomically $domainPath $domainBefore }
                if ($authPublished) { Restore-FileAtomically $profileAuth $authBefore }
            }
            catch { throw "login publication failed and rollback failed: $($_.Exception.Message); secured staged login: $stageHome" }
            throw $publicationError
        }
        $published = $true
        [Console]::Out.WriteLine('Signed in and selected Codex profile: {0}', $Profile)
    }
    finally {
        $root = [IO.Path]::GetFullPath($stageRoot).TrimEnd([char[]] @('\','/')) + [IO.Path]::DirectorySeparatorChar
        $resolvedHome = [IO.Path]::GetFullPath($stageHome)
        if ($resolvedHome.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) -and [IO.Directory]::Exists($resolvedHome)) {
            if ($published -or $discardStage -or -not [IO.File]::Exists((Join-Path $resolvedHome 'auth.json'))) {
                Remove-Item -LiteralPath $resolvedHome -Recurse -Force
            }
            else {
                [Console]::Error.WriteLine('codex-switch: uninstalled login remains in secured staging home: {0}', $resolvedHome)
            }
        }
    }
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
        $candidate = Read-FirstLine $script:ActiveMarker
        if (Test-ValidProfileName $candidate) { $script:ActiveProfile = $candidate }
        elseif ($candidate -and $candidate.EndsWith('.')) {
            $canonical = $candidate.TrimEnd([char] '.')
            if ((Test-ValidProfileName $canonical) -and [IO.Directory]::Exists("$($script:ActiveHome)-$canonical")) {
                $script:ActiveProfile = $canonical
            }
        }
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
        'shell-init' {
            $launcher = (Join-Path $PSScriptRoot 'codex-switch.ps1').Replace("'", "''")
            "function global:codex { & '$launcher' run @args }"
            break
        }
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
            $profileHome = "$($script:ActiveHome)-$profile"
            $loginArguments = @(if ($arguments.Count -gt 2) { $arguments | Select-Object -Skip 2 })
            $expectedDomain = Get-ExpectedDomain $profileHome
            if ($loginArguments.Count -gt 0 -and $loginArguments[0] -eq '--expect-domain') {
                if ($loginArguments.Count -lt 2) { Stop-WithError '--expect-domain requires a domain' 64 }
                $expectedDomain = $loginArguments[1].ToLowerInvariant()
                if ($expectedDomain -notmatch '^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$') { Stop-WithError 'invalid expected domain' 64 }
                $loginArguments = @($loginArguments | Select-Object -Skip 2)
            }
            Invoke-StagedLogin $profile $loginArguments $expectedDomain
            exit 0
        }
        'logout' {
            $profileHome = "$($script:ActiveHome)-$profile"
            if (-not [IO.Directory]::Exists($profileHome)) { Stop-WithError "profile does not exist: $profile" }
            Invoke-Codex $profile @('logout')
            exit $script:LastCodexExitCode
        }
        'remove' {
            $profileHome = "$($script:ActiveHome)-$profile"
            if (-not [IO.Directory]::Exists($profileHome)) { Stop-WithError "profile does not exist: $profile" }
            if ($script:ActiveProfile -and $profile -ieq $script:ActiveProfile) { Stop-WithError "cannot remove active profile: $profile" }
            $auth = Join-Path $profileHome 'auth.json'; if ([IO.File]::Exists($auth)) { [IO.File]::Delete($auth) }
            foreach ($name in @('config.toml','.codex-switch-config-shared-v1','.codex-switch-expected-domain','skills','plugins','packages','sessions','archived_sessions','thread-writer-locks','history.jsonl','session_index.jsonl','transcription-history.jsonl')) {
                $path = Join-Path $profileHome $name; if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
            }
            try { [IO.Directory]::Delete($profileHome, $false) } catch { }
            "Removed Codex profile: $profile"; break
        }
        'switch' {
            $profileHome = "$($script:ActiveHome)-$profile"
            if (-not [IO.Directory]::Exists($profileHome)) { Stop-WithError "profile does not exist: $profile" }
            Assert-Auth (Join-Path $profileHome 'auth.json') $profile
            Assert-ExpectedDomain $profileHome (Join-Path $profileHome 'auth.json')
            [void] (Prepare-Profile $profile); Write-ActiveMarker $profile
            Enable-CodexInCurrentPowerShell
            "Selected Codex profile: $profile"; break
        }
        'status' {
            $profile = if ($arguments.Count -eq 2) { $arguments[1] } else { $script:ActiveProfile }
            if (-not $profile) { Stop-WithError 'no active profile' }
            Assert-ProfileName $profile
            $profileHome = "$($script:ActiveHome)-$profile"
            Assert-Auth (Join-Path $profileHome 'auth.json') $profile
            Assert-ExpectedDomain $profileHome (Join-Path $profileHome 'auth.json')
            Invoke-Codex $profile @('login','status')
            exit $script:LastCodexExitCode
        }
        'run' {
            $rest = @($arguments | Select-Object -Skip 1)
            $explicitProfile = $false
            if ($rest.Count -gt 0 -and (Test-ValidProfileName $rest[0]) -and [IO.Directory]::Exists("$($script:ActiveHome)-$($rest[0])")) {
                $profile = $rest[0]; $rest = @($rest | Select-Object -Skip 1)
                $explicitProfile = $true
            } else { $profile = $script:ActiveProfile }
            if (-not $profile) { Stop-WithError 'run requires a profile when none is active' 64 }
            $profileHome = "$($script:ActiveHome)-$profile"
            Assert-Auth (Join-Path $profileHome 'auth.json') $profile
            Assert-ExpectedDomain $profileHome (Join-Path $profileHome 'auth.json')
            if ($rest.Count -gt 0 -and $rest[0] -eq '--with-hooks') {
                $env:CODEX_SWITCH_DISABLE_HOOKS = '0'
                $rest = @($rest | Select-Object -Skip 1)
            }
            if ($explicitProfile) { Write-ActiveMarker $profile }
            Invoke-Codex $profile $rest -NoDaemon -DisableHooks:($env:CODEX_SWITCH_DISABLE_HOOKS -eq '1')
            exit $script:LastCodexExitCode
        }
    }
}
catch { [Console]::Error.WriteLine('codex-switch: {0}', $_.Exception.Message); exit 1 }
