param(
    [int]$Port = 9222,
    [string]$StartUrl = 'about:blank'
)

$ErrorActionPreference = 'Stop'
$endpoint = "http://127.0.0.1:$Port/json/version"

try {
    $version = Invoke-RestMethod -Uri $endpoint -TimeoutSec 2
    Write-Output "Copart browser already available: $($version.Browser)"
    exit 0
}
catch {
    # A dedicated profile is required by Chrome 136+ and keeps the debugging
    # endpoint isolated from the user's normal browser profile.
}

$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
if (-not (Test-Path $chrome)) {
    throw "Chrome was not found at $chrome"
}
$profile = Join-Path $env:LOCALAPPDATA 'car-bid-tracker\copart-cdp-profile'
$arguments = @(
    "--remote-debugging-port=$Port",
    '--remote-debugging-address=127.0.0.1',
    "--user-data-dir=$profile",
    '--no-first-run',
    $StartUrl
)
Start-Process -FilePath $chrome -ArgumentList $arguments

for ($attempt = 0; $attempt -lt 40; $attempt += 1) {
    Start-Sleep -Milliseconds 500
    try {
        $version = Invoke-RestMethod -Uri $endpoint -TimeoutSec 2
        Write-Output "Copart browser started: $($version.Browser)"
        Write-Output "  profile: $profile"
        Write-Output "  DevTools: Windows localhost:$Port"
        exit 0
    }
    catch {
        # Continue waiting for Chrome's local endpoint.
    }
}
throw "Chrome did not expose its local DevTools endpoint on port $Port"
