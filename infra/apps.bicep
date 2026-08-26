// Container Apps for the self-hosted variants.
//
// Three apps, one image, three configurations. The image is identical in all
// three - what differs is how much supporting machinery each one is given, and
// that machinery is precisely the cost of self-hosting.
//
//   selfhosted-naive     state on local disk, 2 replicas, no affinity
//                        -> loses session state, on purpose
//   selfhosted-hardened  state in Cosmos, 2 replicas
//                        -> correct, at the price of everything in CosmosStore
//   hybrid-router        thin router that forwards to the hosted agent
//                        -> the recommended pattern
//
// Read the `naive` block and note that nothing in it is obviously wrong. That
// is the trap: the bug is not in any single setting, it is in the combination
// of replicas > 1 and state that assumes there is only one.

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Resource name prefix.')
param prefix string = 'aghl'

@description('Container Apps environment resource id.')
param environmentId string

@description('ACR login server, e.g. myacr.azurecr.io')
param acrLoginServer string

@description('User-assigned managed identity resource id.')
param identityId string

@description('Client id of the user-assigned managed identity.')
param identityClientId string

@description('Foundry project endpoint.')
param projectEndpoint string

@description('Model deployment name.')
param modelDeploymentName string

@description('Cosmos DB account endpoint.')
param cosmosEndpoint string

@description('Application Insights connection string.')
param appInsightsConnectionString string

@description('Container image tag.')
param imageTag string = 'v1'

@description('Endpoint of the Foundry hosted agent, for the hybrid router. Empty until the hosted agent exists.')
param hostedAgentEndpoint string = ''

@description('''
Secret the router signs session handles with. Must be the same on every replica:
a handle issued by one replica is verified by another, so a per-instance value
would reject valid sessions at random. Rotating it invalidates live sessions,
which costs users one cold start and nothing else.
''')
@secure()
param sessionSecret string = newGuid()

var image = '${acrLoginServer}/agent-hosting-lab:${imageTag}'

// Environment variables every variant needs. Note that under the hosted model
// none of these are written by hand: the platform injects the project endpoint
// and the App Insights connection string into the container for you.
var commonEnv = [
  {
    name: 'AZURE_AI_PROJECT_ENDPOINT'
    value: projectEndpoint
  }
  {
    name: 'MODEL_DEPLOYMENT_NAME'
    value: modelDeploymentName
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: identityClientId
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsightsConnectionString
  }
]

// ---------------------------------------------------------------------------
// A1: the naive self-hosted agent.
//
// Two replicas, state on local disk. Each replica has its own filesystem, so
// turn 1 and turn 2 of the same conversation can land on different replicas and
// the second one has never heard of the first. This is not a contrived bug -
// it is the single most common way a working prototype breaks the first time it
// is scaled past one instance.
// ---------------------------------------------------------------------------
resource naive 'Microsoft.App/containerApps@2025-01-01' = {
  name: '${prefix}-selfhosted-naive'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        // No session affinity. With affinity on, the naive variant would appear
        // to work, which is worse: the bug would surface later, under load, in
        // production, after a scale event.
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: image
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: concat(commonEnv, [
            {
              name: 'STATE_BACKEND'
              value: 'disk'
            }
            {
              name: 'VARIANT'
              value: 'selfhosted-naive'
            }
          ])
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 2
        maxReplicas: 2
      }
    }
  }
}

// ---------------------------------------------------------------------------
// A2: the hardened self-hosted agent.
//
// Same image, same replica count. The only change is STATE_BACKEND=cosmos, and
// that one environment variable stands for: a Cosmos account, a database, a
// container, a partition key, a TTL policy, a data-plane role assignment, an
// async SDK client with its own lifecycle, and 40 lines of CosmosStore. All to
// reach the behaviour the hosted model provides with no configuration at all.
// ---------------------------------------------------------------------------
resource hardened 'Microsoft.App/containerApps@2025-01-01' = {
  name: '${prefix}-selfhosted-hardened'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: image
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: concat(commonEnv, [
            {
              name: 'STATE_BACKEND'
              value: 'cosmos'
            }
            {
              name: 'COSMOS_ENDPOINT'
              value: cosmosEndpoint
            }
            {
              name: 'VARIANT'
              value: 'selfhosted-hardened'
            }
          ])
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 2
        maxReplicas: 2
      }
    }
  }
}

// ---------------------------------------------------------------------------
// C: the hybrid router.
//
// Self-hosted, because routing is business logic and business logic is where
// self-hosting earns its keep: custom authorization, header forwarding, private
// networking, anything the platform does not model. It holds no session state
// of its own - it forwards to the hosted agent, which holds the state. This is
// the shape of the recommendation: own the orchestration, rent the runtime.
// ---------------------------------------------------------------------------
resource hybrid 'Microsoft.App/containerApps@2025-01-01' = if (!empty(hostedAgentEndpoint)) {
  name: '${prefix}-hybrid-router'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          identity: identityId
        }
      ]
      secrets: [
        {
          name: 'session-secret'
          value: sessionSecret
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'router'
          image: image
          command: [
            'uvicorn'
          ]
          args: [
            'selfhosted.router:app'
            '--host'
            '0.0.0.0'
            '--port'
            '8000'
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(commonEnv, [
            {
              name: 'HOSTED_AGENT_ENDPOINT'
              value: hostedAgentEndpoint
            }
            {
              name: 'SESSION_SECRET'
              secretRef: 'session-secret'
            }
            {
              name: 'VARIANT'
              value: 'hybrid-router'
            }
          ])
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        // The router is stateless, so it scales without ceremony. That is the
        // point of pushing state down into the hosted agent.
        minReplicas: 2
        maxReplicas: 5
      }
    }
  }
}

output naiveUrl string = 'https://${naive.properties.configuration.ingress.fqdn}'
output hardenedUrl string = 'https://${hardened.properties.configuration.ingress.fqdn}'
output hybridUrl string = empty(hostedAgentEndpoint) ? '' : 'https://${hybrid.properties.configuration.ingress.fqdn}'
