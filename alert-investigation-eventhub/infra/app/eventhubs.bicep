// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

param namespaceName string
param eventHubName string
@description('Consumer group the deployed function app reads from.')
param consumerGroupName string
@description('Separate consumer group for running the router locally, so it does not compete with the deployed app.')
param localConsumerGroupName string
param location string = resourceGroup().location
param tags object = {}
param managedIdentityPrincipalId string
@description('Optional user principal (the deployer) that sends demo alerts and can run the router locally.')
param userPrincipalId string = ''

var eventHubsDataReceiverRoleId = 'a638d3c7-ab3a-418d-83e6-5f17a39d4fde'
var eventHubsDataSenderRoleId = '2b629674-e913-4c01-ae53-ef4638d8f975'

resource namespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: namespaceName
  location: location
  tags: tags
  // Standard is the lowest tier with more than one consumer group.
  sku: {
    name: 'Standard'
    tier: 'Standard'
    capacity: 1
  }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

resource eventHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: namespace
  name: eventHubName
  properties: {
    partitionCount: 2
    messageRetentionInDays: 1
  }
}

resource consumerGroup 'Microsoft.EventHub/namespaces/eventhubs/consumergroups@2024-01-01' = {
  parent: eventHub
  name: consumerGroupName
}

resource localConsumerGroup 'Microsoft.EventHub/namespaces/eventhubs/consumergroups@2024-01-01' = {
  parent: eventHub
  name: localConsumerGroupName
}

resource appReceiverRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(namespace.id, managedIdentityPrincipalId, eventHubsDataReceiverRoleId)
  scope: namespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', eventHubsDataReceiverRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// The deployer sends demo alerts and can run the router locally against the local consumer group.
resource userRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in [eventHubsDataSenderRoleId, eventHubsDataReceiverRoleId]: if (!empty(userPrincipalId)) {
  name: guid(namespace.id, userPrincipalId, roleId)
  scope: namespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    principalId: userPrincipalId
    principalType: 'User'
  }
}]

output namespaceName string = namespace.name
output fullyQualifiedNamespace string = '${namespace.name}.servicebus.windows.net'
output eventHubName string = eventHub.name
output consumerGroupName string = consumerGroup.name
output localConsumerGroupName string = localConsumerGroup.name
