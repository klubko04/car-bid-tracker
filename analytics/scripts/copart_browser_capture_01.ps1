param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+$')]
    [string]$Lot,

    [Parameter(Mandatory = $true)]
    [string]$Out,

    [int]$Port = 9222,
    [int]$CaptureSeconds = 35,

    [switch]$DedicatedTab
)

$ErrorActionPreference = 'Stop'

function Send-Cdp {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [hashtable]$Params = @{}
    )
    $script:CommandId += 1
    $message = @{
        id = $script:CommandId
        method = $Method
        params = $Params
    } | ConvertTo-Json -Depth 20 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($message)
    $segment = [ArraySegment[byte]]::new($bytes)
    [void]$script:Socket.SendAsync(
        $segment,
        [Net.WebSockets.WebSocketMessageType]::Text,
        $true,
        [Threading.CancellationToken]::None
    ).GetAwaiter().GetResult()
    return $script:CommandId
}

function Receive-Cdp {
    param([int]$WaitMilliseconds = 250)

    if ($null -eq $script:ReceiveTask) {
        $segment = [ArraySegment[byte]]::new($script:ReceiveBuffer)
        $script:ReceiveTask = $script:Socket.ReceiveAsync(
            $segment, [Threading.CancellationToken]::None
        )
    }
    if (-not $script:ReceiveTask.Wait($WaitMilliseconds)) {
        return $null
    }

    $result = $script:ReceiveTask.GetAwaiter().GetResult()
    $script:ReceiveTask = $null
    if ($result.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) {
        throw 'Chrome closed the DevTools connection.'
    }
    if ($result.Count -gt 0) {
        $script:ReceiveStream.Write($script:ReceiveBuffer, 0, $result.Count)
    }
    if (-not $result.EndOfMessage) {
        return $null
    }

    $text = [Text.Encoding]::UTF8.GetString($script:ReceiveStream.ToArray())
    $script:ReceiveStream.SetLength(0)
    if (-not $text) {
        return $null
    }
    return $text | ConvertFrom-Json
}

function Add-CaptureEntry {
    param(
        [string]$Url,
        [string]$ResourceType,
        [string]$MimeType,
        [string]$Body = '',
        [bool]$Base64Encoded = $false
    )
    if (-not $Url) {
        return
    }
    $content = @{
        mimeType = if ($MimeType) { $MimeType } else { 'application/octet-stream' }
        text = $Body
    }
    if ($Base64Encoded) {
        $content.encoding = 'base64'
    }
    $script:Entries.Add(@{
        _resourceType = if ($ResourceType) { $ResourceType.ToLowerInvariant() } else { 'other' }
        request = @{ url = $Url }
        response = @{ content = $content }
    })
}

$endpoint = "http://127.0.0.1:$Port/json/list"
$createdTab = $false
if ($DedicatedTab) {
    $newTarget = "http://127.0.0.1:$Port/json/new?" +
        [Uri]::EscapeDataString('about:blank')
    $tab = Invoke-RestMethod -Method Put -Uri $newTarget
    $createdTab = $true
}
else {
    $tabsResponse = Invoke-RestMethod -Uri $endpoint
    $tabs = @($tabsResponse.GetEnumerator())
    $lotPattern = "^https://(?:www\.)?copart\.com/lot/$Lot(?:[/?#]|$)"
    $tab = $tabs | Where-Object {
        $_.type -eq 'page' -and $_.url -match $lotPattern
    } | Select-Object -First 1
    if ($null -eq $tab) {
        $tab = $tabs | Where-Object {
            $_.type -eq 'page' -and $_.url -match '^https://(?:www\.)?copart\.com/'
        } | Select-Object -First 1
    }
    if ($null -eq $tab) {
        $newTarget = "http://127.0.0.1:$Port/json/new?" +
            [Uri]::EscapeDataString('about:blank')
        $tab = Invoke-RestMethod -Method Put -Uri $newTarget
    }
}

$script:Socket = [Net.WebSockets.ClientWebSocket]::new()
[void]$script:Socket.ConnectAsync(
    [Uri]$tab.webSocketDebuggerUrl,
    [Threading.CancellationToken]::None
).GetAwaiter().GetResult()
$script:CommandId = 0
$script:ReceiveBuffer = New-Object byte[] 1048576
$script:ReceiveStream = [IO.MemoryStream]::new()
$script:ReceiveTask = $null
$script:Entries = [Collections.Generic.List[object]]::new()
$requests = @{}
$responses = @{}
$pendingBodies = @{}
$runtimeResults = @{}
$galleryClicked = $false
$thumbnailsCycled = $false
$pageLoadedAt = $null

try {
    [void](Send-Cdp -Method 'Network.enable' -Params @{
        maxTotalBufferSize = 50000000
        maxResourceBufferSize = 10000000
    })
    [void](Send-Cdp -Method 'Page.enable')
    [void](Send-Cdp -Method 'Runtime.enable')
    [void](Send-Cdp -Method 'Page.navigate' -Params @{
        url = "https://www.copart.com/lot/$Lot"
    })

    $deadline = [DateTime]::UtcNow.AddSeconds($CaptureSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $message = Receive-Cdp -WaitMilliseconds 250

        if ($message.method -eq 'Network.requestWillBeSent') {
            $requestId = [string]$message.params.requestId
            $requests[$requestId] = @{
                url = [string]$message.params.request.url
                type = [string]$message.params.type
            }
        }
        if ($message.method -eq 'Network.responseReceived') {
            $requestId = [string]$message.params.requestId
            $responses[$requestId] = @{
                url = [string]$message.params.response.url
                mime = [string]$message.params.response.mimeType
                type = [string]$message.params.type
                status = [int]$message.params.response.status
            }
        }
        if ($message.method -eq 'Network.loadingFinished') {
            $requestId = [string]$message.params.requestId
            $meta = $responses[$requestId]
            if ($null -ne $meta) {
                Add-CaptureEntry -Url $meta.url -ResourceType $meta.type -MimeType $meta.mime
                if ($meta.status -eq 200 -and $meta.type -in @('Document', 'XHR', 'Fetch')) {
                    $command = Send-Cdp -Method 'Network.getResponseBody' -Params @{
                        requestId = $requestId
                    }
                    $pendingBodies[[string]$command] = $meta
                }
            }
        }
        if ($message.method -eq 'Page.loadEventFired') {
            $pageLoadedAt = [DateTime]::UtcNow
        }

        if ($null -ne $message.id) {
            $id = [string]$message.id
            if ($pendingBodies.ContainsKey($id)) {
                $meta = $pendingBodies[$id]
                $pendingBodies.Remove($id)
                if ($null -ne $message.result) {
                    Add-CaptureEntry `
                        -Url $meta.url `
                        -ResourceType $meta.type `
                        -MimeType $meta.mime `
                        -Body ([string]$message.result.body) `
                        -Base64Encoded ([bool]$message.result.base64Encoded)
                }
            }
            if ($runtimeResults.ContainsKey($id)) {
                $kind = $runtimeResults[$id]
                $runtimeResults.Remove($id)
                $value = [string]$message.result.result.value
                if ($kind -eq 'snapshot' -and $value) {
                    Add-CaptureEntry `
                        -Url "https://www.copart.com/lot/$Lot#runtime-capture" `
                        -ResourceType 'fetch' `
                        -MimeType 'application/json' `
                        -Body $value
                }
            }
        }

        if (-not $galleryClicked -and $null -ne $pageLoadedAt -and
            [DateTime]::UtcNow -ge $pageLoadedAt.AddSeconds(1)) {
            $clickExpression = @'
(() => {
  const text = el => `${el.innerText || ''} ${el.getAttribute('aria-label') || ''} ${el.title || ''}`;
  const controls = [...document.querySelectorAll('button,a,[role="button"]')];
  const target = controls.find(el => /view\s+all\s+photos|all\s+photos|view\s+photos/i.test(text(el)));
  if (!target) return {clicked:false, title:document.title, url:location.href};
  target.click();
  return {clicked:true, label:text(target).trim(), title:document.title, url:location.href};
})()
'@
            $command = Send-Cdp -Method 'Runtime.evaluate' -Params @{
                expression = $clickExpression
                returnByValue = $true
                awaitPromise = $true
            }
            $runtimeResults[[string]$command] = 'click'
            $galleryClicked = $true
        }

        if (-not $thumbnailsCycled -and $galleryClicked -and
            $null -ne $pageLoadedAt -and
            [DateTime]::UtcNow -ge $pageLoadedAt.AddSeconds(4)) {
            $cycleExpression = @'
(async () => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const thumbnails = [...document.querySelectorAll('img')]
    .filter(img => /_thb\.(?:jpe?g|png|webp)(?:[?#]|$)/i.test(img.currentSrc || img.src || ''));
  const unique = [...new Map(thumbnails.map(img => [img.currentSrc || img.src, img])).values()];
  let clicked = 0;
  for (const img of unique) {
    const control = img.closest('button,a,[role="button"]') || img;
    control.scrollIntoView({block:'nearest', inline:'nearest'});
    control.click();
    clicked += 1;
    await sleep(1250);
  }
  return {found: unique.length, clicked};
})()
'@
            $command = Send-Cdp -Method 'Runtime.evaluate' -Params @{
                expression = $cycleExpression
                returnByValue = $true
                awaitPromise = $true
            }
            $runtimeResults[[string]$command] = 'cycle'
            $thumbnailsCycled = $true
        }
    }

    $snapshotExpression = @'
JSON.stringify({
  url: location.href,
  title: document.title,
  images: [...document.images].flatMap(img => [img.currentSrc, img.src, img.srcset]).filter(Boolean),
  resources: performance.getEntriesByType('resource').map(entry => entry.name),
  html: document.documentElement.outerHTML
})
'@
    $snapshotCommand = Send-Cdp -Method 'Runtime.evaluate' -Params @{
        expression = $snapshotExpression
        returnByValue = $true
        awaitPromise = $true
    }
    $runtimeResults[[string]$snapshotCommand] = 'snapshot'

    $snapshotDeadline = [DateTime]::UtcNow.AddSeconds(5)
    while ($runtimeResults.ContainsKey([string]$snapshotCommand) -and
           [DateTime]::UtcNow -lt $snapshotDeadline) {
        $message = Receive-Cdp -WaitMilliseconds 250
        if ($null -eq $message -or $null -eq $message.id) {
            continue
        }
        $id = [string]$message.id
        if ($runtimeResults.ContainsKey($id)) {
            $runtimeResults.Remove($id)
            $value = [string]$message.result.result.value
            if ($value) {
                Add-CaptureEntry `
                    -Url "https://www.copart.com/lot/$Lot#runtime-capture" `
                    -ResourceType 'fetch' `
                    -MimeType 'application/json' `
                    -Body $value
            }
        }
    }

    $output = @{
        log = @{
            version = '1.2'
            creator = @{ name = 'copart_browser_capture_01'; version = '1' }
            entries = $script:Entries
        }
    }
    $destination = [IO.Path]::GetFullPath($Out)
    $parent = Split-Path -Parent $destination
    if ($parent -and -not (Test-Path $parent)) {
        [void](New-Item -ItemType Directory -Path $parent -Force)
    }
    $output | ConvertTo-Json -Depth 30 | Set-Content -Path $destination -Encoding UTF8
    Write-Output "Copart browser capture: lot $Lot"
    Write-Output "  entries: $($script:Entries.Count)"
    Write-Output "  capture: $destination"
}
finally {
    if ($script:Socket.State -eq [Net.WebSockets.WebSocketState]::Open) {
        [void]$script:Socket.CloseAsync(
            [Net.WebSockets.WebSocketCloseStatus]::NormalClosure,
            'capture complete',
            [Threading.CancellationToken]::None
        ).GetAwaiter().GetResult()
    }
    $script:Socket.Dispose()
    $script:ReceiveStream.Dispose()
    if ($createdTab -and $null -ne $tab.id) {
        try {
            [void](Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/close/$($tab.id)")
        }
        catch {
            Write-Warning "Could not close dedicated Copart capture tab $($tab.id): $($_.Exception.Message)"
        }
    }
}
