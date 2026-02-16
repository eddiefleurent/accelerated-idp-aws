// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState, useCallback, useRef } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import useAppContext from '../contexts/app';
import listDocumentsDateShard from '../graphql/queries/listDocumentsDateShard';
import listDocumentsDateHour from '../graphql/queries/listDocumentsDateHour';
import listDocumentsByUseCaseQuery from '../graphql/queries/listDocumentsByUseCase';
import getDocument from '../graphql/queries/getDocument';
import deleteDocument from '../graphql/queries/deleteDocument';
import reprocessDocument from '../graphql/queries/reprocessDocument';
import abortWorkflow from '../graphql/queries/abortWorkflow';
import onCreateDocument from '../graphql/queries/onCreateDocument';
import onUpdateDocument from '../graphql/queries/onUpdateDocument';
import { DOCUMENT_LIST_SHARDS_PER_DAY } from '../components/document-list/documents-table-config';
import { ALL_USE_CASES_ID } from './use-use-cases';

const client = generateClient();

const logger = new ConsoleLogger('useGraphQlApi');

export const MAX_DOCUMENTS = 1000;

const useGraphQlApi = ({ initialPeriodsToLoad = DOCUMENT_LIST_SHARDS_PER_DAY * 2, useCaseFilter = null } = {}) => {
  const [periodsToLoad, setPeriodsToLoad] = useState(initialPeriodsToLoad);
  const [isDocumentsListLoading, setIsDocumentsListLoading] = useState(false);
  const [documents, setDocuments] = useState([]);
  const [isDocumentListTruncated, setIsDocumentListTruncated] = useState(false);
  const { setErrorMessage } = useAppContext();

  // Use ref for useCaseFilter so closures always see the latest value
  const useCaseFilterRef = useRef(useCaseFilter);
  useCaseFilterRef.current = useCaseFilter;

  const subscriptionsRef = useRef({ onCreate: null, onUpdate: null });
  const pendingReloadRef = useRef(false);
  const reloadTimeoutRef = useRef(null);
  const loadTimeoutRef = useRef(null);
  // Guard to prevent both periodsToLoad and useCaseFilter effects from
  // independently triggering a load on the initial mount.  The first effect
  // to fire sets this to true and triggers the load; subsequent effects skip
  // their initial invocation.
  const hasMountedRef = useRef(false);
  // Monotonically increasing load sequence token to discard stale results
  // when the use-case filter changes while a load is still in flight.
  const loadSequenceRef = useRef(0);

  // Cleanup timeouts on unmount to prevent state updates after unmount
  useEffect(() => {
    return () => {
      if (reloadTimeoutRef.current) {
        clearTimeout(reloadTimeoutRef.current);
      }
      if (loadTimeoutRef.current) {
        clearTimeout(loadTimeoutRef.current);
      }
    };
  }, []);

  const finalizeDocumentsLoad = useCallback(() => {
    if (pendingReloadRef.current) {
      pendingReloadRef.current = false;
      // Clear any existing timeout before setting a new one
      if (reloadTimeoutRef.current) {
        clearTimeout(reloadTimeoutRef.current);
      }
      // Briefly toggle loading off then on to ensure React sees a state change
      // and re-triggers the loading effect with the latest filter values
      setIsDocumentsListLoading(false);
      reloadTimeoutRef.current = setTimeout(() => setIsDocumentsListLoading(true), 0);
    } else {
      setIsDocumentsListLoading(false);
    }
  }, []);

  const setDocumentsDeduped = useCallback((documentValues) => {
    logger.debug('setDocumentsDeduped called with:', documentValues);
    setDocuments((currentDocuments) => {
      const documentValuesdocumentIds = documentValues.map((c) => c.ObjectKey);

      // Remove old entries with matching ObjectKeys
      const filteredCurrentDocuments = currentDocuments.filter((c) => !documentValuesdocumentIds.includes(c.ObjectKey));

      // Add new entries with PK/SK preserved
      const newDocuments = documentValues.map((document) => ({
        ...document,
        ListPK: document.ListPK || currentDocuments.find((c) => c.ObjectKey === document.ObjectKey)?.ListPK,
        ListSK: document.ListSK || currentDocuments.find((c) => c.ObjectKey === document.ObjectKey)?.ListSK,
      }));

      // Combine and deduplicate by ObjectKey, keeping only the latest entry per ObjectKey
      const allDocuments = [...filteredCurrentDocuments, ...newDocuments];
      const deduplicatedByObjectKey = Object.values(
        allDocuments.reduce((acc, doc) => {
          const existing = acc[doc.ObjectKey];
          // Keep the document with the most recent CompletionTime or InitialEventTime
          if (!existing) {
            acc[doc.ObjectKey] = doc;
          } else {
            const existingTime = existing.CompletionTime || existing.InitialEventTime || '0';
            const newTime = doc.CompletionTime || doc.InitialEventTime || '0';
            if (newTime > existingTime) {
              acc[doc.ObjectKey] = doc;
            }
          }
          return acc;
        }, {}),
      );

      return deduplicatedByObjectKey;
    });
  }, []);

  const getDocumentDetailsFromIds = useCallback(async (objectKeys) => {
    // prettier-ignore
    logger.debug('getDocumentDetailsFromIds', objectKeys);
    const getDocumentPromises = objectKeys.map((objectKey) => client.graphql({ query: getDocument, variables: { objectKey } }));
    const getDocumentResolutions = await Promise.allSettled(getDocumentPromises);

    // Separate rejected promises from null/undefined results
    const getDocumentRejected = getDocumentResolutions.filter((r) => r.status === 'rejected');
    const getDocumentNull = getDocumentResolutions
      .map((r, idx) => ({
        status: r.status,
        doc: r.status === 'fulfilled' ? r.value?.data?.getDocument : null,
        key: objectKeys[idx],
      }))
      .filter((item) => item.status === 'fulfilled' && !item.doc)
      .map((item) => item.key);

    // Log partial failures but NEVER show error banner for individual document failures
    if (getDocumentRejected.length > 0) {
      logger.warn(`Failed to load ${getDocumentRejected.length} of ${objectKeys.length} document(s) due to query rejection`);
      logger.debug('Rejected promises:', getDocumentRejected);
    }
    if (getDocumentNull.length > 0) {
      logger.warn(`${getDocumentNull.length} of ${objectKeys.length} document(s) not found (returned null):`, getDocumentNull);
      logger.warn('These documents have list entries but no corresponding document records - possible orphaned list entries');
    }

    // Filter out null/undefined documents to prevent downstream errors
    const documentValues = getDocumentResolutions
      .filter((r) => r.status === 'fulfilled')
      .map((r) => r.value?.data?.getDocument)
      .filter((doc) => doc != null);

    logger.debug(`Successfully loaded ${documentValues.length} of ${objectKeys.length} requested documents`);
    return documentValues;
  }, []);

  useEffect(() => {
    if (subscriptionsRef.current.onCreate) {
      logger.debug('onCreateDocument subscription already exists, skipping');
      return undefined;
    }

    logger.debug('onCreateDocument subscription');
    const subscription = client.graphql({ query: onCreateDocument }).subscribe({
      next: async (subscriptionData) => {
        logger.debug('document list subscription update', subscriptionData);
        const data = subscriptionData?.data;
        const objectKey = data?.onCreateDocument?.ObjectKey || '';
        if (objectKey) {
          try {
            const documentValues = await getDocumentDetailsFromIds([objectKey]);
            if (documentValues && documentValues.length > 0) {
              // Filter by active use-case to avoid cross-scope leakage
              const filter = useCaseFilterRef.current;
              if (!filter?.useCaseId || filter.useCaseId === ALL_USE_CASES_ID) {
                setDocumentsDeduped(documentValues);
              } else {
                const filtered = documentValues.filter(
                  (doc) => doc.BusinessUnitId === filter.businessUnitId && doc.UseCaseId === filter.useCaseId,
                );
                if (filtered.length > 0) {
                  setDocumentsDeduped(filtered);
                }
              }
            }
          } catch (error) {
            logger.error('Error processing onCreateDocument subscription:', error);
          }
        }
      },
      error: (error) => {
        logger.error('onCreateDocument subscription error:', error);
        setErrorMessage('document list network subscription failed - please reload the page');
      },
    });

    subscriptionsRef.current.onCreate = subscription;

    return () => {
      logger.debug('onCreateDocument subscription cleanup');
      if (subscriptionsRef.current.onCreate) {
        subscriptionsRef.current.onCreate.unsubscribe();
        subscriptionsRef.current.onCreate = null;
      }
    };
  }, [getDocumentDetailsFromIds, setDocumentsDeduped, setErrorMessage]);

  useEffect(() => {
    if (subscriptionsRef.current.onUpdate) {
      logger.debug('onUpdateDocument subscription already exists, skipping');
      return undefined;
    }

    logger.debug('onUpdateDocument subscription setup');
    const subscription = client.graphql({ query: onUpdateDocument }).subscribe({
      next: async (subscriptionData) => {
        logger.debug('document update subscription received', subscriptionData);
        const data = subscriptionData?.data;
        const documentUpdateEvent = data?.onUpdateDocument;
        if (documentUpdateEvent?.ObjectKey) {
          // Fetch full document details to ensure we have complete data
          try {
            const documentValues = await getDocumentDetailsFromIds([documentUpdateEvent.ObjectKey]);
            if (documentValues && documentValues.length > 0) {
              // Filter by active use-case to avoid cross-scope leakage
              const filter = useCaseFilterRef.current;
              if (!filter?.useCaseId || filter.useCaseId === ALL_USE_CASES_ID) {
                setDocumentsDeduped(documentValues);
              } else {
                const filtered = documentValues.filter(
                  (doc) => doc.BusinessUnitId === filter.businessUnitId && doc.UseCaseId === filter.useCaseId,
                );
                if (filtered.length > 0) {
                  setDocumentsDeduped(filtered);
                }
              }
            }
          } catch (error) {
            logger.error('Error fetching document details after update:', error);
            // Fallback to subscription data if fetch fails
            const filter = useCaseFilterRef.current;
            const matchesFilter =
              !filter?.useCaseId ||
              filter.useCaseId === ALL_USE_CASES_ID ||
              (documentUpdateEvent.BusinessUnitId === filter.businessUnitId && documentUpdateEvent.UseCaseId === filter.useCaseId);
            if (matchesFilter) {
              setDocumentsDeduped([documentUpdateEvent]);
            }
          }
        }
      },
      error: (error) => {
        logger.error('onUpdateDocument subscription error:', error);
        setErrorMessage('document update network request failed - please reload the page');
      },
    });

    subscriptionsRef.current.onUpdate = subscription;

    return () => {
      logger.debug('onUpdateDocument subscription cleanup');
      if (subscriptionsRef.current.onUpdate) {
        subscriptionsRef.current.onUpdate.unsubscribe();
        subscriptionsRef.current.onUpdate = null;
      }
    };
  }, [setDocumentsDeduped, setErrorMessage, getDocumentDetailsFromIds]);

  const listDocumentIdsByDateShards = async ({ date, shards }) => {
    const listDocumentsDateShardPromises = shards.map((i) => {
      logger.debug('sending list document date shard', date, i);
      return client.graphql({ query: listDocumentsDateShard, variables: { date, shard: i } });
    });
    const listDocumentsDateShardResolutions = await Promise.allSettled(listDocumentsDateShardPromises);

    const listRejected = listDocumentsDateShardResolutions.filter((r) => r.status === 'rejected');
    if (listRejected.length) {
      setErrorMessage('failed to list documents - please try again later');
      logger.error('list document promises rejected', listRejected);
    }
    const documentData = listDocumentsDateShardResolutions
      .filter((r) => r.status === 'fulfilled')
      .map((r) => r.value?.data?.listDocumentsDateShard?.Documents || [])
      .reduce((pv, cv) => [...cv, ...pv], []);

    return documentData;
  };

  const listDocumentIdsByDateHours = async ({ date, hours }) => {
    const listDocumentsDateHourPromises = hours.map((i) => {
      logger.debug('sending list document date hour', date, i);
      return client.graphql({ query: listDocumentsDateHour, variables: { date, hour: i } });
    });
    const listDocumentsDateHourResolutions = await Promise.allSettled(listDocumentsDateHourPromises);

    const listRejected = listDocumentsDateHourResolutions.filter((r) => r.status === 'rejected');
    if (listRejected.length) {
      setErrorMessage('failed to list documents - please try again later');
      logger.error('list document promises rejected', listRejected);
    }

    const documentData = listDocumentsDateHourResolutions
      .filter((r) => r.status === 'fulfilled')
      .map((r) => r.value?.data?.listDocumentsDateHour?.Documents || [])
      .reduce((pv, cv) => [...cv, ...pv], []);

    return documentData;
  };

  const sendSetDocumentsByUseCase = async (useCaseId, businessUnitId, loadSeq) => {
    if (!businessUnitId || !useCaseId) {
      setDocumentsDeduped([]);
      setIsDocumentListTruncated(false);
      finalizeDocumentsLoad();
      return;
    }

    const BATCH_SIZE = 50;

    try {
      const allDocumentItems = [];
      let nextToken = null;
      // Paginate through documents for this use case, up to MAX_DOCUMENTS
      do {
        if (loadSeq !== loadSequenceRef.current) {
          logger.debug(`Discarding stale use-case load (seq ${loadSeq}, current ${loadSequenceRef.current})`);
          return;
        }
        // eslint-disable-next-line no-await-in-loop
        const result = await client.graphql({
          query: listDocumentsByUseCaseQuery,
          variables: { useCaseId, businessUnitId, limit: 100, nextToken },
        });
        const data = result?.data?.listDocumentsByUseCase || null;
        const docs = data?.Documents || [];
        allDocumentItems.push(...docs);
        nextToken = data?.nextToken || null;
      } while (nextToken && allDocumentItems.length < MAX_DOCUMENTS);

      // Discard results if a newer load has been started (filter changed mid-flight)
      if (loadSeq !== loadSequenceRef.current) {
        logger.debug(`Discarding stale use-case load (seq ${loadSeq}, current ${loadSequenceRef.current})`);
        return;
      }

      // Trim to cap in case the last page pushed us over
      const cappedItems = allDocumentItems.slice(0, MAX_DOCUMENTS);
      // Truncated if we hit the cap and more documents exist (nextToken present)
      const wasTruncated = nextToken != null && allDocumentItems.length >= MAX_DOCUMENTS;
      setIsDocumentListTruncated(wasTruncated);
      if (wasTruncated) {
        logger.warn(`Use-case document list capped at ${MAX_DOCUMENTS} (total available: ${allDocumentItems.length}+)`);
      }

      const objectKeys = cappedItems.map((item) => item.ObjectKey);
      if (objectKeys.length === 0) {
        setDocumentsDeduped([]);
        finalizeDocumentsLoad();
        return;
      }

      // Fetch document details in bounded batches to avoid overwhelming the API
      const allDocumentValues = [];
      for (let i = 0; i < objectKeys.length; i += BATCH_SIZE) {
        if (loadSeq !== loadSequenceRef.current) {
          logger.debug(`Discarding stale use-case load during detail fetch (seq ${loadSeq}, current ${loadSequenceRef.current})`);
          return;
        }
        const batch = objectKeys.slice(i, i + BATCH_SIZE);
        // eslint-disable-next-line no-await-in-loop
        const batchValues = await getDocumentDetailsFromIds(batch);
        allDocumentValues.push(...batchValues);
      }

      // Discard results if a newer load has been started (filter changed mid-flight)
      if (loadSeq !== loadSequenceRef.current) {
        logger.debug(`Discarding stale use-case load after detail fetch (seq ${loadSeq}, current ${loadSequenceRef.current})`);
        return;
      }

      // Merge list-entry PK/SK from cappedItems into each document detail
      // (same pattern as sendSetDocumentsForPeriod) so ListPK/ListSK are
      // preserved on initial use-case loads.
      // Build a Map for O(1) lookups instead of O(n²) find-in-loop
      const itemsByKey = new Map(cappedItems.map((item) => [item.ObjectKey, item]));
      const mergedDocumentValues = allDocumentValues
        .filter((detail) => detail != null)
        .map((detail) => {
          const matchingItem = itemsByKey.get(detail.ObjectKey);
          return matchingItem ? { ...detail, ListPK: matchingItem.PK, ListSK: matchingItem.SK } : detail;
        });

      setDocumentsDeduped(mergedDocumentValues);
      finalizeDocumentsLoad();
    } catch (error) {
      setIsDocumentListTruncated(false);
      finalizeDocumentsLoad();
      setErrorMessage('failed to list documents by use case - please try again later');
      logger.error('error listing documents by use case', error);
    }
  };

  const sendSetDocumentsForPeriod = async (loadSeq) => {
    // XXX this logic should be moved to the API
    setIsDocumentListTruncated(false);
    try {
      const now = new Date();

      // array of arrays containing date / shard pairs relative to current UTC time
      // e.g. 2 periods to on load 2021-01-01T:20:00:00.000Z ->
      // [ [ '2021-01-01', 3 ], [ '2021-01-01', 4 ] ]
      const hoursInShard = 24 / DOCUMENT_LIST_SHARDS_PER_DAY;
      const dateShardPairs = [...Array(parseInt(periodsToLoad, 10)).keys()].map((p) => {
        const deltaInHours = p * hoursInShard;
        const relativeDate = new Date(now - deltaInHours * 3600 * 1000);

        const relativeDateString = relativeDate.toISOString().split('T')[0];
        const shard = Math.floor(relativeDate.getUTCHours() / hoursInShard);

        return [relativeDateString, shard];
      });

      // reduce array of date/shard pairs into object of shards by date
      // e.g. [ [ '2021-01-01', 3 ], [ '2021-01-01', 4 ] ] -> { '2021-01-01': [ 3, 4 ] }
      const dateShards = dateShardPairs.reduce((p, c) => ({ ...p, [c[0]]: [...(p[c[0]] || []), c[1]] }), {});
      logger.debug('document list date shards', dateShards);

      // parallelizes listDocuments and getDocumentDetails
      // alternatively we could implement it by sending multiple graphql queries in 1 request
      const documentDataDateShardPromises = Object.keys(dateShards).map(
        // pretttier-ignore
        async (d) => listDocumentIdsByDateShards({ date: d, shards: dateShards[d] }),
      );

      // get document Ids by hour on residual hours outside of the lower shard date/hour boundary
      // or just last n hours when periodsToLoad is less than 1 shard period
      let baseDate;
      let residualHours;
      if (periodsToLoad < 1) {
        baseDate = new Date(now);
        const numHours = parseInt(periodsToLoad * hoursInShard, 10);
        residualHours = [...Array(numHours).keys()].map((h) => (((baseDate.getUTCHours() - h) % 24) + 24) % 24);
      } else {
        baseDate = new Date(now - periodsToLoad * hoursInShard * 3600 * 1000);
        const residualBaseHour = baseDate.getUTCHours() % hoursInShard;
        residualHours = [...Array(hoursInShard - residualBaseHour).keys()].map((h) => (baseDate.getUTCHours() + h) % 24);
      }
      const baseDateString = baseDate.toISOString().split('T')[0];

      const residualDateHours = { date: baseDateString, hours: residualHours };
      logger.debug('document list date hours', residualDateHours);

      const documentDataDateHourPromise = listDocumentIdsByDateHours(residualDateHours);

      const documentDataPromises = [...documentDataDateShardPromises, documentDataDateHourPromise];
      const documentDetailsPromises = documentDataPromises.map(async (documentDataPromise) => {
        const documentData = await documentDataPromise;
        const objectKeys = documentData.map((item) => item.ObjectKey);
        const documentDetails = await getDocumentDetailsFromIds(objectKeys);

        // Log orphaned list entries with full PK/SK details for debugging
        const retrievedKeys = new Set(documentDetails.map((d) => d.ObjectKey));
        const missingDocs = documentData.filter((item) => !retrievedKeys.has(item.ObjectKey));
        if (missingDocs.length > 0) {
          missingDocs.forEach((item) => {
            logger.warn(`Orphaned list entry detected:`);
            logger.warn(`  - List entry: PK="${item.PK}", SK="${item.SK}"`);
            logger.warn(`  - Expected doc entry: PK="doc#${item.ObjectKey}", SK="none"`);
            logger.warn(`  - ObjectKey: "${item.ObjectKey}"`);
          });
        }

        // Merge document details with PK and SK, filtering out nulls to prevent shard-level failures
        // Build a Map for O(1) lookups instead of O(n²) find-in-loop
        const dataByKey = new Map(documentData.map((item) => [item.ObjectKey, item]));
        return documentDetails
          .filter((detail) => detail != null)
          .map((detail) => {
            const matchingData = dataByKey.get(detail.ObjectKey);
            return matchingData ? { ...detail, ListPK: matchingData.PK, ListSK: matchingData.SK } : detail;
          });
      });

      const documentValuesPromises = documentDetailsPromises.map(async (documentValuesPromise) => {
        const documentValues = await documentValuesPromise;
        logger.debug('documentValues', documentValues);
        return documentValues;
      });

      const getDocumentsPromiseResolutions = await Promise.allSettled(documentValuesPromises);
      logger.debug('getDocumentsPromiseResolutions', getDocumentsPromiseResolutions);
      const documentValuesReduced = getDocumentsPromiseResolutions
        .filter((r) => r.status === 'fulfilled')
        .map((r) => r.value)
        .reduce((previous, current) => [...previous, ...current], []);
      logger.debug('documentValuesReduced', documentValuesReduced);

      // Discard results if a newer load has been started (filter/period changed mid-flight)
      if (loadSeq !== loadSequenceRef.current) {
        logger.debug(`Discarding stale period load (seq ${loadSeq}, current ${loadSequenceRef.current})`);
        return;
      }

      setDocumentsDeduped(documentValuesReduced);
      finalizeDocumentsLoad();
      const getDocumentsRejected = getDocumentsPromiseResolutions.filter((r) => r.status === 'rejected');
      // Only show error banner if ALL shard queries failed
      if (getDocumentsRejected.length === documentDataPromises.length) {
        setErrorMessage('failed to get document details - please try again later');
        logger.error('All shard queries rejected', getDocumentsRejected);
      } else if (getDocumentsRejected.length > 0) {
        // Partial failure - log but don't show error banner
        logger.warn(`${getDocumentsRejected.length} of ${documentDataPromises.length} shard queries failed`);
        logger.debug('Rejected shard queries:', getDocumentsRejected);
      }
    } catch (error) {
      finalizeDocumentsLoad();
      setErrorMessage('failed to list Documents - please try again later');
      logger.error('error obtaining document list', error);
    }
  };

  useEffect(() => {
    if (isDocumentsListLoading) {
      logger.debug('document list is loading');
      // Increment the load sequence so any in-flight request from a previous
      // filter/period is discarded when it completes.
      loadSequenceRef.current += 1;
      const thisLoadSeq = loadSequenceRef.current;
      // send in a timeout to avoid blocking rendering
      loadTimeoutRef.current = setTimeout(() => {
        setDocuments([]);
        const currentFilter = useCaseFilterRef.current;
        if (currentFilter?.useCaseId && currentFilter.useCaseId !== ALL_USE_CASES_ID) {
          sendSetDocumentsByUseCase(currentFilter.useCaseId, currentFilter.businessUnitId, thisLoadSeq);
        } else {
          sendSetDocumentsForPeriod(thisLoadSeq);
        }
      }, 1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDocumentsListLoading]);

  useEffect(() => {
    logger.debug('list period changed', periodsToLoad);
    if (!hasMountedRef.current) {
      // First mount: claim the initial load and mark as mounted
      hasMountedRef.current = true;
      setIsDocumentsListLoading(true);
      return;
    }
    // Subsequent changes to periodsToLoad always trigger a reload
    setIsDocumentsListLoading(true);
  }, [periodsToLoad]);

  // Reload documents when the use-case filter changes
  useEffect(() => {
    if (!hasMountedRef.current) {
      // Initial mount already handled by the periodsToLoad effect above;
      // skip to avoid a duplicate load / race condition.
      return;
    }
    if (isDocumentsListLoading) {
      // A load is already in flight; queue a reload so the new filter is applied once it finishes
      pendingReloadRef.current = true;
      return;
    }
    setIsDocumentsListLoading(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [useCaseFilter?.useCaseId, useCaseFilter?.businessUnitId]);

  const deleteDocuments = async (objectKeys) => {
    try {
      logger.debug('Deleting documents', objectKeys);
      const result = await client.graphql({ query: deleteDocument, variables: { objectKeys } });
      logger.debug('Delete documents result', result);

      // Refresh the document list after deletion
      setIsDocumentsListLoading(true);

      return result.data.deleteDocument;
    } catch (error) {
      setErrorMessage('Failed to delete document(s) - please try again later');
      logger.error('Error deleting documents', error);
      return false;
    }
  };

  const reprocessDocuments = async (objectKeys) => {
    try {
      logger.debug('Reprocessing documents', objectKeys);
      const result = await client.graphql({ query: reprocessDocument, variables: { objectKeys } });
      logger.debug('Reprocess documents result', result);
      // Refresh the document list after reprocessing
      setIsDocumentsListLoading(true);
      return result.data.reprocessDocument;
    } catch (error) {
      setErrorMessage('Failed to reprocess document(s) - please try again later');
      logger.error('Error reprocessing documents', error);
      return false;
    }
  };

  const abortWorkflows = async (objectKeys) => {
    try {
      logger.debug('Aborting workflows for documents', objectKeys);
      const result = await client.graphql({ query: abortWorkflow, variables: { objectKeys } });
      logger.debug('Abort workflows result', result);
      const response = result.data.abortWorkflow;

      // Refresh the document list after aborting
      setIsDocumentsListLoading(true);

      // Show error message if some aborts failed but not all
      if (response.failedCount > 0 && response.abortedCount > 0) {
        setErrorMessage(`Aborted ${response.abortedCount} document(s), but ${response.failedCount} failed`);
      } else if (response.failedCount > 0 && response.abortedCount === 0) {
        setErrorMessage(`Failed to abort document(s): ${response.errors?.join(', ') || 'Unknown error'}`);
      }

      return response;
    } catch (error) {
      setErrorMessage('Failed to abort workflow(s) - please try again later');
      logger.error('Error aborting workflows', error);
      return { success: false, abortedCount: 0, failedCount: objectKeys.length, errors: [error.message] };
    }
  };

  return {
    documents,
    isDocumentsListLoading,
    isDocumentListTruncated,
    getDocumentDetailsFromIds,
    setIsDocumentsListLoading,
    setPeriodsToLoad,
    periodsToLoad,
    deleteDocuments,
    reprocessDocuments,
    abortWorkflows,
  };
};

export default useGraphQlApi;
