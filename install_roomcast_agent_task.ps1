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
$ensureScript = Join-Path $resolvedRepo "ensure_roomcast_agent.ps1"
if (-not (Test-Path $agentScript)) {
    throw "roomcast_agent.py was not found in $resolvedRepo"
}
if (-not (Test-Path $ensureScript)) {
    throw "ensure_roomcast_agent.ps1 was not found in $resolvedRepo"
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

$ensureArgs = @(
    "& '$ensureScript'"
    "-ServerUrl '$ServerUrl'"
    "-HostSlug '$HostSlug'"
    "-Token '$Token'"
    "-RepoPath '$resolvedRepo'"
    "-PythonExecutable '$resolvedPythonExecutable'"
    "-PollIntervalSeconds $PollIntervalSeconds"
) -join " "

$encodedEnsureCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($ensureArgs))
$powershellTaskArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $encodedEnsureCommand"

$runtimeDir = Join-Path $resolvedRepo "runtime"
$lockPath = Join-Path $runtimeDir "$HostSlug-roomcast-agent.lock"

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $powershellTaskArguments

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
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

$guardianTaskName = "$TaskName Guardian"
$guardianAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $powershellTaskArguments
$guardianSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -WakeToRun `
    -MultipleInstances IgnoreNew

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
try {
    Stop-ScheduledTask -TaskName $guardianTaskName -ErrorAction SilentlyContinue | Out-Null
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
    -Trigger @($logonTrigger, $startupTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description "WebCall source agent for $HostSlug" `
    -Force | Out-Null

$guardianStartBoundary = [DateTime]::Now.AddMinutes(1).ToString("s")
$guardianXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>WebCall guardian for $HostSlug</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$guardianStartBoundary</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
      <Repetition>
        <Interval>PT1M</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$env:USERNAME</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT72H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$powershellTaskArguments</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$guardianXmlPath = Join-Path $env:TEMP "$($HostSlug)-roomcast-guardian.xml"
$guardianXml | Out-File -FilePath $guardianXmlPath -Encoding Unicode -Force
Register-ScheduledTask -TaskName $guardianTaskName -Xml (Get-Content -Raw $guardianXmlPath) -Force | Out-Null
Remove-Item $guardianXmlPath -Force -ErrorAction SilentlyContinue

Start-ScheduledTask -TaskName $TaskName
Start-ScheduledTask -TaskName $guardianTaskName

Get-ScheduledTask -TaskName $TaskName, $guardianTaskName | Select-Object TaskName, State, Author
