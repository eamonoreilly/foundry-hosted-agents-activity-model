$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "..\Grant-AgentProjectAccess.ps1"
$roleId = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
$principalId = "11111111-1111-1111-1111-111111111111"
$projectId = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/account/projects/project"

function Assert-Equal {
    param($Expected, $Actual, [string] $Message)
    if ($Expected -ne $Actual) {
        throw "$Message Expected '$Expected', got '$Actual'."
    }
}

function Invoke-TestCase {
    param([int] $ExistingCount)

    $global:MockAzCalls = @()
    $global:MockExistingCount = $ExistingCount
    function global:az {
        $global:MockAzCalls += [pscustomobject]@{ Arguments = @($args) }
        $global:LASTEXITCODE = 0
        if ($args[2] -eq "list") {
            return $global:MockExistingCount
        }
    }

    try {
        & $scriptPath -PrincipalId $principalId -ProjectId $projectId
        return @($global:MockAzCalls)
    }
    finally {
        Remove-Item Function:\az
        Remove-Variable MockAzCalls -Scope Global
        Remove-Variable MockExistingCount -Scope Global
    }
}

$createCalls = Invoke-TestCase -ExistingCount 0
Assert-Equal 2 $createCalls.Count "A missing assignment should query and create."
Assert-Equal "create" $createCalls[1].Arguments[2] "The second Azure CLI call should create the assignment."
Assert-Equal $principalId $createCalls[1].Arguments[4] "The create call should use the agent object ID."
Assert-Equal "ServicePrincipal" $createCalls[1].Arguments[6] "The create call should identify the principal type."
Assert-Equal $roleId $createCalls[1].Arguments[8] "The create call should use Foundry User."
Assert-Equal $projectId $createCalls[1].Arguments[10] "The create call should use the exact project scope."

$existingCalls = Invoke-TestCase -ExistingCount 1
Assert-Equal 1 $existingCalls.Count "An existing assignment should only be queried."
Assert-Equal "list" $existingCalls[0].Arguments[2] "The idempotency check should list assignments."

$previousProjectId = $env:AZURE_AI_PROJECT_ID
$env:AZURE_AI_PROJECT_ID = $null
try {
    try {
        & $scriptPath -PrincipalId $principalId
        throw "Missing project ID did not fail."
    }
    catch {
        if ($_.Exception.Message -notlike "AZURE_AI_PROJECT_ID is unavailable*") {
            throw
        }
    }
}
finally {
    $env:AZURE_AI_PROJECT_ID = $previousProjectId
}

Write-Host "Grant-AgentProjectAccess tests passed."