[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "Medium")]
param(
    [string] $PrincipalId,
    [string] $ProjectId
)

$ErrorActionPreference = "Stop"
$FoundryUserRoleId = "53ca6127-db72-4b80-b1b0-d745d6d5456d"

if ([string]::IsNullOrWhiteSpace($PrincipalId)) {
    $principalIds = @(
        Get-ChildItem Env: |
            Where-Object {
                $_.Name -match '^AGENT_.*_INSTANCE_IDENTITY_PRINCIPAL_ID$' -and
                -not [string]::IsNullOrWhiteSpace($_.Value)
            } |
            Select-Object -ExpandProperty Value -Unique
    )

    if ($principalIds.Count -ne 1) {
        throw "Expected one deployed agent identity, but found $($principalIds.Count). Pass -PrincipalId explicitly."
    }
    $PrincipalId = $principalIds[0]
}

if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    $ProjectId = $env:AZURE_AI_PROJECT_ID
}
if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    throw "AZURE_AI_PROJECT_ID is unavailable. Run this script through the azd postdeploy hook or pass -ProjectId."
}

$existingCount = & az role assignment list `
    --role $FoundryUserRoleId `
    --scope $ProjectId `
    --query "[?principalId=='$PrincipalId'] | length(@)" `
    --output tsv `
    --only-show-errors
if ($LASTEXITCODE -ne 0) {
    throw "Failed to inspect Foundry User access for agent identity $PrincipalId."
}

if ([int] $existingCount -gt 0) {
    Write-Host "Agent identity already has Foundry User at the project scope."
    return
}

if (-not $PSCmdlet.ShouldProcess(
    "agent identity $PrincipalId at $ProjectId",
    "Assign Foundry User"
)) {
    return
}

& az role assignment create `
    --assignee-object-id $PrincipalId `
    --assignee-principal-type ServicePrincipal `
    --role $FoundryUserRoleId `
    --scope $ProjectId `
    --output none `
    --only-show-errors
if ($LASTEXITCODE -ne 0) {
    throw "Failed to assign Foundry User to agent identity $PrincipalId."
}

Write-Host "Assigned Foundry User to the agent identity at the project scope."