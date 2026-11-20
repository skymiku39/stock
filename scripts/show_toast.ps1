# Show one Windows toast notification. Text comes from environment variables so
# that no user text is ever parsed as script:
#   STOCK_TOAST_TITLE, STOCK_TOAST_BODY, STOCK_TOAST_APP_ID (name shown on the toast).
# Same technique as the winotify package (used by Hello Streamer): the classic
# ToastImageAndText02 template shown under the app's own name, not PowerShell's.
# Keep this file ASCII only: it has no BOM and Windows PowerShell reads it as ANSI.
$ErrorActionPreference = 'Stop'

$title = [string]$env:STOCK_TOAST_TITLE
$body = [string]$env:STOCK_TOAST_BODY
$appId = [string]$env:STOCK_TOAST_APP_ID
if (-not $title) { $title = 'Stock Lab' }
if (-not $appId) { $appId = 'Stock Lab' }

[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

$titleXml = [System.Security.SecurityElement]::Escape($title)
$bodyXml = [System.Security.SecurityElement]::Escape($body)
$xml = '<toast duration="long"><visual><binding template="ToastImageAndText02">' +
    '<image id="1" src="" />' +
    "<text id=`"1`">$titleXml</text><text id=`"2`">$bodyXml</text>" +
    '</binding></visual><audio src="ms-winsoundevent:Notification.Default" loop="false" /></toast>'

$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
