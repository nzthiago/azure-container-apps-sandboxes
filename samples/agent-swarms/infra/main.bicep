targetScope = 'resourceGroup'

@description('Short prefix used in deterministic resource names.')
@minLength(5)
param namePrefix string = 'swarm'

@description('Deployment environment name used by azd.')
@minLength(1)
param environmentName string

@description('Azure region for all resources. ACA sandbox groups are in preview; verify provider registration and regional availability before running `azd up`.')
param location string = resourceGroup().location

@description('Optional tags applied to all resources.')
param tags object = {}

@description('azd service name used to map the deployed image onto the provisioned Container App.')
param serviceName string = 'swarm-api'

@description('Placeholder image used when the Container App is first created. `azd deploy` replaces it with the freshly built image from ACR.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('FastAPI container port.')
param targetPort int = 8000

@description('CPU requested by the Container App.')
param containerCpu string = '0.5'

@description('Memory requested by the Container App.')
param containerMemory string = '1.0Gi'

@description('Optional explicit sandbox group name. Leave empty to derive one from the environment name.')
param sandboxGroupName string = ''

@description('Optional private sandbox DiskId surfaced to the runtime and forwarded to ACA sandbox creation.')
param sandboxDiskId string = ''

@description('Default planner model surfaced to the app.')
param defaultPlannerModel string = 'gpt-4.1'

@description('Default worker model surfaced to the app.')
param defaultWorkerModel string = 'gpt-4.1'

@description('Default reviewer model surfaced to the app.')
param defaultReviewerModel string = 'gpt-4.1'

@minValue(60)
@description('Default sandbox idle timeout in seconds surfaced to the app.')
param sandboxIdleTimeoutSeconds int = 300

@description('Whether failed sandboxes are retained by default.')
param keepFailedSandboxes bool = false

@description('Optional explicit Durable Task Scheduler name. Leave empty to derive one from the environment name.')
param dtsSchedulerName string = ''

@description('Optional explicit Durable Task task hub name. Leave empty to derive one from the environment name.')
param dtsTaskHubName string = ''

@allowed([
  'Consumption'
  'Dedicated'
])
@description('Durable Task Scheduler SKU. Consumption keeps the public sample closer to a fresh-environment default.')
param dtsSkuName string = 'Consumption'

@minValue(1)
@description('Durable Task Scheduler SKU capacity. Only applied when the DTS SKU is Dedicated.')
param dtsSkuCapacity int = 1

@allowed([
  'Enabled'
  'Disabled'
])
@description('Public network access for Durable Task Scheduler. Keep enabled for the default external sample unless ACA egress is pinned.')
param dtsPublicNetworkAccess string = 'Enabled'

@description('Allowed ingress CIDRs for Durable Task Scheduler. Defaults to open access because ACA egress is not pinned in the baseline sample.')
param dtsIpAllowlist array = [
  '0.0.0.0/0'
]

@description('Review-ready snapshot of the runtime settings injected into the Container App.')
param runtimeContractSettings array = [
  'SWARM_APP_BASE_URL'
  'DTS_CONNECTION_STRING'
  'AZURE_SUBSCRIPTION_ID'
  'AZURE_RESOURCE_GROUP'
  'AZURE_LOCATION'
  'SWARM_COPILOT_RUNTIME'
  'SWARM_COPILOT_AUTH_MODE'
  'SWARM_COPILOT_TOKEN_ENV_VAR'
  'SWARM_COPILOT_USE_LOGGED_IN_USER'
  'SWARM_STORAGE_ACCOUNT_URL'
  'SWARM_SANDBOX_GROUP_NAME'
  'SWARM_SANDBOX_DISK_ID'
]

var normalizedPrefix = toLower(replace(replace(namePrefix, '_', '-'), '.', '-'))
var normalizedEnvironment = toLower(replace(replace(environmentName, '_', '-'), '.', '-'))
var uniqueSuffix = take(uniqueString(subscription().subscriptionId, resourceGroup().id, normalizedPrefix, normalizedEnvironment, location), 6)
var constrainedBaseName = take('${normalizedPrefix}-${normalizedEnvironment}-${uniqueSuffix}', 24)
var logAnalyticsWorkspaceName = take('${constrainedBaseName}-law', 63)
var appInsightsName = take('${constrainedBaseName}-appi', 64)
var containerRegistryNameSeed = toLower(replace('${uniqueSuffix}acr00${normalizedPrefix}${normalizedEnvironment}', '-', ''))
var containerRegistryName = take(containerRegistryNameSeed, 50)
var managedIdentityName = take('${constrainedBaseName}-mi', 128)
var storageAccountNameSeed = toLower(replace('${uniqueSuffix}st000${normalizedPrefix}${normalizedEnvironment}', '-', ''))
var storageAccountName = take(storageAccountNameSeed, 24)
var containerAppsEnvironmentName = take('${constrainedBaseName}-cae', 32)
var containerAppName = take('${constrainedBaseName}-api', 32)
var effectiveSandboxGroupName = empty(sandboxGroupName) ? take('${constrainedBaseName}-sandbox', 63) : sandboxGroupName
var effectiveDtsSchedulerName = empty(dtsSchedulerName) ? take('${constrainedBaseName}-dts', 63) : dtsSchedulerName
var effectiveDtsTaskHubName = empty(dtsTaskHubName) ? take('${constrainedBaseName}-hub', 63) : dtsTaskHubName
var dtsSku = dtsSkuName == 'Consumption'
  ? {
      name: dtsSkuName
    }
  : {
      name: dtsSkuName
      capacity: dtsSkuCapacity
    }
var resourceTags = union(tags, {
  'azd-env-name': environmentName
  'azd-service-name': serviceName
})
var swarmAppBaseUrl = 'https://${containerAppName}.${containerAppsEnvironment.properties.defaultDomain}'
var dtsConnectionString = 'Endpoint=${dtsScheduler.properties.endpoint};Authentication=ManagedIdentity;ClientID=${managedIdentity.properties.clientId};TaskHub=${dtsTaskHub.name}'
var appEnvironmentVariables = [
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsights.properties.ConnectionString
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: managedIdentity.properties.clientId
  }
  {
    name: 'AZURE_LOCATION'
    value: location
  }
  {
    name: 'AZURE_RESOURCE_GROUP'
    value: resourceGroup().name
  }
  {
    name: 'AZURE_SUBSCRIPTION_ID'
    value: subscription().subscriptionId
  }
  {
    name: 'DTS_CONNECTION_STRING'
    value: dtsConnectionString
  }
  {
    name: 'SWARM_APP_BASE_URL'
    value: swarmAppBaseUrl
  }
  {
    name: 'SWARM_COPILOT_AUTH_MODE'
    value: 'run-scoped-pat'
  }
  {
    name: 'SWARM_COPILOT_RUNTIME'
    value: 'github-copilot-sdk'
  }
  {
    name: 'SWARM_COPILOT_TOKEN_ENV_VAR'
    value: 'GH_TOKEN'
  }
  {
    name: 'SWARM_COPILOT_USE_LOGGED_IN_USER'
    value: 'false'
  }
  {
    name: 'SWARM_PLANNER_MODEL'
    value: defaultPlannerModel
  }
  {
    name: 'SWARM_REVIEWER_MODEL'
    value: defaultReviewerModel
  }
  {
    name: 'SWARM_WORKER_MODEL'
    value: defaultWorkerModel
  }
  {
    name: 'SWARM_KEEP_FAILED_SANDBOXES'
    value: string(keepFailedSandboxes)
  }
  {
    name: 'SWARM_SANDBOX_GROUP_NAME'
    value: effectiveSandboxGroupName
  }
  {
    name: 'SWARM_SANDBOX_IDLE_TIMEOUT_SECONDS'
    value: string(sandboxIdleTimeoutSeconds)
  }
  {
    name: 'SWARM_STORAGE_ACCOUNT_NAME'
    value: storageAccount.name
  }
  {
    name: 'SWARM_STORAGE_ACCOUNT_URL'
    value: storageAccount.properties.primaryEndpoints.blob
  }
]
var optionalSandboxSelectorEnvironmentVariables = concat(
  empty(sandboxDiskId)
    ? []
    : [
        {
          name: 'SWARM_SANDBOX_DISK_ID'
          value: sandboxDiskId
        }
      ]
)
resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
  tags: resourceTags
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: containerRegistryName
  location: location
  sku: {
    name: 'Basic'
  }
  tags: resourceTags
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  tags: resourceTags
  properties: {
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  tags: resourceTags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalyticsWorkspace.id
  }
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  tags: resourceTags
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource dtsScheduler 'Microsoft.DurableTask/schedulers@2026-02-01' = {
  name: effectiveDtsSchedulerName
  location: location
  tags: resourceTags
  properties: {
    ipAllowlist: dtsIpAllowlist
    publicNetworkAccess: dtsPublicNetworkAccess
    sku: dtsSku
  }
}

resource dtsTaskHub 'Microsoft.DurableTask/schedulers/taskHubs@2026-02-01' = {
  parent: dtsScheduler
  name: effectiveDtsTaskHubName
  properties: {}
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: resourceTags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsWorkspace.properties.customerId
        sharedKey: logAnalyticsWorkspace.listKeys().primarySharedKey
      }
    }
  }
}

// Provision the ACA sandbox group and Durable Task Scheduler in the same pass so
// `azd up` can lay down the orchestration backing store and the execution plane together.
resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' = {
  name: effectiveSandboxGroupName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  tags: resourceTags
  properties: {}
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: resourceTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: true
        targetPort: targetPort
        transport: 'auto'
      }
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: managedIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'swarm-api'
          image: containerImage
          env: concat(
            appEnvironmentVariables,
            optionalSandboxSelectorEnvironmentVariables
          )
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: targetPort
              }
              initialDelaySeconds: 15
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: targetPort
              }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, managedIdentity.id, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: containerRegistry
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource storageBlobDataContributorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, managedIdentity.id, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: storageAccount
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}

// Storage is used for the run-id index and short-lived run-scoped secret retention.
resource storageTableDataContributorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, managedIdentity.id, '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
  scope: storageAccount
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
  }
}

// Durable Task Data Contributor lets the app's user-assigned identity create, query, and drive DTS instances.
resource dtsDataContributorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(dtsScheduler.id, managedIdentity.id, '0ad04412-c4d5-4796-b79c-f76d14c8d402')
  scope: dtsScheduler
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0ad04412-c4d5-4796-b79c-f76d14c8d402')
  }
}

// ACA Sandboxes data-plane calls require the sandbox-group data role in addition to ARM Contributor.
resource sandboxDataOwnerRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sandboxGroup.id, managedIdentity.id, 'c24cf47c-5077-412d-a19c-45202126392c')
  scope: sandboxGroup
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'c24cf47c-5077-412d-a19c-45202126392c')
  }
}

// Contributor on the sandbox group covers preview management operations that flow through ARM.
resource sandboxContributorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sandboxGroup.id, managedIdentity.id, 'b24988ac-6180-42a0-ab88-20f7382dd24c')
  scope: sandboxGroup
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')
  }
}

output applicationInsightsConnectionString string = appInsights.properties.ConnectionString
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistry.properties.loginServer
output containerAppName string = containerApp.name
output containerAppUrl string = swarmAppBaseUrl
output containerRegistryLoginServer string = containerRegistry.properties.loginServer
output dtsConnectionString string = dtsConnectionString
output dtsEndpoint string = dtsScheduler.properties.endpoint
output dtsSchedulerName string = dtsScheduler.name
output dtsTaskHubName string = dtsTaskHub.name
output managedIdentityClientId string = managedIdentity.properties.clientId
output managedIdentityPrincipalId string = managedIdentity.properties.principalId
output runtimeSettingsContract array = runtimeContractSettings
output sandboxGroupName string = sandboxGroup.name
output storageAccountBlobEndpoint string = storageAccount.properties.primaryEndpoints.blob
output storageAccountTableEndpoint string = storageAccount.properties.primaryEndpoints.table
