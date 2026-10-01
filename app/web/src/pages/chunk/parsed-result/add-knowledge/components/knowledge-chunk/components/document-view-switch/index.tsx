import DocumentPreview from '@/components/document-preview';
import TemporaryOriginalPreview from '@/components/document-preview/temporary-original-preview';
import DocumentHeader from '@/components/document-preview/document-header';
import { Segmented, type SegmentedValue } from '@/components/ui/segmented';
import Representation, {
  type ClickableNode,
} from '@/pages/chunk/representation';
import { File, LayoutList } from 'lucide-react';
import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IHighlight } from 'react-pdf-highlighter';

enum ViewMode {
  Preview = 'preview',
  Representations = 'representations',
}

interface DocumentViewSwitchProps {
  documentInfo?: {
    size: number;
    name: string;
    create_date: string;
  };
  fileType: string;
  highlights: IHighlight[];
  setWidthAndHeight: (width: number, height: number) => void;
  url: string;
  documentId?: string;
  sourceVersionId?: string;
  chunkSetId?: string;
  temporaryOriginal?: boolean;
  onChunkSetChanged?: () => void;
  onChunkIdsChange?: (chunkIds: string[]) => void;
}

export default function DocumentViewSwitch({
  documentInfo,
  fileType,
  highlights,
  setWidthAndHeight,
  url,
  documentId,
  sourceVersionId,
  chunkSetId,
  temporaryOriginal,
  onChunkSetChanged,
  onChunkIdsChange,
}: DocumentViewSwitchProps) {
  const { t } = useTranslation();
  const [viewMode, setViewMode] = useState<ViewMode>(ViewMode.Preview);

  const handleNodeClick = useCallback(
    (node: ClickableNode) => {
      onChunkIdsChange?.(node.source_chunk_ids ?? []);
    },
    [onChunkIdsChange],
  );

  const handleViewModeChange = useCallback(
    (value: SegmentedValue) => {
      setViewMode(value as ViewMode);
      if (value === ViewMode.Preview && viewMode !== ViewMode.Preview) {
        onChunkIdsChange?.([]);
      }
    },
    [onChunkIdsChange, viewMode],
  );

  const options = [
    {
      value: ViewMode.Preview,
      label: (
        <div className="flex items-center gap-1">
          <File className="h-4 w-4" />
          <span>{t('common.preview', 'Preview')}</span>
        </div>
      ),
    },
    {
      value: ViewMode.Representations,
      label: (
        <div className="flex items-center gap-1">
          <LayoutList className="h-4 w-4" />
          <span>Artifact</span>
        </div>
      ),
    },
  ];

  return (
    <>
      <DocumentHeader
        className="w-full min-w-0 @lg:flex-1 @lg:w-auto"
        wrapperClassName="flex flex-col items-stretch @lg:flex-row @lg:items-center @lg:justify-between p-5 pb-0 gap-2"
        size={documentInfo?.size ?? 0}
        name={documentInfo?.name ?? ''}
        create_date={documentInfo?.create_date ?? ''}
      >
        <Segmented
          options={options}
          value={viewMode}
          onChange={handleViewModeChange}
        />
      </DocumentHeader>

      <div className="flex-1 h-0 min-h-0 overflow-hidden p-5 pt-2.5 [&>section]:h-full [&>section]:min-h-0">
        {temporaryOriginal && documentId ? (
          <>
            <div hidden={viewMode !== ViewMode.Preview} className="h-full min-h-0">
            <TemporaryOriginalPreview
              key={`${documentId}:${sourceVersionId ?? ''}`}
              documentId={documentId}
              sourceFormat={fileType}
              sourceVersionId={sourceVersionId}
              chunkSetId={chunkSetId}
              onChunkSetChanged={onChunkSetChanged}
              className="h-full min-h-0 overflow-auto [&_img]:max-w-full [&_img]:h-auto"
              highlights={highlights}
              setWidthAndHeight={setWidthAndHeight}
            />
            </div>
            {viewMode === ViewMode.Representations && (
              <Representation onNodeClick={handleNodeClick} />
            )}
          </>
        ) : viewMode === ViewMode.Preview ? (
          <DocumentPreview
            className="h-full min-h-0 overflow-auto [&_img]:max-w-full [&_img]:h-auto"
            fileType={fileType}
            highlights={highlights}
            setWidthAndHeight={setWidthAndHeight}
            url={url}
          />
        ) : (
          <Representation onNodeClick={handleNodeClick} />
        )}
      </div>
    </>
  );
}
