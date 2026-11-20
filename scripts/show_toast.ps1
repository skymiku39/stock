# Show one Windows toast notification. Text comes from environment variables so
# that no user text is ever parsed as script: STOCK_TOAST_TITLE, STOCK_TOAST_BODY.
# Keep this file ASCII only: it has no BOM and Windows PowerShell reads it as ANSI.
$ErrorActionPreference = 'Stop'

$title = [string]$env:STOCK_TOAST_TITLE
$body = [string]$env:STOCK_TOAST_BODY
if (-not $title) { $title = 'Stock Lab' }

[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime]

$titleXml = [System.Security.SecurityElement]::Escape($title)
$bodyXml = [System.Security.SecurityElement]::Escape($body)
$xml = '<toast duration="long"><visual><binding template="ToastGeneric">' +
    "<text>$titleXml</text><text>$bodyXml</text>" +
    '</binding></visual><audio src="ms-winsoundevent:Notification.Reminder"/></toast>'

$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
# Borrow Windows PowerShell's registered app id; an unregistered id shows nothing.
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
