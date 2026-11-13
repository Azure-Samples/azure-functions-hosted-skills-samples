// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

param namespaceName string
@description('Queues to create. The app listens on the first one; the others are for local runs.')
param queueNames array
param location string = resourceGroup().location
param tags object = {}
param managedIdentityPrincipalId string
param userPrincipalId string = ''

@description('Failed deliveries before a record moves to the dead-letter queue. Validation errors never succeed on retry, so keep this low.')
param maxDeliveryCount int = 3

var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
// The app receives from the queue and its dead-letter queue, and sends repaired records back.
// The deployer sends samples and peeks the dead-letter queue with scripts/demo.py, and runs the app locally.
var roleIds = [
  serviceBusDataReceiverRoleId
  serviceBusDataSenderRoleId
]

// Standard tier is required for duplicate detection, which makes the repair requeue idempotent.
resource namespace 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: namespaceName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
  }
}

resource queues 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = [for queueName in queueNames: {
  parent: namespace
  name: queueName
  properties: {
    // The dead-letter queue inherits this lock; a hosted skill run can take a minute or two.
    lockDuration: 'PT5M'
    maxDeliveryCount: maxDeliveryCount
    defaultMessageTimeToLive: 'P7D'
    deadLetteringOnMessageExpiration: true
    // A repaired record is sent with message ID "repair-<original id>". If the skill runs twice
    // for the same dead-lettered message, Service Bus drops the second copy.
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'PT1H'
  }
}]

resource appRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in roleIds: {
  name: guid(namespace.id, managedIdentityPrincipalId, roleId)
  scope: namespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}]

resource userRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in roleIds: if (!empty(userPrincipalId)) {
  name: guid(namespace.id, userPrincipalId, roleId)
  scope: namespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    principalId: userPrincipalId
    principalType: 'User'
  }
}]

output fullyQualifiedNamespace string = '${namespace.name}.servicebus.windows.net'
