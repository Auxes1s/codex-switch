[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$commandPath = Join-Path $repoDirectory 'bin\codex-switch.ps1'
$installerPath = Join-Path $repoDirectory 'install.ps1'
$powerShellPath = (Get-Process -Id $PID).Path
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('codex-switch-tests-' + [Guid]::NewGuid().ToString('N'))
$utf8WithoutBom = New-Object Text.UTF8Encoding($false)

function Fail-Test {
    param([string] $Message)
    throw "not ok - $Message"
}

function Assert-True {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) { Fail-Test $Message }
}

function Assert-TextEquals {
    param([AllowEmptyString()][string] $Expected, [AllowEmptyString()][string] $Actual, [string] $Message)
    if ($Expected -cne $Actual) {
        Fail-Test "$Message (expected '$Expected', got '$Actual')"
    }
}

function Assert-FileEquals {
    param([string] $Expected, [string] $Actual, [string] $Message)
    $expectedBytes = [IO.File]::ReadAllBytes($Expected)
    $actualBytes = [IO.File]::ReadAllBytes($Actual)
    if ($expectedBytes.Length -ne $actualBytes.Length) { Fail-Test $Message }
    for ($index = 0; $index -lt $expectedBytes.Length; $index++) {
        if ($expectedBytes[$index] -ne $actualBytes[$index]) { Fail-Test $Message }
    }
}

function Write-TestFile {
    param([string] $Path, [string] $Value)
    [IO.File]::WriteAllText($Path, $Value + [Environment]::NewLine, $script:utf8WithoutBom)
}

function Read-FirstLine {
    param([string] $Path)
    return [IO.File]::ReadLines($Path) | Select-Object -First 1
}

function Quote-NativeArgument {
    param([string] $Value)
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-Process {
    param(
        [string] $FilePath,
        [string[]] $Arguments,
        [hashtable] $Environment = @{}
    )

    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (($Arguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    foreach ($key in $Environment.Keys) {
        if ($null -eq $Environment[$key]) {
            [void] $startInfo.EnvironmentVariables.Remove($key)
        }
        else {
            $startInfo.EnvironmentVariables[$key] = [string] $Environment[$key]
        }
    }
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    [void] $process.Start()
    $stdout = $process.StandardOutput.ReadToEnd().TrimEnd([char[]] @("`r", "`n"))
    $stderr = $process.StandardError.ReadToEnd().TrimEnd([char[]] @("`r", "`n"))
    $process.WaitForExit()
    return [pscustomobject] @{
        ExitCode = $process.ExitCode
        Stdout = $stdout
        Stderr = $stderr
    }
}

function Invoke-CodexSwitch {
    param([string] $ActiveHome, [string[]] $Arguments)
    return Invoke-Process $script:powerShellPath (@('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script:commandPath) + $Arguments) @{ CODEX_HOME = $ActiveHome }
}

function New-TestHome {
    param([string] $CaseName)

    $caseRoot = Join-Path $script:testRoot $CaseName
    $activeHome = Join-Path $caseRoot '.codex'
    foreach ($directory in @($activeHome, "$activeHome-personal", "$activeHome-work", "$activeHome-gig")) {
        [void] [IO.Directory]::CreateDirectory($directory)
    }
    Write-TestFile (Join-Path $activeHome 'auth.json') 'default-auth'
    Write-TestFile (Join-Path $activeHome 'config.toml') 'keep-config'
    Write-TestFile (Join-Path $activeHome 'history.jsonl') 'keep-history'
    Write-TestFile (Join-Path "$activeHome-personal" 'auth.json') 'personal-auth'
    Write-TestFile (Join-Path "$activeHome-work" 'auth.json') 'work-auth'
    Write-TestFile (Join-Path "$activeHome-gig" 'auth.json') 'gig-auth'
    return $activeHome
}

try {
    [void] [IO.Directory]::CreateDirectory($testRoot)

    $result = Invoke-CodexSwitch (Join-Path $testRoot 'unused-home') @('--help')
    Assert-TextEquals '0' ([string] $result.ExitCode) 'help failed'
    Assert-True ($result.Stdout -match '^Usage: codex-switch') 'help omitted usage text'

    $activeHome = New-TestHome 'persistent path with spaces'
    $result = Invoke-CodexSwitch $activeHome @('personal')
    Assert-TextEquals '0' ([string] $result.ExitCode) 'switch failed'
    Assert-TextEquals 'Active Codex profile: personal' $result.Stdout 'switch confirmation is incorrect'
    Assert-TextEquals 'personal-auth' (Read-FirstLine (Join-Path $activeHome 'auth.json')) 'personal credentials were not activated'
    Assert-TextEquals 'personal' (Read-FirstLine (Join-Path $activeHome '.codex-switch-active')) 'active marker is incorrect'
    Assert-TextEquals 'keep-config' (Read-FirstLine (Join-Path $activeHome 'config.toml')) 'config was changed'
    Assert-TextEquals 'keep-history' (Read-FirstLine (Join-Path $activeHome 'history.jsonl')) 'history was changed'

    Write-TestFile (Join-Path $activeHome 'auth.json') 'personal-refreshed'
    $result = Invoke-CodexSwitch $activeHome @('switch', 'work')
    Assert-TextEquals '0' ([string] $result.ExitCode) 'explicit switch failed'
    Assert-TextEquals 'personal-refreshed' (Read-FirstLine (Join-Path "$activeHome-personal" 'auth.json')) 'outgoing credentials were not synced'
    Assert-TextEquals 'work-auth' (Read-FirstLine (Join-Path $activeHome 'auth.json')) 'work credentials were not activated'

    Write-TestFile (Join-Path $activeHome 'auth.json') 'work-refreshed'
    $result = Invoke-CodexSwitch $activeHome @('WORK')
    Assert-TextEquals '0' ([string] $result.ExitCode) 'case-insensitive same-profile switch failed'
    Assert-TextEquals 'work-refreshed' (Read-FirstLine (Join-Path "$activeHome-work" 'auth.json')) 'same-profile credentials were not synced'

    $authAcl = Get-Acl -LiteralPath (Join-Path $activeHome 'auth.json')
    Assert-True $authAcl.AreAccessRulesProtected 'active credentials inherit ACL entries'
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $authRules = @($authAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
    Assert-True ($authRules.Count -ge 1) 'active credentials have no explicit ACL rule'
    foreach ($rule in $authRules) {
        Assert-True ($rule.IdentityReference -eq $currentSid) 'active credentials grant access to another identity'
        Assert-True ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) 'active credentials contain a deny rule'
    }
    $temporaryFiles = @(Get-ChildItem -LiteralPath $activeHome -Force | Where-Object { $_.Name -like '.*codex-switch*.tmp' })
    Assert-TextEquals '0' ([string] $temporaryFiles.Count) 'temporary files were left behind'

    Write-TestFile (Join-Path $activeHome 'auth.json') 'gig2-auth'
    $result = Invoke-CodexSwitch $activeHome @('add', 'gig2')
    Assert-TextEquals 'Added and activated Codex profile: gig2' $result.Stdout 'add confirmation is incorrect'
    Assert-TextEquals 'gig2-auth' (Read-FirstLine (Join-Path "$activeHome-gig2" 'auth.json')) 'add did not save active credentials'
    Assert-True ((Invoke-CodexSwitch $activeHome @('add', 'gig2')).ExitCode -ne 0) 'adding a duplicate profile succeeded'
    foreach ($invalidName in @('../escape', '_leading', 'bad name', 'ADD')) {
        Assert-True ((Invoke-CodexSwitch $activeHome @('add', $invalidName)).ExitCode -ne 0) "adding an invalid profile succeeded: $invalidName"
    }

    $result = Invoke-CodexSwitch $activeHome @('list')
    Assert-True ($result.Stdout -match '(?m)^gig2\t\(active\)$') 'list did not mark gig2 active'
    Assert-True ($result.Stdout -match '(?m)^personal$') 'list omitted a saved profile'

    Write-TestFile (Join-Path $activeHome 'auth.json') 'replacement-auth'
    $result = Invoke-CodexSwitch $activeHome @('auth', 'personal')
    Assert-TextEquals 'Updated auth for Codex profile: personal' $result.Stdout 'auth confirmation is incorrect'
    Assert-TextEquals 'replacement-auth' (Read-FirstLine (Join-Path "$activeHome-personal" 'auth.json')) 'auth did not update saved credentials'
    Assert-True ((Invoke-CodexSwitch $activeHome @('auth', 'unknown')).ExitCode -ne 0) 'updating an unknown profile succeeded'

    Write-TestFile (Join-Path "$activeHome-gig2" 'notes.txt') 'preserve-me'
    $result = Invoke-CodexSwitch $activeHome @('remove', 'gig2')
    Assert-TextEquals '0' ([string] $result.ExitCode) 'removing an inactive profile failed'
    Assert-True (-not [IO.File]::Exists((Join-Path "$activeHome-gig2" 'auth.json'))) 'remove retained profile credentials'
    Assert-True ([IO.File]::Exists((Join-Path "$activeHome-gig2" 'notes.txt')) ) 'remove deleted unrelated profile contents'
    Assert-True ((Invoke-CodexSwitch $activeHome @('remove', 'personal')).ExitCode -ne 0) 'removing the active profile succeeded'
    [void] [IO.Directory]::CreateDirectory("$activeHome-temporary")
    Write-TestFile (Join-Path "$activeHome-temporary" 'auth.json') 'temporary-auth'
    $result = Invoke-CodexSwitch $activeHome @('remove', 'temporary')
    Assert-True (-not [IO.Directory]::Exists("$activeHome-temporary")) 'remove retained an empty profile directory'

    $invalidHome = New-TestHome 'invalid'
    [IO.File]::Copy((Join-Path $invalidHome 'auth.json'), (Join-Path (Split-Path -Parent $invalidHome) 'original-auth'))
    $invalidArgumentSets = New-Object Collections.ArrayList
    [void] $invalidArgumentSets.Add([string[]] @())
    [void] $invalidArgumentSets.Add([string[]] @('unknown'))
    [void] $invalidArgumentSets.Add([string[]] @('personal', 'extra'))
    foreach ($invalidArguments in $invalidArgumentSets) {
        $result = Invoke-CodexSwitch $invalidHome ([string[]] $invalidArguments)
        Assert-True ($result.ExitCode -ne 0) 'invalid arguments succeeded'
        Assert-FileEquals (Join-Path (Split-Path -Parent $invalidHome) 'original-auth') (Join-Path $invalidHome 'auth.json') 'invalid arguments changed credentials'
    }
    Assert-TextEquals '64' ([string] (Invoke-CodexSwitch $invalidHome @()).ExitCode) 'missing arguments returned the wrong exit code'
    Assert-TextEquals '64' ([string] (Invoke-CodexSwitch $invalidHome @('add', '_invalid')).ExitCode) 'invalid profile returned the wrong exit code'

    $missingHome = New-TestHome 'missing'
    [IO.File]::Delete((Join-Path "$missingHome-work" 'auth.json'))
    $result = Invoke-CodexSwitch $missingHome @('work')
    Assert-True ($result.ExitCode -ne 0) 'missing target credentials succeeded'
    Assert-True (-not [IO.File]::Exists((Join-Path $missingHome '.codex-switch-active'))) 'missing target created an active marker'

    $lockedHome = New-TestHome 'locked-destination'
    Write-TestFile (Join-Path $lockedHome '.codex-switch-active') 'personal'
    $lockedAuth = Join-Path $lockedHome 'auth.json'
    $lockStream = [IO.File]::Open($lockedAuth, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $result = Invoke-CodexSwitch $lockedHome @('work')
        Assert-True ($result.ExitCode -ne 0) 'switching over a locked destination succeeded'
        Assert-TextEquals 'default-auth' (Read-FirstLine $lockedAuth) 'a failed atomic replacement changed active credentials'
        Assert-TextEquals 'personal' (Read-FirstLine (Join-Path $lockedHome '.codex-switch-active')) 'a failed atomic replacement changed the marker'
    }
    finally {
        $lockStream.Dispose()
    }
    $temporaryFiles = @(Get-ChildItem -LiteralPath $lockedHome -Force | Where-Object { $_.Name -like '.*codex-switch*.tmp' })
    Assert-TextEquals '0' ([string] $temporaryFiles.Count) 'a failed replacement left temporary files behind'

    $defaultRoot = Join-Path $testRoot 'default-user-profile'
    [void] [IO.Directory]::CreateDirectory((Join-Path $defaultRoot '.codex-personal'))
    Write-TestFile (Join-Path $defaultRoot '.codex-personal\auth.json') 'default-profile-auth'
    $result = Invoke-Process $powerShellPath @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $commandPath, 'personal') @{ CODEX_HOME = $null; USERPROFILE = $defaultRoot }
    Assert-TextEquals '0' ([string] $result.ExitCode) 'USERPROFILE default failed'
    Assert-TextEquals 'default-profile-auth' (Read-FirstLine (Join-Path $defaultRoot '.codex\auth.json')) 'USERPROFILE default selected the wrong Codex home'

    $installDirectory = Join-Path $testRoot 'installed path\bin'
    $result = Invoke-Process $powerShellPath @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installerPath, '-NoPathUpdate') @{ CODEX_SWITCH_INSTALL_DIR = $installDirectory }
    Assert-TextEquals '0' ([string] $result.ExitCode) 'installer failed'
    Assert-FileEquals $commandPath (Join-Path $installDirectory 'codex-switch.ps1') 'installer copied a different PowerShell command'
    Assert-FileEquals (Join-Path $repoDirectory 'bin\codex-switch.cmd') (Join-Path $installDirectory 'codex-switch.cmd') 'installer copied a different launcher'
    $result = Invoke-Process $powerShellPath @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installerPath, '-NoPathUpdate') @{ CODEX_SWITCH_INSTALL_DIR = $installDirectory }
    Assert-TextEquals '0' ([string] $result.ExitCode) 'repeat installation failed'
    $installerTemporaryFiles = @(Get-ChildItem -LiteralPath $installDirectory -Force | Where-Object { $_.Name -like '*.install.*.tmp' })
    Assert-TextEquals '0' ([string] $installerTemporaryFiles.Count) 'installer left temporary files behind'
    $result = Invoke-Process (Join-Path $installDirectory 'codex-switch.cmd') @('list') @{ CODEX_HOME = $activeHome }
    Assert-TextEquals '0' ([string] $result.ExitCode) '.cmd launcher failed'

    [Console]::Out.WriteLine('ok - all Windows codex-switch tests passed')
}
finally {
    if ([IO.Directory]::Exists($testRoot)) {
        [IO.Directory]::Delete($testRoot, $true)
    }
}
