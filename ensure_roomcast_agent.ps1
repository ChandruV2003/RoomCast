param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [Parameter(Mandatory = $true)]
    [string]$HostSlug,

    [Parameter(Mandatory = $true)]
    [string]$Token,

    [Parameter(Mandatory = $true)]
    [string]$RepoPath,

    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [int]$PollIntervalSeconds = 1
)

$resolvedRepo = (Resolve-Path $RepoPath).Path
$resolvedPython = (Resolve-Path $PythonExecutable).Path
$agentScript = Join-Path $resolvedRepo "roomcast_agent.py"
$runtimeDir = Join-Path $resolvedRepo "runtime"
$lockPath = Join-Path $runtimeDir "$HostSlug-roomcast-agent.lock"

if (-not (Test-Path $agentScript)) {
    throw "roomcast_agent.py was not found in $resolvedRepo"
}

if (-not (Test-Path $resolvedPython)) {
    throw "Python executable was not found: $resolvedPython"
}

New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

$agentProcess = Get-CimInstance Win32_Process |
    Where-Object {
        ($_.Name -match "^python") -and
        $_.CommandLine -and
        $_.CommandLine.Contains("roomcast_agent.py") -and
        $_.CommandLine.Contains($HostSlug)
    } |
    Select-Object -First 1

if ($agentProcess) {
    exit 0
}

if (Test-Path $lockPath) {
    Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
}

Get-CimInstance Win32_Process |
    Where-Object {
        ($_.Name -match "ffmpeg") -and
        $_.CommandLine -and
        $_.CommandLine.Contains($HostSlug)
    } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {
        }
    }

$agentArgs = @(
    $agentScript,
    "--server-url", $ServerUrl,
    "--host-slug", $HostSlug,
    "--token", $Token,
    "--poll-interval", "$PollIntervalSeconds"
)

Start-Process `
    -FilePath $resolvedPython `
    -ArgumentList $agentArgs `
    -WorkingDirectory $resolvedRepo `
    -WindowStyle Hidden | Out-Null

exit 0
