// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import gql from 'graphql-tag';

export default gql`
  query ListDocumentsByUseCase($useCaseId: String!, $businessUnitId: String!, $limit: Int, $nextToken: String) {
    listDocumentsByUseCase(useCaseId: $useCaseId, businessUnitId: $businessUnitId, limit: $limit, nextToken: $nextToken) {
      Documents {
        ObjectKey
        PK
        SK
        BusinessUnitId
        UseCaseId
        ObjectStatus
        InitialEventTime
        CompletionTime
      }
      nextToken
    }
  }
`;
