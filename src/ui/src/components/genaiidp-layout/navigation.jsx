// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { React, useMemo } from 'react';
import { useLocation } from 'react-router-dom';
import { SideNavigation } from '@cloudscape-design/components';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import UseCaseSelector from '../use-case-selector/UseCaseSelector';

import {
  DOCUMENTS_PATH,
  DOCUMENTS_KB_QUERY_PATH,
  TEST_STUDIO_PATH,
  DEFAULT_PATH,
  UPLOAD_DOCUMENT_PATH,
  CONFIGURATION_PATH,
  PRICING_PATH,
  DISCOVERY_PATH,
  USER_MANAGEMENT_PATH,
  USE_CASE_MANAGEMENT_PATH,
  AGENT_CHAT_PATH,
} from '../../routes/constants';

export const documentsNavHeader = { text: 'Tools', href: `#${DEFAULT_PATH}` };

// Full navigation items for Admin users
export const adminNavItems = [
  { type: 'link', text: 'Document List', href: `#${DOCUMENTS_PATH}` },
  { type: 'link', text: 'Document KB', href: `#${DOCUMENTS_KB_QUERY_PATH}` },
  { type: 'link', text: 'Upload Document(s)', href: `#${UPLOAD_DOCUMENT_PATH}` },
  { type: 'link', text: 'Agent Companion Chat', href: `#${AGENT_CHAT_PATH}` },
  {
    type: 'section',
    text: 'Configuration',
    items: [
      { type: 'link', text: 'Discovery', href: `#${DISCOVERY_PATH}` },
      { type: 'link', text: 'View/Edit Configuration', href: `#${CONFIGURATION_PATH}` },
      { type: 'link', text: 'View/Edit Pricing', href: `#${PRICING_PATH}` },
      { type: 'link', text: 'User Management', href: `#${USER_MANAGEMENT_PATH}` },
      { type: 'link', text: 'Use Cases', href: `#${USE_CASE_MANAGEMENT_PATH}` },
    ],
  },
  {
    type: 'section',
    text: 'Test Studio',
    items: [
      { type: 'link', text: 'Test Sets', href: `#${TEST_STUDIO_PATH}?tab=sets` },
      { type: 'link', text: 'Test Executions', href: `#${TEST_STUDIO_PATH}?tab=executions` },
    ],
  },
  {
    type: 'section',
    text: 'Resources',
    items: [
      {
        type: 'link',
        text: 'README',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/README.md',
        external: true,
      },
      {
        type: 'link',
        text: 'Source Code',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws',
        external: true,
      },
    ],
  },
];

// Navigation items for Supervisor users (full doc visibility + HITL management, no config/upload/admin)
export const supervisorNavItems = [
  { type: 'link', text: 'Document List', href: `#${DOCUMENTS_PATH}` },
  { type: 'link', text: 'Document KB', href: `#${DOCUMENTS_KB_QUERY_PATH}` },
  { type: 'link', text: 'Agent Companion Chat', href: `#${AGENT_CHAT_PATH}` },
];

// Limited navigation items for Reviewer users
export const reviewerNavItems = [{ type: 'link', text: 'Document List', href: `#${DOCUMENTS_PATH}` }];

// Keep for backward compatibility
export const documentsNavItems = adminNavItems;

const defaultOnFollowHandler = (ev) => {
  if (ev.detail.href === '#deployment-info') {
    ev.preventDefault();
    return;
  }
  console.log(ev);
};

/* eslint-disable react/prop-types */
const Navigation = ({ header = documentsNavHeader, items, onFollowHandler = defaultOnFollowHandler }) => {
  const location = useLocation();
  const path = location.pathname;
  let activeHref = `#${DEFAULT_PATH}`;
  const { settings } = useSettingsContext() || {};
  const { isReviewer, isSupervisor, isAdmin, loading: roleLoading } = useUserRole();

  // Select navigation items based on user role (deny-by-default while loading)
  const baseItems = useMemo(() => {
    if (items) return items;
    if (roleLoading) return reviewerNavItems;
    if (isAdmin) return adminNavItems;
    if (isSupervisor) return supervisorNavItems;
    if (isReviewer) return reviewerNavItems;
    return reviewerNavItems;
  }, [items, roleLoading, isReviewer, isSupervisor, isAdmin]);

  // Determine active link based on current path
  if (path.includes(PRICING_PATH)) {
    activeHref = `#${PRICING_PATH}`;
  } else if (path.includes(CONFIGURATION_PATH)) {
    activeHref = `#${CONFIGURATION_PATH}`;
  } else if (path.includes(DOCUMENTS_KB_QUERY_PATH)) {
    activeHref = `#${DOCUMENTS_KB_QUERY_PATH}`;
  } else if (path.includes(TEST_STUDIO_PATH)) {
    const urlParams = new URLSearchParams(location.search);
    const tab = urlParams.get('tab');
    activeHref = tab ? `#${TEST_STUDIO_PATH}?tab=${tab}` : `#${TEST_STUDIO_PATH}?tab=sets`;
  } else if (path.includes(UPLOAD_DOCUMENT_PATH)) {
    activeHref = `#${UPLOAD_DOCUMENT_PATH}`;
  } else if (path.includes(DISCOVERY_PATH)) {
    activeHref = `#${DISCOVERY_PATH}`;
  } else if (path.includes(USER_MANAGEMENT_PATH)) {
    activeHref = `#${USER_MANAGEMENT_PATH}`;
  } else if (path.includes(USE_CASE_MANAGEMENT_PATH)) {
    activeHref = `#${USE_CASE_MANAGEMENT_PATH}`;
  } else if (path.includes(DOCUMENTS_PATH)) {
    activeHref = `#${DOCUMENTS_PATH}`;
  } else if (path === AGENT_CHAT_PATH) {
    activeHref = `#${AGENT_CHAT_PATH}`;
  }

  // Create navigation items with deployment info
  const navigationItems = [...baseItems];

  if (settings?.Version || settings?.StackName || settings?.BuildDateTime || settings?.IDPPattern) {
    const deploymentInfoItems = [];

    if (settings?.StackName) {
      deploymentInfoItems.push({ type: 'link', text: `Stack Name: ${settings.StackName}`, href: '#stackname' });
    }
    if (settings?.Version) {
      deploymentInfoItems.push({ type: 'link', text: `Version: ${settings.Version}`, href: '#version' });
    }
    if (settings?.BuildDateTime) {
      deploymentInfoItems.push({ type: 'link', text: `Build: ${settings.BuildDateTime}`, href: '#builddatetime' });
    }
    if (settings?.IDPPattern) {
      const pattern = settings.IDPPattern.split(' ')[0];
      deploymentInfoItems.push({ type: 'link', text: `Pattern: ${pattern}`, href: '#idppattern' });
    }

    navigationItems.push({
      type: 'section',
      text: 'Deployment Info',
      items: deploymentInfoItems,
    });
  }

  return (
    <>
      {!roleLoading && <UseCaseSelector isAdmin={isAdmin} />}
      <SideNavigation items={navigationItems} header={header || documentsNavHeader} activeHref={activeHref} onFollow={onFollowHandler} />
    </>
  );
};

export default Navigation;
