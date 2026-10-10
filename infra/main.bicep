// sqlvec: Azure SQL Database on the free offer, plus optional Azure OpenAI.
//
// Creates, in the target resource group:
//   * a logical Azure SQL server (SQL auth, TLS 1.2, public endpoint)
//   * a firewall rule for one client IP
//   * one General Purpose SERVERLESS database (GP_S_Gen5_2) using the free
//     offer: useFreeLimit = true and freeLimitExhaustionBehavior = 'AutoPause',
//     so when the monthly 100,000 vCore-seconds are used up the database
//     pauses until the 1st instead of billing you
//   * optionally, an Azure OpenAI account with a pay-per-token
//     text-embedding-3-small deployment (for SQLVEC_EMBED_PROVIDER=azure and
//     for sql/azure/in_db_embeddings.sql)
//   * a monthly budget on the resource group with e-mail alerts
//
// The free offer is limited to 10 databases per subscription. If this
// subscription already used them up, the deployment fails rather than
// creating a paid database, which is what we want.
//
// Deploy with scripts/up.sh, or:
//   az deployment group create -g <rg> -f infra/main.bicep \
//     -p clientIp=<your ip> adminPassword=<secret> budgetEmail=<you@example.com>

targetScope = 'resourceGroup'

@description('Region for the SQL server and database.')
param location string = resourceGroup().location

@description('Globally unique name for the logical SQL server.')
@minLength(1)
@maxLength(63)
param serverName string = 'sqlvec-${uniqueString(resourceGroup().id)}'

@description('SQL administrator login.')
param adminLogin string = 'sqlvecadmin'

@description('SQL administrator password.')
@secure()
@minLength(12)
param adminPassword string

@description('Your public IP address, allowed through the server firewall.')
param clientIp string

@description('Database name.')
param databaseName string = 'sqlvec'

@description('Max vCores for the serverless database. The free offer allows up to 4 with auto-pause.')
@allowed([1, 2, 4])
param maxVCores int = 2

@description('Minutes idle before auto-pause. 15 is the General Purpose minimum and burns the fewest vCore-seconds; 60 is the default. I have not verified that the free offer accepts 15, so the default stays at 60.')
@minValue(15)
param autoPauseDelayMinutes int = 60

@description('Use the free offer. Set false only if you deliberately want a paid serverless database.')
param useFreeLimit bool = true

@description('Deploy an Azure OpenAI account with a text-embedding-3-small deployment.')
param deployOpenAi bool = false

@description('Region for Azure OpenAI. Not every region offers every model.')
param openAiLocation string = location

@description('Globally unique name (and custom subdomain) for the Azure OpenAI account.')
param openAiName string = 'sqlvec-aoai-${uniqueString(resourceGroup().id)}'

@description('Embedding deployment SKU. Standard and GlobalStandard are both pay-per-token.')
@allowed(['Standard', 'GlobalStandard'])
param embeddingDeploymentSku string = 'Standard'

@description('Rate limit for the embedding deployment, in thousands of tokens per minute. Caps throughput, not cost.')
@minValue(1)
param embeddingCapacityK int = 30

@description('Monthly budget for this resource group, in your billing currency.')
@minValue(1)
param monthlyBudget int = 5

@description('E-mail address that receives budget alerts.')
param budgetEmail string

@description('First day of the budget period (must be the first of a month). Defaults to this month.')
param budgetStartDate string = utcNow('yyyy-MM-01')

var tags = {
  project: 'sqlvec'
  purpose: 'community-demo'
}

// ---------------------------------------------------------------------------
// Azure SQL
// ---------------------------------------------------------------------------

resource sqlServer 'Microsoft.Sql/servers@2023-08-01' = {
  name: serverName
  location: location
  tags: tags
  properties: {
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    version: '12.0'
  }
}

resource firewall 'Microsoft.Sql/servers/firewallRules@2023-08-01' = {
  parent: sqlServer
  name: 'client-ip'
  properties: {
    startIpAddress: clientIp
    endIpAddress: clientIp
  }
}

resource database 'Microsoft.Sql/servers/databases@2023-08-01' = {
  parent: sqlServer
  name: databaseName
  location: location
  tags: tags
  sku: {
    name: 'GP_S_Gen5'
    tier: 'GeneralPurpose'
    family: 'Gen5'
    capacity: maxVCores
  }
  properties: {
    useFreeLimit: useFreeLimit
    // AutoPause: when the free allowance is gone, pause until next month.
    // Switching an existing database to 'BillOverUsage' can't be undone.
    freeLimitExhaustionBehavior: useFreeLimit ? 'AutoPause' : null
    autoPauseDelay: autoPauseDelayMinutes
    minCapacity: json('0.5')
    maxSizeBytes: 34359738368 // 32 GB, the free offer's data limit
    collation: 'SQL_Latin1_General_CP1_CI_AS'
    requestedBackupStorageRedundancy: 'Local'
    zoneRedundant: false
  }
}

// ---------------------------------------------------------------------------
// Azure OpenAI (optional, pay per token)
// ---------------------------------------------------------------------------

resource openAi 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (deployOpenAi) {
  name: openAiName
  location: openAiLocation
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: openAiName
    publicNetworkAccess: 'Enabled'
  }
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = if (deployOpenAi) {
  parent: openAi
  name: 'text-embedding-3-small'
  sku: {
    name: embeddingDeploymentSku
    capacity: embeddingCapacityK
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-small'
      version: '1'
    }
  }
}

// ---------------------------------------------------------------------------
// Budget with alerts (an alert, not a cap)
// ---------------------------------------------------------------------------

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'sqlvec-monthly-budget'
  properties: {
    category: 'Cost'
    amount: monthlyBudget
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    notifications: {
      actual50: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: [budgetEmail]
      }
      actual100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: [budgetEmail]
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [budgetEmail]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs (no secrets)
// ---------------------------------------------------------------------------

output serverName string = sqlServer.name
output serverFqdn string = sqlServer.properties.fullyQualifiedDomainName
output databaseName string = database.name
output connTemplate string = 'Server=tcp:${sqlServer.properties.fullyQualifiedDomainName},1433;Database=${databaseName};UID=${adminLogin};PWD=<password>;Encrypt=yes;TrustServerCertificate=no'
output openAiEndpoint string = deployOpenAi ? openAi!.properties.endpoint : ''
output openAiName string = deployOpenAi ? openAi.name : ''
output embeddingDeploymentName string = deployOpenAi ? embeddingDeployment.name : ''
