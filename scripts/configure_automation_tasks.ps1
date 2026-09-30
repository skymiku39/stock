#Requires -Version 5.1
<#
.SYNOPSIS
  調整既有 SkyMiku Windows Task Scheduler 工作為「每次登入常駐 daemon」模式。

.DESCRIPTION
  只更新下列既有工作（找不到或權限不足則 fail-closed，不新建 task、不停正在執行程序）：
    - SkyMiku Codex LLM App Server
    - SkyMiku Stock Daily Scheduler

  變更重點：
    - 僅保留單一 AtLogOn（LogonTrigger）；移除每日 CalendarTrigger
    - MultipleInstancesPolicy = IgnoreNew
    - ExecutionTimeLimit = PT0S（無限制）
    - Stock Daily Scheduler：RestartOnFailure Count=3 Interval=PT2M
    - Codex LLM App Server：若定義已有 RestartOnFailure 則原樣保留

  修改前會將原始 XML 備份到：
    %LOCALAPPDATA%\SkyMiku\TaskSchedulerBackup\

  支援 -WhatIf / -Confirm（CmdletBinding SupportsShouldProcess）。

.EXAMPLE
  .\scripts\configure_automation_tasks.ps1 -WhatIf

.EXAMPLE
  .\scripts\configure_automation_tasks.ps1 -Confirm
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskNamespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
$BackupRoot = Join-Path $env:LOCALAPPDATA 'SkyMiku\TaskSchedulerBackup'

# 與現機既有 task / launcher 對齊（唯讀核對結果）：
#   App Server Action : powershell.exe -File C:\skymiku\stock\scripts\start_codex_app_server.ps1
#                       WorkingDirectory = C:\skymiku\stock
#   Scheduler Action  : C:\skymiku\stock\.venv\Scripts\python.exe -m bot.scheduler --root C:\skymiku\stock
#                       WorkingDirectory = C:\skymiku\stock
$TaskPlans = @(
    [pscustomobject]@{
        Name                   = 'SkyMiku Codex LLM App Server'
        Role                   = 'app-server'
        SetRestartOnFailure    = $false   # 保留既有 RestartOnFailure（若已定義）
        RestartCount           = $null
        RestartInterval        = $null
    }
    [pscustomobject]@{
        Name                   = 'SkyMiku Stock Daily Scheduler'
        Role                   = 'scheduler'
        SetRestartOnFailure    = $true
        RestartCount           = 3
        RestartInterval        = 'PT2M'
    }
)

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ("==== {0} ====" -f $Title) -ForegroundColor Cyan
}

function Get-TaskXmlDocument {
    param([Parameter(Mandatory)][string]$TaskName)
    $xmlText = Export-ScheduledTask -TaskName $TaskName
    $doc = New-Object System.Xml.XmlDocument
    $doc.PreserveWhitespace = $true
    $doc.LoadXml($xmlText)
    return $doc
}

function Get-TaskNsManager {
    param([Parameter(Mandatory)][System.Xml.XmlDocument]$Document)
    $nsmgr = New-Object System.Xml.XmlNamespaceManager($Document.NameTable)
    $nsmgr.AddNamespace('t', $TaskNamespace)
    # XmlNamespaceManager is enumerable; unary comma prevents PowerShell from
    # unrolling it into an Object[] when returned through the pipeline.
    return ,$nsmgr
}

function Get-OrCreateChild {
    param(
        [Parameter(Mandatory)][System.Xml.XmlElement]$Parent,
        [Parameter(Mandatory)][string]$LocalName,
        [Parameter(Mandatory)][System.Xml.XmlDocument]$Document
    )
    $existing = $Parent.ChildNodes | Where-Object {
        $_ -is [System.Xml.XmlElement] -and $_.LocalName -eq $LocalName
    } | Select-Object -First 1
    if ($existing) { return $existing }
    $created = $Document.CreateElement($LocalName, $TaskNamespace)
    [void]$Parent.AppendChild($created)
    return $created
}

function Set-ElementText {
    param(
        [Parameter(Mandatory)][System.Xml.XmlElement]$Parent,
        [Parameter(Mandatory)][string]$LocalName,
        [Parameter(Mandatory)][string]$Text,
        [Parameter(Mandatory)][System.Xml.XmlDocument]$Document
    )
    $el = Get-OrCreateChild -Parent $Parent -LocalName $LocalName -Document $Document
    $el.InnerText = $Text
    return $el
}

function Get-TriggerSummary {
    param([Parameter(Mandatory)]$Task)
    $items = @()
    foreach ($tr in @($Task.Triggers)) {
        $kind = $tr.CimClass.CimClassName -replace '^MSFT_Task', ''
        $extra = ''
        if ($tr.PSObject.Properties.Name -contains 'StartBoundary' -and $tr.StartBoundary) {
            $extra = " StartBoundary=$($tr.StartBoundary)"
        }
        if ($tr.PSObject.Properties.Name -contains 'UserId' -and $tr.UserId) {
            $extra += " UserId=$($tr.UserId)"
        }
        if ($tr.PSObject.Properties.Name -contains 'DaysInterval' -and $null -ne $tr.DaysInterval) {
            $extra += " DaysInterval=$($tr.DaysInterval)"
        }
        $items += ("{0} Enabled={1}{2}" -f $kind, $tr.Enabled, $extra)
    }
    if ($items.Count -eq 0) { return @('(none)') }
    return $items
}

function Get-SettingsSummary {
    param([Parameter(Mandatory)]$Task)
    $s = $Task.Settings
    $restart = 'none'
    if ($s.RestartCount -gt 0) {
        $restart = "Count=$($s.RestartCount) Interval=$($s.RestartInterval)"
    }
    return [ordered]@{
        MultipleInstances  = [string]$s.MultipleInstances
        ExecutionTimeLimit = [string]$s.ExecutionTimeLimit
        RestartOnFailure   = $restart
        StartWhenAvailable = [string]$s.StartWhenAvailable
        Enabled            = [string]$s.Enabled
    }
}

function Get-ActionSummary {
    param([Parameter(Mandatory)]$Task)
    $items = @()
    foreach ($a in @($Task.Actions)) {
        $items += ("Execute={0} Arguments={1} WorkingDirectory={2}" -f $a.Execute, $a.Arguments, $a.WorkingDirectory)
    }
    if ($items.Count -eq 0) { return @('(none)') }
    return $items
}

function Show-TaskSnapshot {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)]$Task
    )
    Write-Host ("-- {0} --" -f $Label)
    Write-Host ("State: {0}" -f $Task.State)
    Write-Host 'Principal:'
    Write-Host ("  UserId={0} LogonType={1} RunLevel={2}" -f `
        $Task.Principal.UserId, $Task.Principal.LogonType, $Task.Principal.RunLevel)
    Write-Host 'Actions:'
    foreach ($line in (Get-ActionSummary -Task $Task)) { Write-Host ("  {0}" -f $line) }
    Write-Host 'Triggers:'
    foreach ($line in (Get-TriggerSummary -Task $Task)) { Write-Host ("  {0}" -f $line) }
    Write-Host 'Settings:'
    $settings = Get-SettingsSummary -Task $Task
    foreach ($key in $settings.Keys) {
        Write-Host ("  {0}={1}" -f $key, $settings[$key])
    }
}

function Backup-TaskXml {
    param(
        [Parameter(Mandatory)][string]$TaskName,
        [Parameter(Mandatory)][string]$XmlText
    )
    if (-not (Test-Path -LiteralPath $BackupRoot)) {
        New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
    }
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $safeName = ($TaskName -replace '[^\w\-]+', '_')
    $path = Join-Path $BackupRoot ("{0}_{1}.xml" -f $safeName, $stamp)
    Set-Content -LiteralPath $path -Value $XmlText -Encoding UTF8
    return $path
}

function Update-TaskDocument {
    param(
        [Parameter(Mandatory)][System.Xml.XmlDocument]$Document,
        [Parameter(Mandatory)]$Plan
    )
    $nsmgr = Get-TaskNsManager -Document $Document
    $taskNode = $Document.SelectSingleNode('/t:Task', $nsmgr)
    if (-not $taskNode) {
        throw "Task XML 缺少 /Task 根節點：$($Plan.Name)"
    }

    # --- Settings ---
    $settings = $Document.SelectSingleNode('/t:Task/t:Settings', $nsmgr)
    if (-not $settings) {
        $settings = $Document.CreateElement('Settings', $TaskNamespace)
        [void]$taskNode.AppendChild($settings)
    }
    Set-ElementText -Parent $settings -LocalName 'MultipleInstancesPolicy' -Text 'IgnoreNew' -Document $Document | Out-Null
    Set-ElementText -Parent $settings -LocalName 'ExecutionTimeLimit' -Text 'PT0S' -Document $Document | Out-Null

    $existingRestart = $settings.SelectSingleNode('t:RestartOnFailure', $nsmgr)
    if ($Plan.SetRestartOnFailure) {
        $restart = Get-OrCreateChild -Parent $settings -LocalName 'RestartOnFailure' -Document $Document
        Set-ElementText -Parent $restart -LocalName 'Count' -Text ([string]$Plan.RestartCount) -Document $Document | Out-Null
        Set-ElementText -Parent $restart -LocalName 'Interval' -Text ([string]$Plan.RestartInterval) -Document $Document | Out-Null
    }
    # App Server：已有 RestartOnFailure 則不動；沒有也不新建（保留既有定義）

    # --- Triggers：僅保留單一 LogonTrigger ---
    $triggers = $Document.SelectSingleNode('/t:Task/t:Triggers', $nsmgr)
    if (-not $triggers) {
        $triggers = $Document.CreateElement('Triggers', $TaskNamespace)
        [void]$taskNode.AppendChild($triggers)
    }

    $logonNodes = @($triggers.SelectNodes('t:LogonTrigger', $nsmgr))
    $otherNodes = @($triggers.ChildNodes) | Where-Object {
        $_ -is [System.Xml.XmlElement] -and $_.LocalName -ne 'LogonTrigger'
    }

    foreach ($node in $otherNodes) {
        [void]$triggers.RemoveChild($node)
    }

    $keptLogon = $null
    if ($logonNodes.Count -ge 1) {
        $keptLogon = $logonNodes[0]
        for ($i = 1; $i -lt $logonNodes.Count; $i++) {
            [void]$triggers.RemoveChild($logonNodes[$i])
        }
    }
    else {
        $keptLogon = $Document.CreateElement('LogonTrigger', $TaskNamespace)
        [void]$triggers.AppendChild($keptLogon)
        $userId = $null
        $principalUser = $Document.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:UserId', $nsmgr)
        if ($principalUser -and $principalUser.InnerText) {
            $userId = $principalUser.InnerText
        }
        if (-not $userId) {
            $userId = "$env:USERDOMAIN\$env:USERNAME"
        }
        Set-ElementText -Parent $keptLogon -LocalName 'UserId' -Text $userId -Document $Document | Out-Null
    }

    # 確保 Enabled（若原本沒有則補 true；有則保留）
    $enabledNode = $keptLogon.SelectSingleNode('t:Enabled', $nsmgr)
    if (-not $enabledNode) {
        Set-ElementText -Parent $keptLogon -LocalName 'Enabled' -Text 'true' -Document $Document | Out-Null
    }

    return $Document
}

function Test-TaskDesiredState {
    param(
        [Parameter(Mandatory)]$Task,
        [Parameter(Mandatory)]$Plan
    )
    $errors = New-Object System.Collections.Generic.List[string]

    $triggers = @($Task.Triggers)
    if ($triggers.Count -ne 1) {
        $errors.Add("期望恰好 1 個 trigger，實際 $($triggers.Count)")
    }
    else {
        $kind = $triggers[0].CimClass.CimClassName
        if ($kind -ne 'MSFT_TaskLogonTrigger') {
            $errors.Add("期望 LogonTrigger，實際 $kind")
        }
        if (-not $triggers[0].Enabled) {
            $errors.Add('LogonTrigger 未啟用')
        }
    }

    if ([string]$Task.Settings.MultipleInstances -ne 'IgnoreNew') {
        $errors.Add("MultipleInstances=$($Task.Settings.MultipleInstances)（期望 IgnoreNew）")
    }

    $limit = [string]$Task.Settings.ExecutionTimeLimit
    # CIM 可能回傳 TimeSpan 字串或 PT0S
    if ($limit -notin @('PT0S', '00:00:00', '')) {
        # 空字串有時也代表無限制；仍以 PT0S / 00:00:00 為主
        $ts = $null
        if ([TimeSpan]::TryParse($limit, [ref]$ts)) {
            if ($ts -ne [TimeSpan]::Zero) {
                $errors.Add("ExecutionTimeLimit=$limit（期望 PT0S / 無限制）")
            }
        }
        else {
            $errors.Add("ExecutionTimeLimit=$limit（期望 PT0S / 無限制）")
        }
    }

    if ($Plan.SetRestartOnFailure) {
        if ([int]$Task.Settings.RestartCount -ne [int]$Plan.RestartCount) {
            $errors.Add("RestartCount=$($Task.Settings.RestartCount)（期望 $($Plan.RestartCount)）")
        }
        $interval = [string]$Task.Settings.RestartInterval
        $okInterval = $interval -eq $Plan.RestartInterval -or $interval -eq '00:02:00'
        if (-not $okInterval) {
            $errors.Add("RestartInterval=$interval（期望 $($Plan.RestartInterval)）")
        }
    }

    return $errors
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
$results = @()
$failed = $false

Write-Section 'SkyMiku automation task configure'
Write-Host "Backup directory: $BackupRoot"
Write-Host 'Policy: fail-closed if task missing / access denied; never create new tasks; never stop running processes.'
Write-Host 'Mode: single AtLogOn daemon (remove daily CalendarTrigger).'

foreach ($plan in $TaskPlans) {
    Write-Section $plan.Name
    $entry = [ordered]@{
        name       = $plan.Name
        role       = $plan.Role
        status     = 'pending'
        backup     = $null
        before     = $null
        after      = $null
        validation = @()
        error      = $null
    }

    try {
        $task = Get-ScheduledTask -TaskName $plan.Name -ErrorAction Stop
    }
    catch {
        $failed = $true
        $entry.status = 'missing'
        $entry.error = "找不到既有工作「$($plan.Name)」。fail-closed：不建立新 task。"
        Write-Host $entry.error -ForegroundColor Red
        $results += [pscustomobject]$entry
        continue
    }

    Show-TaskSnapshot -Label 'BEFORE' -Task $task
    $entry.before = [ordered]@{
        state    = [string]$task.State
        actions  = @(Get-ActionSummary -Task $task)
        triggers = @(Get-TriggerSummary -Task $task)
        settings = Get-SettingsSummary -Task $task
    }

    try {
        $doc = Get-TaskXmlDocument -TaskName $plan.Name
        $originalXml = $doc.OuterXml
        $updated = Update-TaskDocument -Document $doc -Plan $plan
        $newXml = $updated.OuterXml

        $target = "Scheduled Task '$($plan.Name)'"
        if ($PSCmdlet.ShouldProcess($target, 'Backup XML then register updated definition (preserve Actions/Principal; AtLogOn only)')) {
            $backupPath = Backup-TaskXml -TaskName $plan.Name -XmlText $originalXml
            $entry.backup = $backupPath
            Write-Host "Backup written: $backupPath" -ForegroundColor DarkGray

            try {
                # -Force 更新既有定義；不 Stop-ScheduledTask / 不停程序
                Register-ScheduledTask -TaskName $plan.Name -Xml $newXml -Force | Out-Null
            }
            catch {
                $failed = $true
                $entry.status = 'access_denied_or_register_failed'
                $entry.error = "寫入失敗（可能權限不足）：$($_.Exception.Message)"
                Write-Host $entry.error -ForegroundColor Red
                $results += [pscustomobject]$entry
                continue
            }

            $afterTask = Get-ScheduledTask -TaskName $plan.Name
            Show-TaskSnapshot -Label 'AFTER' -Task $afterTask
            $entry.after = [ordered]@{
                state    = [string]$afterTask.State
                actions  = @(Get-ActionSummary -Task $afterTask)
                triggers = @(Get-TriggerSummary -Task $afterTask)
                settings = Get-SettingsSummary -Task $afterTask
            }

            $validationErrors = @(Test-TaskDesiredState -Task $afterTask -Plan $plan)
            if ($validationErrors.Count -eq 0) {
                $entry.status = 'ok'
                $entry.validation = @(
                    'pass: AtLogOn-only',
                    'pass: IgnoreNew',
                    'pass: ExecutionTimeLimit',
                    'pass: RestartOnFailure policy'
                )
                Write-Host 'VALIDATION: PASS' -ForegroundColor Green
            }
            else {
                $failed = $true
                $entry.status = 'validation_failed'
                $entry.validation = @($validationErrors)
                Write-Host 'VALIDATION: FAIL' -ForegroundColor Red
                foreach ($err in $validationErrors) { Write-Host ("  - {0}" -f $err) -ForegroundColor Red }
            }
        }
        else {
            $entry.status = 'whatif_or_declined'
            $entry.validation = @('no register performed (-WhatIf or -Confirm declined)')
            Write-Host 'Skipped backup/register (WhatIf / Confirm declined). Desired XML changes prepared in memory only.' -ForegroundColor Yellow
            Write-Host ("Would backup to: {0}" -f $BackupRoot) -ForegroundColor Yellow
            Write-Host 'Desired deltas:' -ForegroundColor Yellow
            Write-Host '  Triggers -> single LogonTrigger (remove CalendarTrigger/daily)'
            Write-Host '  Settings.MultipleInstancesPolicy -> IgnoreNew'
            Write-Host '  Settings.ExecutionTimeLimit -> PT0S'
            if ($plan.SetRestartOnFailure) {
                Write-Host ("  Settings.RestartOnFailure -> Count={0} Interval={1}" -f $plan.RestartCount, $plan.RestartInterval)
            }
            else {
                Write-Host '  Settings.RestartOnFailure -> preserve existing (if present)'
            }
        }
    }
    catch {
        $failed = $true
        $msg = "{0} (line {1}: {2}; stack: {3})" -f $_.Exception.Message, `
            $_.InvocationInfo.ScriptLineNumber, $_.InvocationInfo.Line.Trim(), $_.ScriptStackTrace
        if ($msg -match 'Access is denied|拒絕存取|Unauthorized|permission') {
            $entry.status = 'access_denied'
            $entry.error = "權限不足：$msg"
        }
        else {
            $entry.status = 'error'
            $entry.error = $msg
        }
        Write-Host $entry.error -ForegroundColor Red
    }

    $results += [pscustomobject]$entry
}

Write-Section 'Summary'
$results | ForEach-Object {
    Write-Host ("[{0}] {1}" -f $_.status, $_.name)
    if ($_.backup) { Write-Host ("  backup: {0}" -f $_.backup) }
    if ($_.error) { Write-Host ("  error: {0}" -f $_.error) }
    if ($_.validation) {
        foreach ($v in $_.validation) { Write-Host ("  validation: {0}" -f $v) }
    }
}

# 機器可讀摘要（stdout 最後一段）
$json = [ordered]@{
    ok      = -not $failed
    command = 'configure_automation_tasks'
    results = $results
}
Write-Section 'JSON'
$json | ConvertTo-Json -Depth 8

if ($failed) {
    exit 2
}
exit 0
