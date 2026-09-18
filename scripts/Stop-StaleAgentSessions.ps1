[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "Medium")]
param(
    [string] $AgentName,
    [string] $Environment,
    [ValidateRange(1, 1000)]
    [int] $PageSize = 100
)

$ErrorActionPreference = "Stop"

function Invoke-AzdJson {
    param([Parameter(Mandatory)][string[]] $Arguments)

    $output = & azd @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "azd $($Arguments -join ' ') failed:`n$($output -join [Environment]::NewLine)"
    }

    try {
        return ($output -join [Environment]::NewLine) | ConvertFrom-Json
    }
    catch {
        throw "azd returned invalid JSON:`n$($output -join [Environment]::NewLine)"
    }
}

function Get-OptionalArguments {
    $arguments = @()
    if ($AgentName) {
        $arguments += "--agent-name", $AgentName
    }
    if ($Environment) {
        $arguments += "--environment", $Environment
    }
    return $arguments
}

$showArguments = @("ai", "agent", "show")
if ($AgentName) {
    $showArguments += $AgentName
}
if ($Environment) {
    $showArguments += "--environment", $Environment
}
$showArguments += "--output", "json", "--no-prompt"
$agent = Invoke-AzdJson $showArguments
$currentVersion = [string] $agent.version
if (-not $currentVersion) {
    throw "The current deployed agent version was not present in 'azd ai agent show'."
}

$sessions = @()
$paginationToken = $null
do {
    $listArguments = @("ai", "agent", "sessions", "list", "--limit", $PageSize)
    $listArguments += Get-OptionalArguments
    if ($paginationToken) {
        $listArguments += "--pagination-token", $paginationToken
    }
    $listArguments += "--output", "json", "--no-prompt"

    $page = Invoke-AzdJson $listArguments
    $sessions += @($page.data)
    $paginationToken = @(
        $page.pagination_token,
        $page.next_pagination_token,
        $page.continuation_token
    ) | Where-Object { $_ } | Select-Object -First 1
} while ($paginationToken)

$staleSessions = @(
    $sessions | Where-Object {
        [string] $_.version_indicator.agent_version -ne $currentVersion -and
        [string] $_.status -ne "stopped"
    }
)

if ($staleSessions.Count -eq 0) {
    Write-Host "No running sessions are pinned to a version older than $currentVersion."
    return
}

Write-Host "Current agent version: $currentVersion"
$staleSessions |
    Select-Object `
        @{Name = "SessionId"; Expression = { $_.agent_session_id }},
        @{Name = "Version"; Expression = { $_.version_indicator.agent_version }},
        Status,
        @{Name = "LastAccessed"; Expression = {
            [DateTimeOffset]::FromUnixTimeSeconds($_.last_accessed_at).ToLocalTime()
        }} |
    Format-Table -AutoSize

foreach ($session in $staleSessions) {
    $sessionId = [string] $session.agent_session_id
    $sessionVersion = [string] $session.version_indicator.agent_version
    if (-not $PSCmdlet.ShouldProcess(
        "session $sessionId (version $sessionVersion)",
        "Stop"
    )) {
        continue
    }

    $stopArguments = @("ai", "agent", "sessions", "stop", $sessionId)
    $stopArguments += Get-OptionalArguments
    $stopArguments += "--no-prompt"
    & azd @stopArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to stop session $sessionId."
    }
}