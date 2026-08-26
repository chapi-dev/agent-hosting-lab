<#
.SYNOPSIS
    Deploys the whole lab: platform, network, apps, hosted agent, router.

.DESCRIPTION
    End-to-end and idempotent. Roughly 25 minutes on a clean subscription, most
    of it waiting for Foundry and the Container Apps environment.

    The ordering is not arbitrary - each step depends on outputs from the one
    before, and two of them exist only because of constraints discovered the
    hard way:

      1. Platform      Foundry, model, observability, identity, ACR, Cosmos
      2. Network       VNet + private endpoint. Required because governance
                       policy keeps Cosmos public access disabled and a
                       Container Apps environment has 160+ unstable outbound
                       IPs, so allow-listing is not an option.
      3. Environment   A VNet-injected Container Apps environment. Separate
                       from step 1 because VNet config is immutable - you
                       cannot add it to an existing environment.
      4. Image         Built in ACR. No local Docker needed.
      5. Self-hosted   The naive and hardened container apps.
      6. Hosted agent  azd, into the Foundry managed runtime.
      7. Router        Redeployed with the hosted endpoint wired in.

    Writes .env.lab, which every experiment reads.

.PARAMETER ResourceGroup
    Target resource group. Created if missing.

.PARAMETER SkipHostedAgent
    Skip the azd steps. Useful when iterating on the self-hosted side only.

.EXAMPLE
    ./scripts/deploy.ps1
    ./scripts/deploy.ps1 -ResourceGroup rg-my-lab -Location swedencentral
#>
[CmdletBinding()]
param(
    [string] $ResourceGroup = "rg-agenthost-lab",
    [string] $Location      = "swedencentral",
    [string] $Prefix        = "aghl",
    [string] $ImageTag      = "v1",
    [switch] $SkipHostedAgent
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step([string] $Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

# ---------------------------------------------------------------- preflight --

Write-Step "Checking prerequisites"
foreach ($tool in @("az", "azd")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool not found on PATH"
    }
}

$account = az account show -o json 2>$null | ConvertFrom-Json
if (-not $account) { throw "Not logged in. Run: az login" }
Write-Host "subscription: $($account.name) ($($account.id))"

$deployerPrincipalId = az ad signed-in-user show --query id -o tsv 2>$null
if (-not $deployerPrincipalId) {
    # Service principal / CI: fall back to the identity behind the token.
    $deployerPrincipalId = az account show --query user.name -o tsv
    $deployerPrincipalId = az ad sp show --id $deployerPrincipalId --query id -o tsv
}
Write-Host "deployer principal: $deployerPrincipalId"

# ------------------------------------------------------------- 1. platform ---

Write-Step "1/7 Resource group"
az group create -n $ResourceGroup -l $Location -o none

Write-Step "2/7 Platform (Foundry, model, observability, identity, ACR, Cosmos)"
$platform = az deployment group create `
    -g $ResourceGroup -f infra/main.bicep `
    -p prefix=$Prefix deployerPrincipalId=$deployerPrincipalId `
    --query properties.outputs -o json | ConvertFrom-Json

$foundryName    = $platform.foundryName.value
$projectEndpoint= $platform.projectEndpoint.value
$acrName        = $platform.acrName.value
$acrLoginServer = $platform.acrLoginServer.value
$identityId     = $platform.identityId.value
$identityClient = $platform.identityClientId.value
$cosmosEndpoint = $platform.cosmosEndpoint.value
$cosmosAccount  = $platform.cosmosAccountName.value
$appInsights    = $platform.appInsightsConnectionString.value
$modelName      = $platform.modelDeploymentName.value
$logCustomerId  = $platform.logAnalyticsCustomerId.value
$logWorkspaceId = $platform.logAnalyticsWorkspaceId.value

Write-Host "foundry: $foundryName"
Write-Host "project: $projectEndpoint"

# -------------------------------------------------------------- 2. network ---

Write-Step "3/7 Network (VNet, private endpoint, private DNS)"
Write-Host "Cosmos public access is policy-controlled; a private endpoint is the supported path." -ForegroundColor DarkGray
$network = az deployment group create `
    -g $ResourceGroup -f infra/network.bicep `
    -p prefix=$Prefix cosmosAccountName=$cosmosAccount `
    --query properties.outputs -o json | ConvertFrom-Json

$infraSubnetId = $network.infraSubnetId.value

# ---------------------------------------------------------- 3. environment ---

Write-Step "4/7 VNet-injected Container Apps environment"
Write-Host "Separate from the platform deployment: VNet config is immutable once an environment exists." -ForegroundColor DarkGray
$environment = az deployment group create `
    -g $ResourceGroup -f infra/environment-vnet.bicep `
    -p prefix=$Prefix `
       logAnalyticsCustomerId=$logCustomerId `
       logAnalyticsWorkspaceId=$logWorkspaceId `
       infrastructureSubnetId=$infraSubnetId `
    --query properties.outputs -o json | ConvertFrom-Json

$environmentId = $environment.environmentId.value

# ---------------------------------------------------------------- 4. image ---

Write-Step "5/7 Container image"
# --no-logs: without it the Azure CLI crashes with UnicodeEncodeError on a
# Windows console while streaming build output. The build itself still runs.
# Context is ./src because the Dockerfile's COPY paths are relative to it.
az acr build --registry $acrName `
    --image "agent-hosting-lab:$ImageTag" `
    --file src/Dockerfile ./src --no-logs -o none

# ----------------------------------------------------------- 5. self-hosted --

Write-Step "6/7 Self-hosted container apps"

# The router signs session handles with this key. Every replica must hold the
# same value or handles issued by one replica fail verification on another, and
# it has to survive the redeploy in step 7 or handles issued minutes ago stop
# working. So: reuse whatever the router already has, and only mint a new one
# on a genuinely first deployment.
$routerName = "$Prefix-hybrid-router"
$sessionSecret = az containerapp secret show -g $ResourceGroup -n $routerName `
    --secret-name session-secret --query value -o tsv 2>$null
if (-not $sessionSecret) {
    $sessionSecret = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    Write-Host "minted a new session signing key" -ForegroundColor DarkGray
} else {
    Write-Host "reusing the router's existing session signing key" -ForegroundColor DarkGray
}

$apps = az deployment group create `
    -g $ResourceGroup -f infra/apps.bicep `
    -p prefix=$Prefix `
       environmentId=$environmentId `
       acrLoginServer=$acrLoginServer `
       identityId=$identityId `
       identityClientId=$identityClient `
       projectEndpoint=$projectEndpoint `
       modelDeploymentName=$modelName `
       cosmosEndpoint=$cosmosEndpoint `
       appInsightsConnectionString=$appInsights `
       imageTag=$ImageTag `
       sessionSecret=$sessionSecret `
    --query properties.outputs -o json | ConvertFrom-Json

$naiveUrl    = $apps.naiveUrl.value
$hardenedUrl = $apps.hardenedUrl.value

# ---------------------------------------------------------- 6. hosted agent --

$hostedEndpoint = ""
if (-not $SkipHostedAgent) {
    Write-Step "7/7 Foundry hosted agent"

    & "$PSScriptRoot/prepare-hosted.ps1"

    azd ext install microsoft.foundry 2>$null | Out-Null

    if (-not (Test-Path "azure.yaml")) {
        Write-Host "azure.yaml missing - run 'azd ai agent init --src ./src/hosted --deploy-mode code --dep-resolution remote_build' and add the env: block from runbooks/01." -ForegroundColor Yellow
    } else {
        azd deploy trip-planner --no-prompt
        $hostedEndpoint = azd env get-value AGENT_TRIP_PLANNER_RESPONSES_ENDPOINT 2>$null
    }
}

# --------------------------------------------------------------- 7. router ---

$hybridUrl = ""
if ($hostedEndpoint) {
    Write-Step "Router (redeployed with the hosted agent endpoint)"
    $routed = az deployment group create `
        -g $ResourceGroup -f infra/apps.bicep `
        -p prefix=$Prefix `
           environmentId=$environmentId `
           acrLoginServer=$acrLoginServer `
           identityId=$identityId `
           identityClientId=$identityClient `
           projectEndpoint=$projectEndpoint `
           modelDeploymentName=$modelName `
           cosmosEndpoint=$cosmosEndpoint `
           appInsightsConnectionString=$appInsights `
           imageTag=$ImageTag `
           hostedAgentEndpoint=$hostedEndpoint `
           sessionSecret=$sessionSecret `
        --query properties.outputs -o json | ConvertFrom-Json
    $hybridUrl = $routed.hybridUrl.value
}

# -------------------------------------------------------------- write .env ---

Write-Step "Writing .env.lab"
$lines = @(
    "RESOURCE_GROUP=$ResourceGroup"
    "AZURE_LOCATION=$Location"
    "PROJECT_ENDPOINT=$projectEndpoint"
    "MODEL_DEPLOYMENT_NAME=$modelName"
    "COSMOS_ENDPOINT=$cosmosEndpoint"
    "ACR_NAME=$acrName"
    "NAIVE_URL=$naiveUrl"
    "HARDENED_URL=$hardenedUrl"
    "HOSTED_AGENT_ENDPOINT=$hostedEndpoint"
    "HYBRID_URL=$hybridUrl"
)
# WriteAllText rather than Set-Content: -Encoding utf8 emits a BOM, which the
# naive KEY=VALUE parsing in the experiments would read into the first key.
[System.IO.File]::WriteAllText(
    (Join-Path $repoRoot ".env.lab"),
    ($lines -join "`n") + "`n",
    (New-Object System.Text.UTF8Encoding($false))
)

Write-Host ""
Write-Host "Deployed." -ForegroundColor Green
$lines | ForEach-Object { Write-Host "  $_" }
Write-Host ""
Write-Host "Next: ./scripts/run-experiments.ps1" -ForegroundColor Cyan
