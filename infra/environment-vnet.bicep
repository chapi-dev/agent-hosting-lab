// A second Container Apps environment, this one inside the VNet.
//
// This file exists because VNet configuration is immutable on a Container Apps
// environment. You cannot add a VNet to the environment you already have; you
// create a new one, redeploy every app into it, and retire the old one. Worth
// knowing before you build the first environment, because by the time you find
// out you already have apps running in the wrong place.
//
// The environment also switches from consumption-only to workload profiles,
// which VNet integration requires. That changes the cost model: a workload
// profile environment can host dedicated profiles, which bill per provisioned
// instance whether or not anything is running. This one declares only the
// Consumption profile, so it keeps consumption billing - but the option to
// accidentally provision a dedicated profile is now yours to manage.
//
// The Foundry hosted agent needed none of this. Its sandboxes are isolated by
// the platform and its storage is reached without a subnet, a private
// endpoint, a DNS zone, or a second environment to migrate into.

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Resource name prefix.')
param prefix string = 'aghl'

@description('Log Analytics workspace customer id.')
param logAnalyticsCustomerId string

@description('Log Analytics workspace resource id, used to read the shared key.')
param logAnalyticsWorkspaceId string

@description('Resource id of the delegated infrastructure subnet.')
param infrastructureSubnetId string

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: last(split(logAnalyticsWorkspaceId, '/'))
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: '${prefix}-cae-vnet'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      // Ingress stays public so the experiments can reach it from a laptop.
      // A production deployment would set this true and put Front Door or
      // Application Gateway in front - more infrastructure, same direction.
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

output environmentId string = environment.id
output environmentName string = environment.name
output staticIp string = environment.properties.staticIp
