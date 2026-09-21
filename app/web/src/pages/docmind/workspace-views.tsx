import { lazy } from 'react';

// Load the existing large viewers only when their workspace view is opened.
export const ChunkInspection = lazy(
  () =>
    import('@/pages/chunk/parsed-result/add-knowledge/components/knowledge-chunk'),
);
export const WorkspaceSettings = lazy(() => import('./workspace-settings'));
