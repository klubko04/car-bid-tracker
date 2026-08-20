<#
.SYNOPSIS
Render one URL in the dedicated debugging Chrome profile and save its HTML.

.DESCRIPTION
A deliberately small sibling of copart_browser_capture_01.ps1. That script
intercepts network responses to rebuild a media gallery; this one only needs
the settled DOM, so it navigates, waits for the document to finish, and writes
document.documentElement.outerHTML.

It exists because some sources answer a stdlib HTTP request with an
interstitial challenge rather than the page. Driving the operator's own signed
-in browser is not a way around that check -- the check still runs, in a real
browser, exactly as it would for a person clicking the same link. Nothing here
forges headers, solves a challenge, or retries to wear one down: if the page
does not settle, the script fails and says so.
#>
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$Port = 9222,
    [int]$TimeoutSeconds = 45,
    [int]$SettleSeconds = 3
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
    param([int]$Id, [int]$TimeoutMs = 30000)
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
    while ([DateTime]::UtcNow -lt $deadline) {
        # Only ONE ReceiveAsync may be in flight on a ClientWebSocket. Keep the
        # pending task across loop turns; starting a second one throws.
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

$version = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 5
$tab = Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$Port/json/new?about:blank" -TimeoutSec 10

$script:ReceiveBuffer = New-Object byte[] 262144
$script:ReceiveStream = New-Object System.IO.MemoryStream
$script:ReceiveTask = $null
$script:Socket = New-Object Net.WebSockets.ClientWebSocket
$script:Socket.ConnectAsync(
    [Uri]$tab.webSocketDebuggerUrl, [Threading.CancellationToken]::None
).GetAwaiter().GetResult()

try {
    [void](Receive-Until -Id (Send-Cdp -Method 'Page.enable'))
    $navId = Send-Cdp -Method 'Page.navigate' -Params @{ url = $Url }
    $nav = Receive-Until -Id $navId -TimeoutMs ($TimeoutSeconds * 1000)
    if ($nav.result.errorText) { throw "navigation failed: $($nav.result.errorText)" }

    # Poll readyState rather than trusting a fixed sleep, then allow a short
    # settle window for client-side rendering to attach the results list.
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $probe = Receive-Until -Id (Send-Cdp -Method 'Runtime.evaluate' -Params @{
            expression = 'document.readyState'; returnByValue = $true
        })
        if ($probe.result.result.value -eq 'complete') { break }
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Seconds $SettleSeconds

    $dom = Receive-Until -Id (Send-Cdp -Method 'Runtime.evaluate' -Params @{
        expression = 'document.documentElement.outerHTML'; returnByValue = $true
    })
    $html = $dom.result.result.value
    if (-not $html) { throw 'the page returned no HTML' }
    $final = Receive-Until -Id (Send-Cdp -Method 'Runtime.evaluate' -Params @{
        expression = 'location.href'; returnByValue = $true
    })
    [IO.File]::WriteAllText($Out, $html, [Text.UTF8Encoding]::new($false))
    Write-Output "browser fetch: $($final.result.result.value)"
    Write-Output "  bytes: $($html.Length)"
    Write-Output "  saved: $Out"
}
finally {
    if ($script:Socket.State -eq [Net.WebSockets.WebSocketState]::Open) {
        [void]$script:Socket.CloseAsync(
            [Net.WebSockets.WebSocketCloseStatus]::NormalClosure, 'done',
            [Threading.CancellationToken]::None
        ).GetAwaiter().GetResult()
    }
    try { Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/close/$($tab.id)" -TimeoutSec 5 | Out-Null }
    catch { Write-Warning "could not close tab $($tab.id)" }
}
