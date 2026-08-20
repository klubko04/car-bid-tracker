<#
.SYNOPSIS
POST a form to a first-party JSON endpoint from inside the site's own origin,
using the operator's dedicated debugging Chrome profile.

.DESCRIPTION
Some first-party JSON endpoints stop answering stdlib HTTP entirely once the
site's WAF decides an IP looks automated: Copart's /public/lots/search began
returning an Incapsula interstitial to urllib while the same endpoint kept
working normally in a browser on the same machine.

This navigates to a page on the target origin, then evaluates the site's own
fetch() against its own endpoint. Same origin, same cookies, same headers the
page would send. It is not a way around the WAF check -- the check runs, in a
real browser, and if it has not been satisfied the fetch returns the
interstitial and this script reports that verbatim rather than retrying.

Used by pull_copart_web_01.py as a fallback after the direct transport is
challenged. Nothing here is Copart-specific.
#>
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Origin,
    [Parameter(Mandatory = $true)][string]$Body,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$ContentType = 'application/x-www-form-urlencoded;charset=UTF-8',
    [int]$Port = 9222,
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
$script:CommandId = 0

function Send-Cdp {
    param([Parameter(Mandatory = $true)][string]$Method, [hashtable]$Params = @{})
    $script:CommandId += 1
    $message = @{ id = $script:CommandId; method = $Method; params = $Params } |
        ConvertTo-Json -Depth 20 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($message)
    $segment = [ArraySegment[byte]]::new($bytes)
    [void]$script:Socket.SendAsync(
        $segment, [Net.WebSockets.WebSocketMessageType]::Text, $true,
        [Threading.CancellationToken]::None
    ).GetAwaiter().GetResult()
    return $script:CommandId
}

function Receive-Until {
    param([int]$Id, [int]$TimeoutMs = 60000)
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -eq $script:ReceiveTask) {
            $segment = [ArraySegment[byte]]::new($script:ReceiveBuffer)
            $script:ReceiveTask = $script:Socket.ReceiveAsync(
                $segment, [Threading.CancellationToken]::None)
        }
        if (-not $script:ReceiveTask.Wait(2000)) { continue }
        $result = $script:ReceiveTask.GetAwaiter().GetResult()
        $script:ReceiveTask = $null
        if ($result.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) {
            throw 'Chrome closed the DevTools connection.'
        }
        if ($result.Count -gt 0) {
            $script:ReceiveStream.Write($script:ReceiveBuffer, 0, $result.Count)
        }
        if (-not $result.EndOfMessage) { continue }
        $text = [Text.Encoding]::UTF8.GetString($script:ReceiveStream.ToArray())
        $script:ReceiveStream.SetLength(0)
        if (-not $text) { continue }
        $message = $text | ConvertFrom-Json
        if ($message.id -eq $Id) { return $message }
    }
    throw "Timed out waiting for CDP response $Id"
}

[void](Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 5)
$tab = Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$Port/json/new?about:blank" -TimeoutSec 10

$script:ReceiveBuffer = New-Object byte[] 1048576
$script:ReceiveStream = New-Object System.IO.MemoryStream
$script:ReceiveTask = $null
$script:Socket = New-Object Net.WebSockets.ClientWebSocket
$script:Socket.ConnectAsync(
    [Uri]$tab.webSocketDebuggerUrl, [Threading.CancellationToken]::None
).GetAwaiter().GetResult()

try {
    [void](Receive-Until -Id (Send-Cdp -Method 'Page.enable'))
    $nav = Receive-Until -Id (Send-Cdp -Method 'Page.navigate' -Params @{ url = $Origin }) `
        -TimeoutMs ($TimeoutSeconds * 1000)
    if ($nav.result.errorText) { throw "navigation failed: $($nav.result.errorText)" }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $probe = Receive-Until -Id (Send-Cdp -Method 'Runtime.evaluate' -Params @{
            expression = 'document.readyState'; returnByValue = $true })
        if ($probe.result.result.value -eq 'complete') { break }
        Start-Sleep -Milliseconds 500
    }

    # The page's own fetch, against the page's own endpoint.
    $payload = @{ url = $Url; body = $Body; contentType = $ContentType } |
        ConvertTo-Json -Compress
    $expression = @"
(async () => {
  const spec = $payload;
  const response = await fetch(spec.url, {
    method: 'POST',
    credentials: 'include',
    headers: {'Content-Type': spec.contentType, 'Accept': 'application/json, text/plain, */*'},
    body: spec.body
  });
  return JSON.stringify({status: response.status, body: await response.text()});
})()
"@
    $evaluated = Receive-Until -Id (Send-Cdp -Method 'Runtime.evaluate' -Params @{
        expression = $expression; awaitPromise = $true; returnByValue = $true
    }) -TimeoutMs ($TimeoutSeconds * 1000)

    if ($evaluated.result.exceptionDetails) {
        throw "fetch failed: $($evaluated.result.exceptionDetails.text)"
    }
    $envelope = $evaluated.result.result.value | ConvertFrom-Json
    [IO.File]::WriteAllText($Out, $envelope.body, [Text.UTF8Encoding]::new($false))
    Write-Output "browser post: $Url"
    Write-Output "  status: $($envelope.status)"
    Write-Output "  bytes: $($envelope.body.Length)"
}
finally {
    if ($script:Socket.State -eq [Net.WebSockets.WebSocketState]::Open) {
        [void]$script:Socket.CloseAsync(
            [Net.WebSockets.WebSocketCloseStatus]::NormalClosure, 'done',
            [Threading.CancellationToken]::None).GetAwaiter().GetResult()
    }
    try { Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/close/$($tab.id)" -TimeoutSec 5 | Out-Null }
    catch { Write-Warning "could not close tab $($tab.id)" }
}
