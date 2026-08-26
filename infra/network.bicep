// Private networking for the self-hosted variants.
//
// This file exists because of a governance policy, and that story is the most
// useful thing in this repository.
//
// The lab subscription has an Azure Policy assignment, MCAPSGovDeployPolicies,
// containing CosmosDB_PublicNetwork_Modify with effect `modify`. Every write to
// a Cosmos account is rewritten to publicNetworkAccess: Disabled. You cannot
// opt out with Bicep, with `az cosmosdb update`, or with a raw REST PATCH - we
// tried all three and the property came back Disabled every time.
//
// So the self-hosted container cannot reach its own database over the internet,
// and IP allow-listing is not an option either: a consumption-profile Container
// Apps environment has 160+ outbound addresses and they are not stable.
//
// The only way through is real network engineering: a VNet, a delegated
// infrastructure subnet, a private endpoint, a private DNS zone, and a VNet
// link. That is what this file is. None of it is agent code. All of it is
// mandatory before the hardened self-hosted agent can read a single document.
//
// The Foundry hosted agent needed none of it. It reached its own session
// storage on the first deploy, because the storage is the platform's problem.
//
// A caveat worth stating plainly: a VNet-integrated environment is a workload
// profile environment, which has a baseline cost even when idle, unlike the
// consumption-only environment it replaces. Private networking is not free.

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Resource name prefix.')
param prefix string = 'aghl'

@description('Cosmos DB account name to place behind a private endpoint.')
param cosmosAccountName string

@description('Address space for the lab VNet.')
param vnetAddressPrefix string = '10.60.0.0/16'

// Container Apps requires a /23 or larger for a workload profile environment.
// Get this wrong and the environment fails to create with an error that does
// not mention subnet size.
var infraSubnetPrefix = '10.60.0.0/23'
var privateEndpointSubnetPrefix = '10.60.4.0/24'

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${prefix}-vnet'
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [ vnetAddressPrefix ]
    }
    subnets: [
      {
        name: 'infrastructure'
        properties: {
          addressPrefix: infraSubnetPrefix
          delegations: [
            {
              name: 'containerapps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosAccountName
}

resource cosmosPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${prefix}-cosmos-pe'
  location: location
  properties: {
    subnet: {
      id: '${vnet.id}/subnets/private-endpoints'
    }
    privateLinkServiceConnections: [
      {
        name: 'cosmos'
        properties: {
          privateLinkServiceId: cosmos.id
          groupIds: [ 'Sql' ]
        }
      }
    ]
  }
}

// Without this zone the container resolves the Cosmos hostname to its public
// address, the firewall rejects it, and you get the same Forbidden as before -
// with a private endpoint sitting there doing nothing. DNS is the step people
// forget.
resource privateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.documents.azure.com'
  location: 'global'
}

resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: privateDnsZone
  name: '${prefix}-link'
  location: 'global'
  properties: {
    virtualNetwork: {
      id: vnet.id
    }
    registrationEnabled: false
  }
}

resource dnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: cosmosPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'documents'
        properties: {
          privateDnsZoneId: privateDnsZone.id
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output infraSubnetId string = '${vnet.id}/subnets/infrastructure'
output privateEndpointSubnetId string = '${vnet.id}/subnets/private-endpoints'
output privateDnsZoneId string = privateDnsZone.id
