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
    [IO.File]::WriteAllText($Path, $Text + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
}
function First-Line([string] $Path) { return [IO.File]::ReadLines($Path) | Select-Object -First 1 }
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

function Invoke-Switch([string] $Home, [string[]] $Arguments, [hashtable] $Extra = @{}) {
    $environment = @{ CODEX_HOME = $Home }
    foreach ($key in $Extra.Keys) { $environment[$key] = $Extra[$key] }
    return Invoke-Process $shell (@('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$command) + $Arguments) $environment
}

function New-Home([string] $Name) {
    $home = Join-Path $root "$Name\.codex"
    foreach ($directory in @($home,"$home-personal","$home-work")) { [void] [IO.Directory]::CreateDirectory($directory) }
    Write-Text (Join-Path $home 'auth.json') 'central-auth'
    Write-Text (Join-Path $home 'config.toml') 'config'
    Write-Text (Join-Path "$home-personal" 'auth.json') 'personal-auth'
    Write-Text (Join-Path "$home-work" 'auth.json') 'work-auth'
    return $home
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
if ($env:CODEX_SWITCH_WRITE_AUTH) { [IO.File]::WriteAllText((Join-Path $env:CODEX_HOME 'auth.json'), $env:CODEX_SWITCH_WRITE_AUTH + "`n", $encoding) }
'@ | Set-Content -LiteralPath $stub -Encoding UTF8

    $home = New-Home 'main path with spaces'
    $result = Invoke-Switch $home @('personal')
    Assert-Equal '0' ([string] $result.ExitCode) 'switch failed'
    Assert-Equal 'Selected Codex profile: personal' $result.Stdout 'switch confirmation is incorrect'
    Assert-Equal 'personal-auth' (First-Line (Join-Path "$home-personal" 'auth.json')) 'switch changed profile auth'
    Assert-Equal 'central-auth' (First-Line (Join-Path $home 'auth.json')) 'switch changed central auth'
    Assert-True ((Get-Item (Join-Path "$home-personal" 'sessions')).Attributes -band [IO.FileAttributes]::ReparsePoint) 'sessions are not shared by junction'

    $capture = Join-Path $root 'capture'
    $result = Invoke-Switch $home @('run','work','exec','task') @{
        CODEX_SWITCH_CODEX_BIN = $stub; CODEX_SWITCH_CAPTURE = $capture; CODEX_SWITCH_WRITE_AUTH = 'work-refreshed'
    }
    Assert-Equal '0' ([string] $result.ExitCode) 'run failed'
    Assert-Equal "$home-work" (First-Line "$capture.home") 'run used wrong CODEX_HOME'
    Assert-Equal $home (First-Line "$capture.sqlite-home") 'run used wrong CODEX_SQLITE_HOME'
    Assert-Equal 'work-refreshed' (First-Line (Join-Path "$home-work" 'auth.json')) 'selected profile was not refreshed'
    Assert-Equal 'personal-auth' (First-Line (Join-Path "$home-personal" 'auth.json')) 'run changed another profile'
    Assert-Equal 'central-auth' (First-Line (Join-Path $home 'auth.json')) 'run changed central auth'

    $result = Invoke-Switch $home @('add','new')
    Assert-True ($result.Stdout -match 'Added and selected Codex profile: new') 'add confirmation is incorrect'
    Assert-True (-not [IO.File]::Exists((Join-Path "$home-new" 'auth.json'))) 'add copied credentials'
    Assert-True ((Invoke-Switch $home @('add','new')).ExitCode -ne 0) 'duplicate add succeeded'
    Assert-True ((Invoke-Switch $home @('add','../bad')).ExitCode -ne 0) 'invalid profile succeeded'

    $list = Invoke-Switch $home @('list')
    Assert-True ($list.Stdout -match '(?m)^new\t\(signed out\)\t\(active\)$') 'list omitted active signed-out profile'
    Assert-Equal 'new' (Invoke-Switch $home @('current')).Stdout 'current returned wrong profile'
    Assert-True ((Invoke-Switch $home @('remove','new')).ExitCode -ne 0) 'active profile removal succeeded'

    $legacy = New-Home 'legacy'
    Write-Text (Join-Path $legacy '.codex-switch-active') 'personal'
    Write-Text (Join-Path $legacy 'auth.json') 'personal-refreshed-in-old-central-home'
    $result = Invoke-Switch $legacy @('work')
    Assert-Equal 'personal-refreshed-in-old-central-home' (First-Line (Join-Path "$legacy-personal" 'auth.json')) 'legacy active auth was not retained'
    Assert-Equal 'work-auth' (First-Line (Join-Path "$legacy-work" 'auth.json')) 'legacy migration changed target auth'
    Assert-True ([IO.File]::Exists((Join-Path $legacy '.codex-switch-auth-isolation-v1'))) 'legacy auth migration marker missing'

    $install = Join-Path $root 'installed\bin'
    $result = Invoke-Process $shell @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$installer,'-NoPathUpdate') @{ CODEX_SWITCH_INSTALL_DIR = $install }
    Assert-Equal '0' ([string] $result.ExitCode) 'installer failed'
    foreach ($name in @('codex-switch.ps1','codex-switch.cmd','codex-switch-migrate.py')) {
        Assert-True ([IO.File]::Exists((Join-Path $install $name))) "installer omitted $name"
    }
    [Console]::Out.WriteLine('ok - all Windows codex-switch tests passed')
}
finally { if ([IO.Directory]::Exists($root)) { [IO.Directory]::Delete($root, $true) } }
