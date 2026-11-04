[CmdletBinding()]
param(
    [string]$Root = '',
    [switch]$Apply,
    [string]$OutputDirectory = '',
    [ValidateSet('Password','S4U')][string]$LogonMode = 'Password'
)
$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Root = (Resolve-Path -LiteralPath $Root).Path
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $Root ('data\deployment\' + (Get-Date -Format 'yyyyMMdd_HHmmss')) }
[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
$resultPath = Join-Path $OutputDirectory 'result.json'
function Save-Result($Value) {
    [IO.File]::WriteAllText($resultPath, ($Value | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
}
function Escape([string]$Text) { return [Security.SecurityElement]::Escape($Text) }
$schedulerName = 'SkyMiku Stock Daily Scheduler'
$modelName = 'SkyMiku Codex LLM App Server'
$watchdogName = 'SkyMiku Stock Watchdog'
$old = @{}
foreach ($name in @($schedulerName,$modelName,$watchdogName)) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        $old[$name] = Export-ScheduledTask -TaskName $name
        [IO.File]::WriteAllText((Join-Path $OutputDirectory ($name + '.before.xml')), $old[$name], (New-Object Text.UTF8Encoding($false)))
    }
}
if (-not $old.ContainsKey($schedulerName) -or -not $old.ContainsKey($modelName)) { throw 'The two original service tasks must exist.' }
if ($Apply -and $LogonMode -eq 'S4U') {
    foreach ($entry in $old.Values) {
        [xml]$prior = $entry
        if ($prior.Task.Principals.Principal.LogonType -eq 'Password') {
            throw 'Cannot safely roll back stored Password credentials during an S4U conversion. Keep the existing mode or provide a credentialed rollback separately.'
        }
    }
}
[xml]$original = $old[$schedulerName]
$account = $original.Task.Principals.Principal.UserId
if ($account -match '^S-1-') {
    $sid = New-Object Security.Principal.SecurityIdentifier($account)
    $account = $sid.Translate([Security.Principal.NTAccount]).Value
} elseif ($account -notmatch '\\') { $account = "$env:COMPUTERNAME\$account" }
$python = Join-Path $Root '.venv\Scripts\python.exe'
$powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Existing uv environment is missing.' }
$plans = @(
    @{name=$modelName;command=$powershell;arguments=('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $Root 'scripts\start_codex_app_server.ps1') + '" -Root "' + $Root + '"');limit='PT0S'},
    @{name=$schedulerName;command=$python;arguments=('-m bot.scheduler --root "' + $Root + '"');limit='PT0S'},
    @{name=$watchdogName;command=$python;arguments=('-m bot.scheduler_watchdog --root "' + $Root + '"');limit='PT1M'}
)
$start = (Get-Date).AddMinutes(2).ToString('s')
foreach ($plan in $plans) {
    $repeat = ''
    if ($plan.name -eq $watchdogName) { $repeat = "<TimeTrigger><Repetition><Interval>PT2M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$start</StartBoundary><Enabled>true</Enabled></TimeTrigger>" }
    $plan.xml = @"
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>Stock research background services; recovery owned by independent watchdog.</Description></RegistrationInfo>
<Triggers><BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger>$repeat</Triggers>
<Principals><Principal id="Author"><UserId>$(Escape $account)</UserId><LogonType>$LogonMode</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>true</Hidden><WakeToRun>false</WakeToRun><ExecutionTimeLimit>$($plan.limit)</ExecutionTimeLimit><Priority>7</Priority></Settings>
<Actions Context="Author"><Exec><Command>$(Escape $plan.command)</Command><Arguments>$(Escape $plan.arguments)</Arguments><WorkingDirectory>$(Escape $Root)</WorkingDirectory></Exec></Actions>
</Task>
"@
    [IO.File]::WriteAllText((Join-Path $OutputDirectory ($plan.name + '.proposed.xml')), $plan.xml, (New-Object Text.UTF8Encoding($false)))
}
$credentialStorage = if ($LogonMode -eq 'S4U') {'No password stored'} else {'Windows Task Scheduler only'}
$result = @{status='preview';account=$account;root=$Root;directory=$OutputDirectory;tasks=@($plans | ForEach-Object {$_.name});logon_type=$LogonMode;credentials_saved_by=$credentialStorage}
Save-Result $result
if (-not $Apply) { $result | ConvertTo-Json -Depth 5; exit 0 }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $result.status='requires_elevation';Save-Result $result;throw 'Run this installer elevated to configure boot triggers and the operational log.'
}
$passwordBuffer = New-Object Text.StringBuilder(512)
if ($LogonMode -eq 'Password') {
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class StockCredentialPrompt {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct Info {
        public int size; public IntPtr parent; public string message; public string caption; public IntPtr banner;
    }
    [DllImport("credui.dll", CharSet=CharSet.Unicode)] public static extern int CredUIPromptForCredentialsW(
        ref Info info, string target, IntPtr reserved, int error, StringBuilder user, int userMax,
        StringBuilder password, int passwordMax, ref bool save, int flags);
}
'@
$info = New-Object StockCredentialPrompt+Info
$info.size = [Runtime.InteropServices.Marshal]::SizeOf($info)
$info.message = 'Enter the Windows account password (not PIN) for background stock research. It is passed only to Windows Task Scheduler.'
$info.caption = 'Stock research background tasks'
$userBuffer = New-Object Text.StringBuilder($account, 512)
$save = $false
$result.status='awaiting_local_credentials';Save-Result $result
$answer = [StockCredentialPrompt]::CredUIPromptForCredentialsW([ref]$info, 'Stock Research Windows Tasks', [IntPtr]::Zero, 0, $userBuffer, 512, $passwordBuffer, 512, [ref]$save, (0x40000 -bor 0x80 -bor 0x2))
if ($answer -ne 0) { $result.status='credentials_cancelled';$result.native_result=$answer;Save-Result $result;exit 2 }
}
$registered = @()
try {
    if ($LogonMode -eq 'Password') {
    $entered = New-Object Security.Principal.NTAccount($userBuffer.ToString())
    $expected = New-Object Security.Principal.NTAccount($account)
    if ($entered.Translate([Security.Principal.SecurityIdentifier]).Value -ne $expected.Translate([Security.Principal.SecurityIdentifier]).Value) { throw 'The account must match the existing ASUS service account.' }
    }
    $maintenance = Join-Path $Root 'data\watchdog_maintenance.json'
    if (-not (Test-Path -LiteralPath $maintenance)) {
        [IO.File]::WriteAllText($maintenance, '{"paused":true,"reason":"deployment in progress"}', (New-Object Text.UTF8Encoding($false)))
    }
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $folder = $service.GetFolder('\')
    $logonType = if ($LogonMode -eq 'S4U') {2} else {1}
    $credential = if ($LogonMode -eq 'S4U') {$null} else {$passwordBuffer.ToString()}
    foreach ($plan in $plans) {
        $null = $folder.RegisterTask($plan.name, $plan.xml, 6, $account, $credential, $logonType, $null)
        $registered += $plan.name
    }
    & wevtutil.exe sl Microsoft-Windows-TaskScheduler/Operational /e:true
    if ($LASTEXITCODE -ne 0) { throw 'Could not enable Task Scheduler operational history.' }
    foreach ($plan in $plans) {
        [xml]$actual = Export-ScheduledTask -TaskName $plan.name
        if ($actual.Task.Principals.Principal.LogonType -ne $LogonMode -or $actual.Task.Settings.MultipleInstancesPolicy -ne 'IgnoreNew' -or $actual.Task.Settings.RestartOnFailure) { throw 'Task definition verification failed.' }
        [IO.File]::WriteAllText((Join-Path $OutputDirectory ($plan.name + '.installed.xml')), $actual.OuterXml, (New-Object Text.UTF8Encoding($false)))
    }
    $result.status='installed';$result.finished_at=[DateTimeOffset]::Now.ToString('o');Save-Result $result
} catch {
    $result.status='failed';$result.error=$_.Exception.Message;$result.rollback_errors=@()
    foreach ($name in $registered) {
        try {
            if ($old.ContainsKey($name)) {
                [xml]$before=$old[$name]
                $kind = switch ($before.Task.Principals.Principal.LogonType) {
                    'Password' {1}
                    'S4U' {2}
                    'InteractiveToken' {3}
                    default {throw 'Unsupported original logon type during rollback.'}
                }
                $secret = if ($kind -eq 1) {$passwordBuffer.ToString()} else {$null}
                $null=$folder.RegisterTask($name,$old[$name],6,$account,$secret,$kind,$null)
                $secret=$null
            } else { $folder.DeleteTask($name,0) }
        } catch { $result.rollback_errors += $_.Exception.Message }
    }
    Save-Result $result
    exit 2
} finally {
    $credential = $null
    for($i=0;$i -lt $passwordBuffer.Length;$i++){ $passwordBuffer[$i]=[char]0 }
    $passwordBuffer.Clear() | Out-Null
}
