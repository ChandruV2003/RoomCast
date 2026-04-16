param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [Parameter(Mandatory = $true)]
    [string]$HostSlug,

    [Parameter(Mandatory = $true)]
    [string]$Token,

    [string]$RepoPath = (Split-Path -Parent $PSCommandPath),
[string]$TaskName = "WebCall Source Agent",
[string]$PythonLauncher = "py",
[string]$PythonSelector = "",
[string]$PythonExecutable = "",
[int]$PollIntervalSeconds = 1,
[switch]$EnforcePowerProfile = $true
)

$resolvedRepo = (Resolve-Path $RepoPath).Path
$agentScript = Join-Path $resolvedRepo "roomcast_agent.py"
if (-not (Test-Path $agentScript)) {
    throw "roomcast_agent.py was not found in $resolvedRepo"
}

$resolvedPythonSelector = $PythonSelector
if (-not $resolvedPythonSelector) {
    foreach ($candidate in @("-3.13", "-3.12", "-3.11", "-3")) {
        & $PythonLauncher $candidate -c "import sys" *> $null
        if ($LASTEXITCODE -eq 0) {
            $resolvedPythonSelector = $candidate
            break
        }
    }
}

if (-not $resolvedPythonSelector) {
    throw "Could not find a usable Python launcher target via '$PythonLauncher'. Pass -PythonSelector explicitly."
}

$resolvedPythonExecutable = $PythonExecutable
if (-not $resolvedPythonExecutable) {
    $resolvedPythonExecutable = (& $PythonLauncher $resolvedPythonSelector -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1).Trim()
}

if (-not $resolvedPythonExecutable) {
    throw "Could not resolve a Python executable via '$PythonLauncher $resolvedPythonSelector'. Pass -PythonExecutable explicitly."
}

if (-not (Test-Path $resolvedPythonExecutable)) {
    throw "Resolved Python executable was not found: $resolvedPythonExecutable"
}

$agentArgs = @(
    "'$agentScript'"
    "--server-url '$ServerUrl'"
    "--host-slug '$HostSlug'"
    "--token '$Token'"
    "--poll-interval '$PollIntervalSeconds'"
) -join " "

$runtimeDir = Join-Path $resolvedRepo "runtime"
$lockPath = Join-Path $runtimeDir "$HostSlug-roomcast-agent.lock"

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -Command `$ErrorActionPreference = 'Stop'; Set-Location '$resolvedRepo'; & '$resolvedPythonExecutable' $agentArgs"

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$guardianTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -WakeToRun `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

if ($EnforcePowerProfile) {
    $powerCommands = @(
        @("/change", "standby-timeout-ac", "0"),
        @("/change", "standby-timeout-dc", "0"),
        @("/change", "hibernate-timeout-ac", "0"),
        @("/change", "hibernate-timeout-dc", "0"),
        @("/setacvalueindex", "scheme_current", "sub_buttons", "lidaction", "0"),
        @("/setdcvalueindex", "scheme_current", "sub_buttons", "lidaction", "0"),
        @("/setacvalueindex", "scheme_current", "sub_sleep", "hybridsleep", "0"),
        @("/setdcvalueindex", "scheme_current", "sub_sleep", "hybridsleep", "0"),
        @("/setacvalueindex", "scheme_current", "sub_sleep", "rtcwake", "1"),
        @("/setdcvalueindex", "scheme_current", "sub_sleep", "rtcwake", "1"),
        @("/setacvalueindex", "scheme_current", "2a737441-1930-4402-8d77-b2bebba308a3", "48e6b7a6-50f5-4782-a5d4-53bb8f07e226", "0"),
        @("/setdcvalueindex", "scheme_current", "2a737441-1930-4402-8d77-b2bebba308a3", "48e6b7a6-50f5-4782-a5d4-53bb8f07e226", "0"),
        @("/setactive", "scheme_current")
    )

    foreach ($powerCommand in $powerCommands) {
        try {
            & powercfg @powerCommand *> $null
        } catch {
            Write-Warning "powercfg $($powerCommand -join ' ') failed: $($_.Exception.Message)"
        }
    }
}

try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
} catch {
}

Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match "roomcast_agent.py" } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {
        }
    }

Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match "ffmpeg" -and $_.CommandLine -match [Regex]::Escape($HostSlug) } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {
        }
    }

if (Test-Path $lockPath) {
    Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($logonTrigger, $startupTrigger, $guardianTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description "WebCall source agent for $HostSlug" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Author
