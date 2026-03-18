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
    [string]$PythonSelector = "-3.12"
)

$resolvedRepo = (Resolve-Path $RepoPath).Path
$agentScript = Join-Path $resolvedRepo "roomcast_agent.py"
if (-not (Test-Path $agentScript)) {
    throw "roomcast_agent.py was not found in $resolvedRepo"
}

$command = @(
    "Set-Location '$resolvedRepo'"
    "& $PythonLauncher $PythonSelector '$agentScript' --server-url '$ServerUrl' --host-slug '$HostSlug' --token '$Token'"
) -join "; "

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -Command `$ErrorActionPreference = 'Stop'; $command"

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "WebCall source agent for $HostSlug" `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Author
