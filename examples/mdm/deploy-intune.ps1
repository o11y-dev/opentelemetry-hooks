# Run as Administrator / Intune SYSTEM. No credentials accepted as parameters.
param(
    [Parameter(Mandatory=$true)][ValidateSet('install','remediate','detect','rollback','uninstall')][string]$Action,
    [string]$Wheelhouse = '', [string]$LockFile = '', [string]$Version = '',
    [string]$Python = 'C:\Program Files\Python312\python.exe',
    [ValidateSet('claude','cursor')][string[]]$Agents = @('claude','cursor')
)
$ErrorActionPreference = 'Stop'
$Root = Join-Path $env:ProgramData 'OpenTelemetryHook'
$Launcher = Join-Path $Root 'bin\otel-hook.ps1'
$ActiveFile = Join-Path $Root 'active.json'
$PreviousFile = Join-Path $Root 'previous.json'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run as Administrator or SYSTEM'
}
function Invoke-Checked([string]$File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Deployment command failed; active runtime and user state retained' }
}
function Get-ActiveHook([string]$Path) {
    $record = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($record.directory -notmatch '^[A-Za-z0-9.+_-]+$') { throw 'Invalid deployment receipt' }
    $target = Join-Path (Join-Path $Root 'versions') $record.directory
    if (-not (Test-Path (Join-Path $target '.ready'))) { throw 'Incomplete runtime' }
    return (Join-Path $target 'Scripts\otel-hook.exe')
}
function Write-Receipt([string]$Path, [string]$Text) {
    $temp = "$Path.new"
    [IO.File]::WriteAllText($temp, $Text, (New-Object Text.UTF8Encoding $false))
    if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temp, $Path, $null) }
    else { [IO.File]::Move($temp, $Path) }
}
function Register-All([string]$Hook) {
    foreach ($agent in $Agents) { Invoke-Checked $Hook @('managed','register','--agent',$agent,'--scope','machine','--executable',$Launcher) }
}
function Detect-All {
    if (-not $Version) { throw 'Detection requires the expected Version' }
    $hook = Get-ActiveHook $ActiveFile
    foreach ($agent in $Agents) { Invoke-Checked $hook @('managed','status','--agent',$agent,'--scope','machine','--executable',$Launcher,'--expected-version',$Version) }
}
switch ($Action) {
    'detect' { Detect-All; exit 0 }
    'uninstall' {
        $hook = Get-ActiveHook $ActiveFile
        foreach ($agent in $Agents) { Invoke-Checked $hook @('managed','unregister','--agent',$agent,'--scope','machine') }
        Remove-Item -LiteralPath $ActiveFile,$Launcher -Force
        Write-Host 'Unregistered. Versioned runtimes, managed credentials and user queues retained for explicit cleanup.'
        exit 0
    }
    'rollback' {
        $hook = Get-ActiveHook $PreviousFile
        $current = Get-Content -LiteralPath $ActiveFile -Raw
        Write-Receipt $ActiveFile (Get-Content -LiteralPath $PreviousFile -Raw)
        Write-Receipt $PreviousFile $current
        Register-All $hook
        Write-Host 'Restore matching managed policy separately; user state retained.'
        exit 0
    }
}
if (-not (Test-Path -LiteralPath $Wheelhouse -PathType Container) -or -not (Test-Path -LiteralPath $LockFile -PathType Leaf) -or $Version -notmatch '^[A-Za-z0-9.+_-]+$') {
    throw 'Supply wheelhouse, requirements.lock and approved Version'
}
if (-not [IO.Path]::IsPathRooted($Python) -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Python must be an absolute installed interpreter path'
}
Invoke-Checked $Python @('-c','import sys; assert sys.version_info >= (3,12)')
$hash = (Get-FileHash -LiteralPath $LockFile -Algorithm SHA256).Hash.ToLower().Substring(0,12)
$directory = "$Version-$hash"
$target = Join-Path (Join-Path $Root 'versions') $directory
New-Item -ItemType Directory -Force (Join-Path $Root 'bin') | Out-Null
# SYSTEM/Admin own executable code; standard users receive read/execute only.
& icacls $Root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to protect runtime directory' }
$venvPython = Join-Path $target 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath (Join-Path $target '.ready'))) {
    Invoke-Checked $Python @('-m','venv',$target)
    Invoke-Checked $venvPython @('-m','pip','install','--no-index','--find-links',$Wheelhouse,'--require-hashes','-r',$LockFile)
    Invoke-Checked $venvPython @('-c',"import importlib.metadata,sys; from otel_hooks.bootstrap import runtime_has_otel_dependencies; assert importlib.metadata.version('opentelemetry-hooks') == sys.argv[1]; assert runtime_has_otel_dependencies()",$Version)
    New-Item -ItemType File -Force (Join-Path $target '.ready') | Out-Null
}
$launcherText = @'
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$HookArguments)
$ErrorActionPreference = 'Stop'
try {
    $root = Split-Path -Parent $PSScriptRoot
    $record = Get-Content -LiteralPath (Join-Path $root 'active.json') -Raw | ConvertFrom-Json
    if ($record.directory -notmatch '^[A-Za-z0-9.+_-]+$') { throw 'Invalid receipt' }
    $hook = Join-Path (Join-Path (Join-Path $root 'versions') $record.directory) 'Scripts\otel-hook.exe'
    if (Test-Path -LiteralPath $hook) { & $hook @HookArguments; exit $LASTEXITCODE }
} catch {}
[Console]::Out.WriteLine('{}')
exit 0
'@
Write-Receipt $Launcher $launcherText
$receipt = @{directory=$directory; version=$Version; lock_sha256=(Get-FileHash -LiteralPath $LockFile -Algorithm SHA256).Hash.ToLower()} | ConvertTo-Json -Compress
if (Test-Path -LiteralPath $ActiveFile) {
    $old = Get-Content -LiteralPath $ActiveFile -Raw
    if (($old | ConvertFrom-Json).directory -ne $directory) { Write-Receipt $PreviousFile $old }
}
Write-Receipt $ActiveFile $receipt
Register-All (Join-Path $target 'Scripts\otel-hook.exe')
Detect-All
