[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$command = Join-Path $repo 'bin\codex-switch.ps1'
$installer = Join-Path $repo 'install.ps1'
$shell = (Get-Process -Id $PID).Path
$root = Join-Path ([IO.Path]::GetTempPath()) ('codex-switch-tests-' + [Guid]::NewGuid().ToString('N'))

function Assert-True([bool] $Condition, [string] $Message) { if (-not $Condition) { throw "not ok - $Message" } }
function Assert-Equal([string] $Expected, [string] $Actual, [string] $Message) {
    if ($Expected -cne $Actual) { throw "not ok - $Message (expected '$Expected', got '$Actual')" }
}
function Write-Text([string] $Path, [string] $Text) {
    if ((Split-Path -Leaf $Path) -eq 'auth.json') {
        $subject = if ($Text -match '^personal') { 'personal-user' } elseif ($Text -match '^work') { 'work-user' } else { 'central-user' }
        $domain = if ($subject -eq 'work-user') { 'ahrinavi.online' } else { 'gmail.com' }
        $payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('{"sub":"' + $subject + '","email":"test@' + $domain + '"}')).TrimEnd('=').Replace('+','-').Replace('/','_')
        $stamp = if ($Text -match 'refreshed') { '2026-09-29T00:00:00Z' } else { '2026-09-20T00:00:00Z' }
        $Text = @{ auth_mode='chatgpt'; tokens=@{ id_token='x.'+$payload+'.x'; refresh_token=$Text; access_token='test'; account_id='test' }; last_refresh=$stamp } | ConvertTo-Json -Compress -Depth 5
    }
    [IO.File]::WriteAllText($Path, $Text + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
}
function First-Line([string] $Path) {
    $reader = [IO.File]::OpenText($Path)
    try { $line = $reader.ReadLine() }
    finally { $reader.Dispose() }
    if ((Split-Path -Leaf $Path) -eq 'auth.json') { return ($line | ConvertFrom-Json).tokens.refresh_token }
    return $line
}
function Quote-Argument([string] $Value) { if ($Value -notmatch '[\s"]') { return $Value }; return '"' + $Value.Replace('"','\"') + '"' }

function Invoke-Process([string] $File, [string[]] $Arguments, [hashtable] $Environment = @{}) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $File
    $start.Arguments = (($Arguments | ForEach-Object { Quote-Argument $_ }) -join ' ')
    $start.UseShellExecute = $false; $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    foreach ($key in $Environment.Keys) {
        if ($null -eq $Environment[$key]) { [void] $start.EnvironmentVariables.Remove($key) }
        else { $start.EnvironmentVariables[$key] = [string] $Environment[$key] }
    }
    $process = New-Object Diagnostics.Process; $process.StartInfo = $start; [void] $process.Start()
    $stdout = $process.StandardOutput.ReadToEnd().TrimEnd([char[]] @("`r","`n"))
    $stderr = $process.StandardError.ReadToEnd().TrimEnd([char[]] @("`r","`n"))
    $process.WaitForExit()
    return [pscustomobject] @{ ExitCode = $process.ExitCode; Stdout = $stdout; Stderr = $stderr }
}

function Invoke-Switch([string] $TestHomeArg, [string[]] $Arguments, [hashtable] $Extra = @{}) {
    $environment = @{ CODEX_HOME = $TestHomeArg; LOCALAPPDATA = (Join-Path $root 'appdata') }
    foreach ($key in $Extra.Keys) { $environment[$key] = $Extra[$key] }
    return Invoke-Process $shell (@('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$command) + $Arguments) $environment
}

function New-Home([string] $Name) {
    $testHome = Join-Path $root "$Name\.codex"
    foreach ($directory in @($testHome,"$testHome-personal","$testHome-work")) { [void] [IO.Directory]::CreateDirectory($directory) }
    Write-Text (Join-Path $testHome 'auth.json') 'central-auth'
    Write-Text (Join-Path $testHome 'config.toml') 'config'
    Write-Text (Join-Path "$testHome-personal" 'auth.json') 'personal-auth'
    Write-Text (Join-Path "$testHome-work" 'auth.json') 'work-auth'
    return $testHome
}

try {
    [void] [IO.Directory]::CreateDirectory($root)
    $stub = Join-Path $root 'codex-stub.ps1'
    @'
param([Parameter(ValueFromRemainingArguments=$true)][string[]] $Arguments)
$encoding = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText(($env:CODEX_SWITCH_CAPTURE + '.home'), $env:CODEX_HOME + [Environment]::NewLine, $encoding)
[IO.File]::WriteAllText(($env:CODEX_SWITCH_CAPTURE + '.sqlite-home'), $env:CODEX_SQLITE_HOME + [Environment]::NewLine, $encoding)
[IO.File]::WriteAllText(($env:CODEX_SWITCH_CAPTURE + '.args'), ($Arguments -join "`n") + "`n", $encoding)
[IO.File]::WriteAllText(($env:CODEX_SWITCH_CAPTURE + '.wif'), [string]([Environment]::GetEnvironmentVariables().Contains('OPENAI_FEDERATION_RULE_ID')), $encoding)
if ($Arguments -contains 'login' -and $env:CODEX_SWITCH_LOGIN_DOMAIN) {
    $domain = $env:CODEX_SWITCH_LOGIN_DOMAIN
    $payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('{"sub":"new-work-user","email":"test@' + $domain + '"}')).TrimEnd('=').Replace('+','-').Replace('/','_')
    $auth = @{ auth_mode='chatgpt'; tokens=@{ id_token='x.'+$payload+'.x'; refresh_token='new-work-auth'; access_token='test'; account_id='test' }; last_refresh='2026-09-29T00:00:00Z' }
    [IO.File]::WriteAllText((Join-Path $env:CODEX_HOME 'auth.json'), ($auth | ConvertTo-Json -Compress -Depth 5) + "`n", $encoding)
}
if ($env:CODEX_SWITCH_WRITE_AUTH) {
    $path = Join-Path $env:CODEX_HOME 'auth.json'
    $auth = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $auth.tokens.refresh_token = $env:CODEX_SWITCH_WRITE_AUTH
    [IO.File]::WriteAllText($path, ($auth | ConvertTo-Json -Compress -Depth 5) + "`n", $encoding)
}
'@ | Set-Content -LiteralPath $stub -Encoding UTF8

    $testHome = New-Home 'main path with spaces'
    $result = Invoke-Switch $testHome @('personal')
    Assert-Equal '0' ([string] $result.ExitCode) 'switch failed'
    Assert-Equal 'Selected Codex profile: personal' $result.Stdout 'switch confirmation is incorrect'
    Assert-Equal 'personal-auth' (First-Line (Join-Path "$testHome-personal" 'auth.json')) 'switch changed profile auth'
    Assert-Equal 'central-auth' (First-Line (Join-Path $testHome 'auth.json')) 'switch changed central auth'
    Assert-True ((Get-Item (Join-Path "$testHome-personal" 'sessions')).Attributes -band [IO.FileAttributes]::ReparsePoint) 'sessions are not shared by junction'

    $sameShell = Join-Path $root 'same-shell.ps1'
    @'
param([string] $SwitchCommand, [string] $TestCodexHome, [string] $Stub, [string] $Capture)
$env:CODEX_HOME = $TestCodexHome
$env:CODEX_SWITCH_CODEX_BIN = $Stub
$env:CODEX_SWITCH_CAPTURE = $Capture
& $SwitchCommand personal
if ((Get-Command codex).CommandType -ne 'Function') { throw 'switch did not install a codex function in the current PowerShell process' }
codex --version
'@ | Set-Content -LiteralPath $sameShell -Encoding UTF8
    $sameShellCapture = Join-Path $root 'same-shell-capture'
    $result = Invoke-Process $shell @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$sameShell,$command,$testHome,$stub,$sameShellCapture) @{
        LOCALAPPDATA = (Join-Path $root 'appdata')
    }
    Assert-Equal '0' ([string] $result.ExitCode) "same-shell switch failed: $($result.Stderr)"
    Assert-Equal "$testHome-personal" (First-Line "$sameShellCapture.home") 'same-shell codex bypassed selected profile'
    Assert-True ((Get-Content -LiteralPath "$sameShellCapture.args") -contains '--no-daemon') 'same-shell codex bypassed daemon flag'
    $shellInit = Invoke-Switch $testHome @('shell-init')
    Assert-True ($shellInit.ExitCode -eq 0 -and $shellInit.Stdout -match 'codex-switch\.ps1' -and $shellInit.Stdout -notmatch 'codex-switch\.cmd') 'PowerShell shell integration still uses the cmd launcher'

    $externalBin = Join-Path $root 'external-bin'
    [void] [IO.Directory]::CreateDirectory($externalBin)
    [IO.File]::Copy($stub, (Join-Path $externalBin 'codex.ps1'))
    $directShell = Join-Path $root 'direct-shell.ps1'
    @'
param([string] $SwitchCommand, [string] $TestCodexHome, [string] $ExternalBin, [string] $Capture)
$env:CODEX_HOME = $TestCodexHome
$env:CODEX_SWITCH_CAPTURE = $Capture
$env:PATH = $ExternalBin + ';' + $env:PATH
Remove-Item Env:CODEX_SWITCH_CODEX_BIN -ErrorAction SilentlyContinue
function global:codex { throw 'selected shell function instead of external Codex executable' }
& $SwitchCommand run personal --version
'@ | Set-Content -LiteralPath $directShell -Encoding UTF8
    $externalCapture = Join-Path $root 'external-capture'
    $result = Invoke-Process $shell @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$directShell,$command,$testHome,$externalBin,$externalCapture)
    Assert-Equal '0' ([string] $result.ExitCode) "direct PowerShell run selected a function: $($result.Stderr)"
    Assert-Equal "$testHome-personal" (First-Line "$externalCapture.home") 'direct PowerShell run used the wrong Codex home'

    $capture = Join-Path $root 'capture'
    $result = Invoke-Switch $testHome @('run','work','exec','task') @{
        CODEX_SWITCH_CODEX_BIN = $stub; CODEX_SWITCH_CAPTURE = $capture; CODEX_SWITCH_WRITE_AUTH = 'work-refreshed'
    }
    Assert-Equal '0' ([string] $result.ExitCode) "run failed: $($result.Stderr)"
    Assert-Equal "$testHome-work" (First-Line "$capture.home") 'run used wrong CODEX_HOME'
    Assert-Equal $testHome (First-Line "$capture.sqlite-home") 'run used wrong CODEX_SQLITE_HOME'
    Assert-Equal 'work-refreshed' (First-Line (Join-Path "$testHome-work" 'auth.json')) 'selected profile was not refreshed'
    Assert-True ((Get-Content -LiteralPath "$capture.args") -contains '--no-daemon') 'run did not disable the shared daemon'
    Assert-Equal 'False' ([IO.File]::ReadAllText("$capture.wif")) 'run passed workload-identity environment variables'
    Assert-True ((Get-Acl -LiteralPath (Join-Path "$testHome-work" 'auth.json')).AreAccessRulesProtected) 'refreshed credentials retained inherited ACLs'
    Assert-Equal 'personal-auth' (First-Line (Join-Path "$testHome-personal" 'auth.json')) 'run changed another profile'
    Assert-Equal 'central-auth' (First-Line (Join-Path $testHome 'auth.json')) 'run changed central auth'

    $result = Invoke-Switch $testHome @('add','new')
    Assert-True ($result.Stdout -match 'Added and selected Codex profile: new') "add confirmation is incorrect: $($result.Stdout) $($result.Stderr)"
    Assert-True (-not [IO.File]::Exists((Join-Path "$testHome-new" 'auth.json'))) 'add copied credentials'
    Assert-True ((Invoke-Switch $testHome @('add','new')).ExitCode -ne 0) 'duplicate add succeeded'
    Assert-True ((Invoke-Switch $testHome @('add','../bad')).ExitCode -ne 0) 'invalid profile succeeded'
    Assert-True ((Invoke-Switch $testHome @('add','work.')).ExitCode -ne 0) 'trailing-dot profile name aliased an existing profile'

    $list = Invoke-Switch $testHome @('list')
    Assert-True ($list.Stdout -match '(?m)^new\t\(signed out\)\t\(active\)\r?$') "list omitted active signed-out profile: $($list.Stdout) $($list.Stderr)"
    Assert-Equal 'new' (Invoke-Switch $testHome @('current')).Stdout 'current returned wrong profile'
    Assert-True ((Invoke-Switch $testHome @('remove','new')).ExitCode -ne 0) 'active profile removal succeeded'

    $domainHome = New-Home 'domain-guard'
    Write-Text (Join-Path "$domainHome-work" '.codex-switch-expected-domain') 'ahrinavi.online'
    Write-Text (Join-Path "$domainHome-work" 'auth.json') 'personal-auth'
    $wrongHash = (Get-FileHash -LiteralPath (Join-Path "$domainHome-work" 'auth.json')).Hash
    $result = Invoke-Switch $domainHome @('work')
    Assert-True ($result.ExitCode -ne 0 -and $result.Stderr -match 'different account domain') 'switch accepted a wrong-domain work login'
    Assert-Equal $wrongHash (Get-FileHash -LiteralPath (Join-Path "$domainHome-work" 'auth.json')).Hash 'wrong-domain check changed credentials'
    Assert-True (-not [IO.File]::Exists((Join-Path $domainHome '.codex-switch-active'))) 'wrong-domain check changed selection'

    $loginHome = New-Home 'staged-login'
    [IO.File]::Delete((Join-Path "$loginHome-work" 'auth.json'))
    $result = Invoke-Switch $loginHome @('login','work','--expect-domain','ahrinavi.online') @{
        CODEX_SWITCH_CODEX_BIN = $stub; CODEX_SWITCH_CAPTURE = (Join-Path $root 'login-capture'); CODEX_SWITCH_LOGIN_DOMAIN = 'gmail.com'
    }
    Assert-True ($result.ExitCode -ne 0 -and $result.Stderr -match 'different account domain') "staged login accepted wrong account: $($result.ExitCode) $($result.Stdout) $($result.Stderr)"
    Assert-True (-not [IO.File]::Exists((Join-Path "$loginHome-work" 'auth.json'))) 'wrong staged login installed credentials'
    $result = Invoke-Switch $loginHome @('login','work','--expect-domain','ahrinavi.online') @{
        CODEX_SWITCH_CODEX_BIN = $stub; CODEX_SWITCH_CAPTURE = (Join-Path $root 'login-capture'); CODEX_SWITCH_LOGIN_DOMAIN = 'ahrinavi.online'
    }
    Assert-Equal '0' ([string] $result.ExitCode) "correct staged login failed: $($result.Stderr)"
    Assert-Equal 'new-work-auth' (First-Line (Join-Path "$loginHome-work" 'auth.json')) 'staged login did not publish credentials'
    Assert-Equal 'ahrinavi.online' (First-Line (Join-Path "$loginHome-work" '.codex-switch-expected-domain')) 'expected domain was not saved'
    Assert-Equal 'work' (First-Line (Join-Path $loginHome '.codex-switch-active')) 'staged login did not select work'

    $legacy = New-Home 'legacy'
    Write-Text (Join-Path $legacy '.codex-switch-active') 'personal'
    Write-Text (Join-Path $legacy 'auth.json') 'personal-refreshed-in-old-central-home'
    $result = Invoke-Switch $legacy @('work')
    Assert-Equal 'personal-refreshed-in-old-central-home' (First-Line (Join-Path "$legacy-personal" 'auth.json')) 'legacy active auth was not retained'
    Assert-Equal 'work-auth' (First-Line (Join-Path "$legacy-work" 'auth.json')) 'legacy migration changed target auth'
    Assert-True ([IO.File]::Exists((Join-Path $legacy '.codex-switch-auth-isolation-v1'))) 'legacy auth migration marker missing'

    $unassigned = New-Home 'unassigned-legacy'
    Write-Text (Join-Path $unassigned '.codex-switch-active') 'personal'
    [IO.File]::Delete((Join-Path "$unassigned-personal" 'auth.json'))
    $result = Invoke-Switch $unassigned @('work')
    Assert-Equal '0' ([string] $result.ExitCode) "unassigned legacy migration blocked switching: $($result.Stderr)"
    Assert-True (-not [IO.File]::Exists((Join-Path "$unassigned-personal" 'auth.json'))) 'legacy migration assigned central credentials without proof'
    $recoveredAuth = Get-Content -LiteralPath (Join-Path $unassigned '.codex-switch-legacy-state\unassigned\auth.json.pre-isolation') -Raw | ConvertFrom-Json
    Assert-Equal 'central-auth' $recoveredAuth.tokens.refresh_token 'unassigned legacy login was not retained'

    $aliasHome = New-Home 'legacy-alias-marker'
    Write-Text (Join-Path $aliasHome '.codex-switch-active') 'work.'
    Write-Text (Join-Path $aliasHome '.codex-switch-auth-isolation-v1') 'complete'
    $result = Invoke-Switch $aliasHome @('remove','work')
    Assert-True ($result.ExitCode -ne 0 -and $result.Stderr -match 'cannot remove active profile') 'legacy trailing-dot marker allowed removal of the active profile'
    Assert-Equal 'work-auth' (First-Line (Join-Path "$aliasHome-work" 'auth.json')) 'alias check deleted active credentials'

    $emptyMarkerHome = New-Home 'empty-marker'
    Write-Text (Join-Path $emptyMarkerHome '.codex-switch-active') ''
    Assert-Equal '0' ([string] (Invoke-Switch $emptyMarkerHome @('list')).ExitCode) 'empty active marker prevented listing profiles'

    $rollbackHome = New-Home 'login-rollback'
    Write-Text (Join-Path $rollbackHome '.codex-switch-active') 'personal'
    Write-Text (Join-Path $rollbackHome '.codex-switch-auth-isolation-v1') 'complete'
    Write-Text (Join-Path "$rollbackHome-work" '.codex-switch-expected-domain') 'gmail.com'
    $lockedMarker = Join-Path $rollbackHome '.codex-switch-active'
    $markerStream = [IO.File]::Open($lockedMarker, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $result = Invoke-Switch $rollbackHome @('login','work','--expect-domain','ahrinavi.online') @{
            CODEX_SWITCH_CODEX_BIN = $stub; CODEX_SWITCH_CAPTURE = (Join-Path $root 'rollback-capture'); CODEX_SWITCH_LOGIN_DOMAIN = 'ahrinavi.online'
        }
        Assert-True ($result.ExitCode -ne 0) 'login succeeded despite a locked active marker'
    }
    finally { $markerStream.Dispose() }
    Assert-Equal 'work-auth' (First-Line (Join-Path "$rollbackHome-work" 'auth.json')) 'failed login replaced existing credentials'
    Assert-Equal 'gmail.com' (First-Line (Join-Path "$rollbackHome-work" '.codex-switch-expected-domain')) 'failed login changed the domain guard'
    Assert-Equal 'personal' (First-Line $lockedMarker) 'failed login changed selection'

    $install = Join-Path $root 'installed\bin'
    $result = Invoke-Process $shell @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$installer,'-NoPathUpdate') @{ CODEX_SWITCH_INSTALL_DIR = $install }
    Assert-Equal '0' ([string] $result.ExitCode) 'installer failed'
    foreach ($name in @('codex-switch.ps1','codex-switch.cmd','codex-switch-migrate.py')) {
        Assert-True ([IO.File]::Exists((Join-Path $install $name))) "installer omitted $name"
    }
    $installedCommand = Join-Path $install 'codex-switch.cmd'
    $result = Invoke-Process $installedCommand @('list') @{ CODEX_HOME = $testHome }
    Assert-Equal '0' ([string] $result.ExitCode) "installed launcher failed: $($result.Stderr)"
    Assert-True ($result.Stdout -match '(?m)^personal\r?$') 'installed launcher omitted a profile'
    $wrapperCapture = Join-Path $root 'wrapper-capture'
    $result = Invoke-Process $installedCommand @('run','work','exec','text! with spaces') @{
        CODEX_HOME = $testHome; CODEX_SWITCH_CODEX_BIN = $stub; CODEX_SWITCH_CAPTURE = $wrapperCapture
    }
    Assert-Equal '0' ([string] $result.ExitCode) "installed launcher did not forward arguments: $($result.Stderr)"
    Assert-True ((Get-Content -LiteralPath "$wrapperCapture.args") -contains 'text! with spaces') 'cmd launcher changed an argument containing spaces or an exclamation mark'
    $result = Invoke-Process $shell @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$installer,'-NoPathUpdate') @{ CODEX_SWITCH_INSTALL_DIR = $install }
    Assert-Equal '0' ([string] $result.ExitCode) 'repeat installation failed'
    Assert-True (@(Get-ChildItem -LiteralPath $install -Force | Where-Object { $_.Name -like '*.install.*.tmp' }).Count -eq 0) 'installer left temporary files after repeat installation'
    [Console]::Out.WriteLine('ok - all Windows codex-switch tests passed')
}
finally {
    if ([IO.Directory]::Exists($root)) {
        try {
            $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char[]] @('\','/')) + [IO.Path]::DirectorySeparatorChar
            $resolved = [IO.Path]::GetFullPath($root)
            if (-not $resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or (Split-Path -Leaf $resolved) -notmatch '^codex-switch-tests-[0-9a-f]{32}$') { throw 'unsafe test cleanup target' }
            Get-ChildItem -LiteralPath $resolved -Recurse -Force -Attributes ReparsePoint | Sort-Object FullName -Descending | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
            [IO.Directory]::Delete($resolved, $true)
        }
        catch { [Console]::Error.WriteLine('test cleanup deferred: {0}', $root) }
    }
}
