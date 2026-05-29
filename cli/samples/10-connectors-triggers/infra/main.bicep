// Connector-gateway triggers — minimal azd infra.
//
// Shared by both language flavors of this sample:
//   * cli/samples/10-connectors-triggers/      (azure.yaml here, uses local infra/)
//   * python/samples/10-connectors-triggers/   (azure.yaml there points infra.path
//                                               at this folder)
//
// All this Bicep template owns is the resource group. Everything else
// (connector gateway + MI, RBAC, office365 connection, access policies,
// OAuth consent, runtime URL) is provisioned by each tree's
// postprovision hook, which delegates to the language-appropriate
// setup script (setup.sh or setup.py).
//
// The sample assumes a sandbox group with the right RBAC already exists
// (see README "Prerequisites"). The postprovision hook fails fast if
// ACA_SANDBOX_GROUP is not set.
//
// Why no Bicep for the rest?
//   * The connector-gateway resource types
//     (Microsoft.Web/connectorGateways*@2026-05-01-preview) and the
//     sandbox group type (Microsoft.App/sandboxGroups@2026-02-01-preview)
//     don't have types published yet — Bicep emits BCP081 warnings on
//     every property and gives zero validation.
//   * OAuth consent for the office365 connection is inherently
//     interactive and can't be expressed in ARM.
//   * The existing setup scripts are already idempotent and battle-tested;
//     re-using them from the hook means the imperative path and the azd
//     path stay in sync.

targetScope = 'subscription'

@minLength(1)
@description('Name of the azd environment. Tagged on the RG so azd down knows what to delete.')
param environmentName string

@minLength(1)
@description('Primary region. Used as the RG location AND as the sandbox-group region. Constrained to the region list returned by Microsoft.App/sandboxGroups so this sample fails fast at deployment time rather than mid-hook. The connector gateway defaults to the same region; override via ACA_CONNECTOR_GATEWAY_REGION.')
@allowed([
  'australiaeast'
  'brazilsouth'
  'canadacentral'
  'canadaeast'
  'centralus'
  'eastasia'
  'eastus2'
  'francecentral'
  'germanywestcentral'
  'japaneast'
  'koreacentral'
  'mexicocentral'
  'northcentralus'
  'northeurope'
  'norwayeast'
  'polandcentral'
  'southafricanorth'
  'southeastasia'
  'southindia'
  'spaincentral'
  'swedencentral'
  'switzerlandnorth'
  'uksouth'
  'westcentralus'
  'westus'
  'westus2'
  'westus3'
])
param location string

@description('Name of the resource group to create. Defaults to ai-apps-samples-rg so it matches the existing scripted baseline; the postprovision hook discovers the same group via .env.')
param resourceGroupName string = 'ai-apps-samples-rg'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    'azd-env-name': environmentName
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output ACA_RESOURCE_GROUP string = rg.name
