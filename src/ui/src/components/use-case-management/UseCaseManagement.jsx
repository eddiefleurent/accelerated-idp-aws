// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { Container, Header, SpaceBetween, Table, Button, Modal, FormField, Input, Box, Alert } from '@cloudscape-design/components';
import useUseCaseContext from '../../contexts/useCase';

const FORBIDDEN_CHARS = ['#', '/'];

const hasForbiddenChars = (value) => FORBIDDEN_CHARS.some((ch) => value.includes(ch));
const getForbiddenCharError = (fieldName, value) =>
  hasForbiddenChars(value) ? `${fieldName} cannot contain '#' or '/' characters` : undefined;

const UseCaseManagement = () => {
  const { useCases, createUseCase, loading, error } = useUseCaseContext();
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [formData, setFormData] = useState({ businessUnitId: '', useCaseId: '', name: '', description: '' });
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);

  const handleCreate = async () => {
    setCreating(true);
    setCreateError(null);
    try {
      const trimmedData = {
        businessUnitId: formData.businessUnitId.trim(),
        useCaseId: formData.useCaseId.trim(),
        name: formData.name.trim(),
        description: formData.description.trim(),
      };
      await createUseCase(trimmedData.businessUnitId, trimmedData.useCaseId, trimmedData.name, trimmedData.description);
      setShowCreateModal(false);
      setFormData({ businessUnitId: '', useCaseId: '', name: '', description: '' });
    } catch (err) {
      const message = err?.message || (typeof err === 'string' ? err : undefined);
      setCreateError(message || 'Failed to create use case');
    } finally {
      setCreating(false);
    }
  };

  const resetForm = () => {
    setShowCreateModal(false);
    setFormData({ businessUnitId: '', useCaseId: '', name: '', description: '' });
    setCreateError(null);
  };

  const handleDismiss = () => {
    if (creating) return;
    resetForm();
  };

  const hasForbiddenChar = hasForbiddenChars(formData.businessUnitId) || hasForbiddenChars(formData.useCaseId);
  const isFormValid = formData.businessUnitId.trim() && formData.useCaseId.trim() && formData.name.trim() && !hasForbiddenChar;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            actions={
              <Button variant="primary" onClick={() => setShowCreateModal(true)}>
                Create Use Case
              </Button>
            }
          >
            Use Case Management
          </Header>
        }
      >
        {error && <Alert type="error">{error.message || String(error) || 'An unexpected error occurred'}</Alert>}

        <Table
          columnDefinitions={[
            { id: 'businessUnitId', header: 'Business Unit', cell: (item) => item.businessUnitId },
            { id: 'useCaseId', header: 'Use Case ID', cell: (item) => item.useCaseId },
            { id: 'name', header: 'Name', cell: (item) => item.name },
            { id: 'description', header: 'Description', cell: (item) => item.description || '-' },
          ]}
          items={useCases}
          loading={loading}
          loadingText="Loading use cases..."
          trackBy={(item) => `${item.businessUnitId}#${item.useCaseId}`}
          empty={
            <Box textAlign="center" color="inherit">
              <b>No use cases</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                No use cases have been configured yet.
              </Box>
            </Box>
          }
          sortingDisabled
        />
      </Container>

      <Modal
        visible={showCreateModal}
        onDismiss={handleDismiss}
        header="Create Use Case"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleDismiss} disabled={creating}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleCreate} loading={creating} disabled={creating || !isFormValid}>
                Create
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {createError && <Alert type="error">{createError}</Alert>}
          <FormField
            label="Business Unit ID"
            constraintText="Identifier for the business unit (e.g., retail-banking)"
            errorText={getForbiddenCharError('Business Unit ID', formData.businessUnitId)}
          >
            <Input
              value={formData.businessUnitId}
              onChange={({ detail }) => setFormData({ ...formData, businessUnitId: detail.value })}
              placeholder="retail-banking"
            />
          </FormField>
          <FormField
            label="Use Case ID"
            constraintText="Identifier for the use case (e.g., mortgage-processing)"
            errorText={getForbiddenCharError('Use Case ID', formData.useCaseId)}
          >
            <Input
              value={formData.useCaseId}
              onChange={({ detail }) => setFormData({ ...formData, useCaseId: detail.value })}
              placeholder="mortgage-processing"
            />
          </FormField>
          <FormField label="Name" constraintText="Required. Human-readable name for the use case">
            <Input
              value={formData.name}
              onChange={({ detail }) => setFormData({ ...formData, name: detail.value })}
              placeholder="Mortgage Document Processing"
            />
          </FormField>
          <FormField label="Description" constraintText="Optional description">
            <Input
              value={formData.description}
              onChange={({ detail }) => setFormData({ ...formData, description: detail.value })}
              placeholder="Processes mortgage application documents"
            />
          </FormField>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default UseCaseManagement;
