<#
.SYNOPSIS
    Diagnoses an HTTP 502 (or 504) "Bad Gateway" by identifying which hop answered
    and which proxy/gateway sits in front of the application.

.DESCRIPTION
    1. Resolves DNS for the target so you see the real origin/CDN you land on.
    2. Times a raw TCP connect, so a dead port is distinguishable from a dead app.
    3. Sends the request and reports status, reason phrase and response headers.
    4. Fingerprints the gateway software from its signature headers
       (nginx, Cloudflare, Tailscale, Varnish, CloudFront, Caddy, IIS, ...).
    5. With -OriginUrl it tests the upstream directly, which tells you whether the
       gateway or the application itself is at fault.

.EXAMPLE
    .\diagnose-502.ps1 -Url https://books.example.com/

.EXAMPLE
    .\diagnose-502.ps1 -Url https://books.example.com/ -OriginUrl http://127.0.0.1:8083/
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [string]$OriginUrl = '',
    [int]$TimeoutSec = 20
)

$ErrorActionPreference = 'Continue'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11 -bor [Net.SecurityProtocolType]::Tls
} catch { }

function Write-Head([string]$Text) { Write-Host ''; Write-Host "=== $Text ===" -ForegroundColor Cyan }

function Get-HttpDiagnostic {
    param([string]$Target, [int]$Timeout)

    $out = [ordered]@{
        Url = $Target; Status = $null; Reason = ''; Server = ''
        Headers = @{}; Body = ''; Error = ''
    }

    try {
        $resp = Invoke-WebRequest -Uri $Target -UseBasicParsing -TimeoutSec $Timeout -MaximumRedirection 5
        $out.Status = [int]$resp.StatusCode
        $out.Reason = $resp.StatusDescription
        $out.Headers = $resp.Headers
        $out.Body = [string]$resp.Content
    }
    catch {
        $out.Error = $_.Exception.Message
        $webResp = $_.Exception.Response
        if ($webResp) {
            $out.Status = [int]$webResp.StatusCode
            $out.Reason = $webResp.StatusDescription
            try { $out.Headers = $webResp.Headers } catch { }
            try {
                $reader = New-Object System.IO.StreamReader($webResp.GetResponseStream())
                $out.Body = $reader.ReadToEnd()
                $reader.Dispose()
            } catch { }
        }
    }

    if ($out.Headers -and $out.Headers['Server']) { $out.Server = ($out.Headers['Server'] -join ', ') }
    return [pscustomobject]$out
}

function Get-GatewayFingerprint {
    param($Diag)

    $h = @{}
    foreach ($k in $Diag.Headers.Keys) { $h[$k.ToLower()] = ($Diag.Headers[$k] -join ', ') }

    $hits = New-Object System.Collections.Generic.List[string]
    if ($h['cf-ray'] -or ($h['server'] -match 'cloudflare')) { $hits.Add('Cloudflare (a terminal 502 usually means the tunnel or origin is unreachable)') }
    if ($h['server'] -match 'nginx') { $hits.Add('nginx') }
    if ($h['server'] -match 'tailscale') { $hits.Add('Tailscale serve/funnel (502 = nothing healthy on the target local port)') }
    if ($h['x-varnish'] -or ($h['via'] -match 'varnish')) { $hits.Add('Varnish') }
    if ($h['x-amz-cf-id']) { $hits.Add('AWS CloudFront') }
    if ($h['x-akamai-transformed'] -or ($h['server'] -match 'akamai')) { $hits.Add('Akamai') }
    if ($h['server'] -match 'caddy') { $hits.Add('Caddy (502 = upstream dial failed)') }
    if ($h['server'] -match 'IIS') { $hits.Add('Microsoft IIS / ARR (502.3 = upstream timeout, 502.4 = no upstream server)') }
    if ($h['x-served-by'] -match 'cache' -or $h['fastly']) { $hits.Add('Fastly') }
    if ($h['via'] -match 'squid' -or $h['x-cache'] -match 'squid') { $hits.Add('Squid') }
    if ($h['x-vercel-id']) { $hits.Add('Vercel') }
    if ($h['x-render-origin-server']) { $hits.Add('Render') }

    if ($hits.Count -eq 0) { return 'no proxy signature headers found (the error may be coming from the origin app itself)' }
    return ($hits | Select-Object -Unique) -join '; '
}

Write-Host "HTTP 502 diagnostic  ->  $Url" -ForegroundColor White
Write-Host ("Local time: {0}  |  Host: {1}" -f (Get-Date -Format 'u'), $env:COMPUTERNAME)

# --- 1. Local client proxy / routing sanity ----------------------------------
Write-Head 'Local client configuration'
$wininet = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
if ($wininet.ProxyEnable -eq 1) {
    Write-Host ("WinINET proxy: ENABLED -> {0} {1}" -f $wininet.ProxyServer, $wininet.AutoConfigURL) -ForegroundColor Yellow
    Write-Host '  A stale client proxy is itself a common source of 502 pages.' -ForegroundColor Yellow
} else {
    Write-Host 'WinINET proxy: disabled (direct connection)' -ForegroundColor Green
}
$winhttp = (netsh winhttp show proxy) 2>&1 | Out-String
if ($winhttp -match 'Direct access') { Write-Host 'WinHTTP proxy: direct access' -ForegroundColor Green }
else { Write-Host $winhttp.Trim() -ForegroundColor Yellow }
Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Select-Object InterfaceAlias, NextHop, RouteMetric |
    Format-Table -AutoSize | Out-String | Write-Host

# --- 2. DNS ------------------------------------------------------------------
$uri = [System.Uri]$Url
Write-Head "DNS for $($uri.DnsSafeHost)"
try {
    $dns = Resolve-DnsName -Name $uri.DnsSafeHost -ErrorAction Stop | Where-Object { $_.Type -in 'A', 'AAAA', 'CNAME' }
    $dns | Select-Object Name, Type, IPAddress, NameHost | Format-Table -AutoSize | Out-String | Write-Host
}
catch {
    Write-Host ("DNS resolution FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host '  If DNS fails, fix DNS/VPN first - nothing else matters.' -ForegroundColor Red
}

# --- 3. Raw TCP reachability -------------------------------------------------
$port = if ($uri.Port -gt 0) { $uri.Port } else { if ($uri.Scheme -eq 'https') { 443 } else { 80 } }
Write-Head "TCP connect $($uri.DnsSafeHost):$port"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$client = New-Object System.Net.Sockets.TcpClient
try {
    $async = $client.BeginConnect($uri.DnsSafeHost, $port, $null, $null)
    if ($async.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($TimeoutSec)) -and $client.Connected) {
        $sw.Stop()
        Write-Host ("OK - connected in {0} ms" -f $sw.ElapsedMilliseconds) -ForegroundColor Green
    } else {
        Write-Host ("FAILED/TIMED OUT after {0} ms (firewall, closed port, or a VPN dropping it)" -f $sw.ElapsedMilliseconds) -ForegroundColor Red
    }
}
catch { Write-Host ("FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red }
finally { $client.Close() }

# --- 4. The actual request ---------------------------------------------------
Write-Head "GET $Url"
$main = Get-HttpDiagnostic -Target $Url -Timeout $TimeoutSec
if ($main.Status) {
    $color = if ($main.Status -ge 500) { 'Red' } elseif ($main.Status -ge 400) { 'Yellow' } else { 'Green' }
    $serverText = if ($main.Server) { $main.Server } else { 'n/a' }
    Write-Host ("STATUS: {0} {1}   (Server: {2})" -f $main.Status, $main.Reason, $serverText) -ForegroundColor $color
} else {
    Write-Host ("NO HTTP RESPONSE: {0}" -f $main.Error) -ForegroundColor Red
}
Write-Host ''
Write-Host 'Response headers:'
$main.Headers.Keys | Sort-Object | ForEach-Object { Write-Host ("  {0}: {1}" -f $_, ($main.Headers[$_] -join ', ')) }

# --- 5. Who answered? --------------------------------------------------------
Write-Head 'Gateway fingerprint'
Write-Host (Get-GatewayFingerprint -Diag $main)

if ($main.Body) {
    $snippet = ($main.Body -replace '\s+', ' ').Trim()
    if ($snippet.Length -gt 300) { $snippet = $snippet.Substring(0, 300) + ' ...' }
    Write-Host ''
    Write-Host 'Body snippet:'
    Write-Host "  $snippet" -ForegroundColor DarkGray
    if ($snippet -match '(?i)502 Bad Gateway|504 Gateway Time|error code: 502|bad gateway') {
        Write-Host '  >> Generic gateway error page: the proxy never reached the app.' -ForegroundColor Yellow
    }
    if ($snippet -match '(?i)<!doctype html><meta charset="utf-8"><meta name=viewport') {
        Write-Host '  >> Minimal unquoted-meta error page: typical of Go-based proxies (e.g. Tailscale serve/funnel), not nginx or Cloudflare.' -ForegroundColor Yellow
    }
}

# --- 6. Optional: test the upstream directly --------------------------------
if ($OriginUrl) {
    Write-Head "Upstream test $OriginUrl"
    $origin = Get-HttpDiagnostic -Target $OriginUrl -Timeout $TimeoutSec
    if ($origin.Status) {
        $color = if ($origin.Status -ge 500) { 'Red' } elseif ($origin.Status -ge 400) { 'Yellow' } else { 'Green' }
        $serverText = if ($origin.Server) { $origin.Server } else { 'n/a' }
        Write-Host ("STATUS: {0} {1}   (Server: {2})" -f $origin.Status, $origin.Reason, $serverText) -ForegroundColor $color
    } else {
        Write-Host ("NO HTTP RESPONSE: {0}" -f $origin.Error) -ForegroundColor Red
    }
    Write-Host ''
    if (($origin.Status -ge 500 -or -not $origin.Status) -and ($main.Status -eq 502 -or $main.Status -eq 504)) {
        Write-Host 'VERDICT: upstream is down/unhealthy -> fix the application, not the proxy.' -ForegroundColor Red
    } elseif ($origin.Status -lt 400 -and ($main.Status -eq 502 -or $main.Status -eq 504)) {
        Write-Host 'VERDICT: upstream is healthy but the gateway cannot reach it -> proxy config, wrong upstream host/port, or network/ACL.' -ForegroundColor Yellow
    } else {
        Write-Host 'Compare both results above; the hop that breaks first is the culprit.' -ForegroundColor White
    }
}

# --- 7. Playbook -------------------------------------------------------------
Write-Head 'What a 502 means'
@(
    '502 Bad Gateway = the proxy accepted your request, forwarded it, and the upstream failed'
    'to answer (connection refused, reset, or dropped). It is almost never the client''s fault.'
    ''
    'Fix order (most common first):'
    '  1. Is the app running?                     docker ps | Get-Service <app> | Get-Process <app>'
    '  2. Is it listening on the IP the proxy dials?  netstat -ano | findstr LISTENING'
    '     (a service bound to 127.0.0.1 is unreachable for a proxy that dials the LAN/container IP)'
    '  3. Does the proxy upstream port match the app port?   (fix config, then reload)'
    '  4. Did the app crash-loop or get OOM-killed?  docker logs --tail 100 <container>'
    '  5. Slow/large requests are usually 504 Gateway Timeout - raise the proxy read timeout.'
) | ForEach-Object { Write-Host $_ }
