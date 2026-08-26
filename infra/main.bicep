// Platform for the agent hosting lab.
//
// Read this file as evidence, not boilerplate. Almost everything here exists to
// satisfy the SELF-HOSTED model. The hosted model needs the Foundry project and
// the model deployment - and nothing else in this file.
//
// The comment markers tell you which is which:
//   [BOTH]        needed by both hosting models
//   [SELF-HOSTED] needed only because we are self-hosting

targetScope = 'resourceGroup'

@description('Location for all lab resources. Must support Foundry hosted agents.')
param location string = resourceGroup().location

@description('Short name used as a prefix for every resource.')
param prefix string = 'aghl'

@description('Model to deploy and use for both hosting models.')
param modelName string = 'gpt-5.4-mini'

@description('Model version.')
param modelVersion string = '2026-03-17'

@description('Tokens-per-minute capacity, in thousands.')
param modelCapacity int = 100

@description('Object ID of the human or service principal running the deployment. Granted data-plane access to Foundry.')
param deployerPrincipalId string

var suffix = uniqueString(resourceGroup().id)
var acrName = replace('${prefix}acr${suffix}', '-', '')

// Built-in role definition IDs.
var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  azureAiUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  cosmosDataContributor: '00000000-0000-0000-0000-000000000002'
}

// ============================================================ [BOTH] Foundry

resource foundry 'Microsoft.CognitiveServices/accounts@2026-07-01' = {
  name: '${prefix}-foundry-${suffix}'
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    // Required for the v2 project model that hosted agents depend on.
    allowProjectManagement: true
    customSubDomainName: '${prefix}-foundry-${suffix}'
    publicNetworkAccess: 'Enabled'
    // Entra-only. Key auth is a liability in an agent platform.
    disableLocalAuth: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-07-01' = {
  parent: foundry
  name: '${prefix}-project'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Agent hosting lab'
    description: 'Self-hosted vs hosted agent comparison'
  }
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = {
  parent: foundry
  name: modelName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
  }
}

// ============================================================ [BOTH] observability
//
// Hosted agents get an Application Insights connection string injected into the
// container automatically. Self-hosted, you create the resource, wire the
// connection string into app settings, and remember to keep it wired.

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-log-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-appi-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    IngestionMode: 'LogAnalytics'
  }
}

// ============================================================ [SELF-HOSTED] identity
//
// A hosted agent is issued its own Entra agent identity at deploy time. Here we
// create one, and we own its lifecycle and its role assignments.

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-id-${suffix}'
  location: location
}

// ============================================================ [SELF-HOSTED] registry

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

// ============================================================ [SELF-HOSTED] session store
//
// This exists ONLY because a self-hosted agent that scales past one replica has
// nowhere durable to keep conversation state. The hosted model persists $HOME
// per session automatically, so this resource has no counterpart there.

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: '${prefix}-cosmos-${suffix}'
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    capabilities: [ { name: 'EnableServerless' } ]
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    disableLocalAuth: true
    // Explicit, because the default is not stable across API versions and
    // subscription policy - we hit exactly that: the account came up with
    // public access disabled and every container request returned Forbidden.
    //
    // An empty ipRules list with public access enabled means "any IP", which is
    // fine for a lab and wrong for production. The production answer is a
    // private endpoint plus VNet integration for the Container Apps
    // environment, because IP allow-listing is not viable here: a
    // consumption-profile environment has over 160 outbound addresses (run
    // `az containerapp show --query properties.outboundIpAddresses` to see for
    // yourself) and they are not stable.
    //
    // That is another piece of network engineering the self-hosted path owns
    // and the hosted path does not.
    publicNetworkAccess: 'Enabled'
    networkAclBypass: 'AzureServices'
    ipRules: []
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: 'agentstate'
  properties: {
    resource: { id: 'agentstate' }
  }
}

resource cosmosContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: cosmosDb
  name: 'sessions'
  properties: {
    resource: {
      id: 'sessions'
      partitionKey: {
        paths: [ '/sessionId' ]
        kind: 'Hash'
      }
      defaultTtl: 86400
    }
  }
}

// ============================================================ [SELF-HOSTED] compute

resource caEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-cae-${suffix}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// ============================================================ RBAC
//
// Every assignment below is one the hosted model would have made for you.

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, roles.acrPull)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource miOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, identity.id, roles.openAiUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource miAzureAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, identity.id, roles.azureAiUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAiUser)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Cosmos data-plane RBAC is a separate provider, not Azure RBAC. Easy to miss:
// Owner on the account does not grant you a single document read.
resource cosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, identity.id, roles.cosmosDataContributor)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${roles.cosmosDataContributor}'
    principalId: identity.properties.principalId
    scope: cosmos.id
  }
}

// The human running the lab needs data-plane access too. Subscription Owner is a
// control-plane role and grants no inference or Agent Service access.
resource deployerAzureAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, deployerPrincipalId, roles.azureAiUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAiUser)
    principalId: deployerPrincipalId
  }
}

resource deployerOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, deployerPrincipalId, roles.openAiUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser)
    principalId: deployerPrincipalId
  }
}

resource deployerCosmos 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, deployerPrincipalId, roles.cosmosDataContributor)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${roles.cosmosDataContributor}'
    principalId: deployerPrincipalId
    scope: cosmos.id
  }
}

// ============================================================ outputs

output foundryName string = foundry.name
output foundryEndpoint string = foundry.properties.endpoint
output projectName string = project.name
output projectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}'
output modelDeploymentName string = model.name

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output identityId string = identity.id
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
output environmentId string = caEnv.id
output environmentName string = caEnv.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosAccountName string = cosmos.name
output logAnalyticsCustomerId string = logs.properties.customerId
output logAnalyticsWorkspaceId string = logs.id
output location string = location
