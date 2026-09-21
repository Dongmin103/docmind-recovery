import {
  useFetchNextChunkList,
  useSwitchChunk,
} from '@/hooks/use-chunk-request';
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';
import ChunkCard from './components/chunk-card';
import CreatingModal from './components/chunk-creating-modal';
import {
  useChangeChunkTextMode,
  useDeleteChunkByIds,
  useGetChunkHighlights,
  useHandleChunkCardClick,
  useUpdateChunk,
} from './hooks';

import ChunkResultBar from './components/chunk-result-bar';
import CheckboxSets from './components/chunk-result-bar/checkbox-sets';
import DocumentViewSwitch from './components/document-view-switch';
// import DocumentHeader from './components/document-preview/document-header';

import { useGetDocumentUrl } from '@/components/document-preview/hooks';
import { PageHeader } from '@/components/page-header';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import message from '@/components/ui/message';
import {
  RAGFlowPagination,
  RAGFlowPaginationType,
} from '@/components/ui/ragflow-pagination';
import { ResizableHandle, ResizablePanel } from '@/components/ui/resizable';
import { Spin } from '@/components/ui/spin';
import {
  QueryStringMap,
  useNavigatePage,
} from '@/hooks/logic-hooks/navigate-hooks';
import { InspectionSource, useInspectionSource } from '@/hooks/route-hook';
import { cn } from '@/lib/utils';
import { LucideArrowBigLeft } from 'lucide-react';
import { useSize } from 'ahooks';
import {
  PanelGroup,
  type ImperativePanelGroupHandle,
} from 'react-resizable-panels';
import { getExtension } from '@/utils/document-util';

export interface ChunkProps {
  embedded?: boolean;
  readOnly?: boolean;
  onBack?: () => void;
}

function Chunk({ embedded = false, readOnly = false, onBack }: ChunkProps) {
  const [layoutContainer, setLayoutContainer] = useState<HTMLDivElement | null>(
    null,
  );
  const layoutSize = useSize(layoutContainer);
  const narrowLayout = !!layoutSize?.width && layoutSize.width < 900;
  const panelGroupRef = useRef<ImperativePanelGroupHandle>(null);
  const [filterChunkIds, setFilterChunkIds] = useState<string[]>([]);
  const [selectedChunkIds, setSelectedChunkIds] = useState<string[]>([]);
  const { removeChunk } = useDeleteChunkByIds();
  const {
    data: { documentInfo, data = [], total },
    pagination,
    loading,
    searchString,
    handleInputChange,
    available,
    handleSetAvailable,
    dataUpdatedAt,
    error: inspectionError,
    retry: retryChunkList,
  } = useFetchNextChunkList(true, { chunkIds: filterChunkIds });
  useLayoutEffect(() => {
    panelGroupRef.current?.setLayout(narrowLayout ? [70, 30] : [40, 60]);
  }, [narrowLayout, inspectionError]);

  const { handleChunkCardClick, selectedChunkId } = useHandleChunkCardClick();

  const { t } = useTranslation();
  const { changeChunkTextMode, textMode } = useChangeChunkTextMode();
  const { switchChunk } = useSwitchChunk();
  const [chunkList, setChunkList] = useState(data);
  const {
    chunkUpdatingLoading,
    onChunkUpdatingOk,
    showChunkUpdatingModal,
    hideChunkUpdatingModal,
    chunkId,
    chunkUpdatingVisible,
    documentId,
  } = useUpdateChunk();
  const { navigateToDataFile, getQueryString } = useNavigatePage();
  const inspectionSource = useInspectionSource();
  const fileUrl = useGetDocumentUrl(false);
  const handleBack =
    onBack ?? navigateToDataFile(getQueryString(QueryStringMap.id) as string);
  const retryInspection = () => {
    void retryChunkList?.();
  };
  useEffect(() => {
    setChunkList(data);
  }, [data]);
  const onPaginationChange: RAGFlowPaginationType['onChange'] = (
    page,
    size,
  ) => {
    setSelectedChunkIds([]);
    pagination.onChange?.(page, size);
  };

  const selectAllChunk = useCallback(
    (checked: boolean) => {
      setSelectedChunkIds(checked ? data.map((x) => x.chunk_id) : []);
    },
    [data],
  );

  const handleSingleCheckboxClick = useCallback(
    (chunkId: string, checked: boolean) => {
      setSelectedChunkIds((previousIds) => {
        const idx = previousIds.findIndex((x) => x === chunkId);
        const nextIds = [...previousIds];
        if (checked && idx === -1) {
          nextIds.push(chunkId);
        } else if (!checked && idx !== -1) {
          nextIds.splice(idx, 1);
        }
        return nextIds;
      });
    },
    [],
  );

  const handleChunkIdsChange = useCallback(
    (chunkIds: string[]) => {
      setFilterChunkIds(chunkIds);
      if (chunkIds.length === 0) {
        pagination.onChange?.(1, pagination.pageSize);
      }
    },
    [pagination],
  );

  const showSelectedChunkWarning = useCallback(() => {
    message.warning(t('message.pleaseSelectChunk'));
  }, [t]);

  const handleRemoveChunk = useCallback(async () => {
    if (readOnly) {
      return;
    }

    if (selectedChunkIds.length > 0) {
      const resCode: number = await removeChunk(selectedChunkIds, documentId);
      if (resCode === 0) {
        setSelectedChunkIds([]);
      }
    } else {
      showSelectedChunkWarning();
    }
  }, [
    readOnly,
    selectedChunkIds,
    documentId,
    removeChunk,
    showSelectedChunkWarning,
  ]);

  const handleSwitchChunk = useCallback(
    async (available?: number, chunkIds?: string[]) => {
      if (readOnly) {
        return;
      }

      let ids = chunkIds;
      if (!chunkIds) {
        ids = selectedChunkIds;
        if (selectedChunkIds.length === 0) {
          showSelectedChunkWarning();
          return;
        }
      }

      const resCode: number = await switchChunk({
        chunk_ids: ids,
        available_int: available,
        doc_id: documentId,
      });
      if (ids?.length && resCode === 0) {
        chunkList.forEach((x: any) => {
          if (ids.indexOf(x['chunk_id']) > -1) {
            x['available_int'] = available;
          }
        });
        setChunkList(chunkList);
      }
    },
    [
      switchChunk,
      documentId,
      selectedChunkIds,
      showSelectedChunkWarning,
      chunkList,
      readOnly,
    ],
  );

  const { highlights, setWidthAndHeight } =
    useGetChunkHighlights(selectedChunkId);

  const fileType = useMemo(() => {
    switch (documentInfo?.type) {
      case 'doc':
        return getExtension(documentInfo?.name ?? '') || 'doc';
      case 'visual':
        return getExtension(documentInfo?.name ?? '') || 'visual';
      case 'docx':
      case 'txt':
      case 'md':
      case 'mdx':
      case 'pdf':
        return documentInfo?.type;
    }
    return 'unknown';
  }, [documentInfo]);

  return (
    <main
      className={cn('flex flex-col', embedded ? 'h-full min-h-0' : 'h-dvh')}
      data-embedded={embedded || undefined}
    >
      {embedded ? (
        <header className="flex shrink-0 items-center gap-3 border-b-0.5 border-border-button bg-bg-base px-5 py-3">
          <Button variant="outline" size="sm" onClick={handleBack}>
            <LucideArrowBigLeft />
            {t('common.back')}
          </Button>
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-text-primary">
              {documentInfo?.name}
            </div>
            <div className="text-xs text-text-secondary">
              {t('chunk.chunkResult')}
            </div>
          </div>
          {readOnly && (
            <Badge variant="secondary" className="ml-auto shrink-0">
              보기 전용
            </Badge>
          )}
        </header>
      ) : (
        <PageHeader>
          <div className="flex items-center gap-3">
            <Button variant="outline" onClick={handleBack}>
              <LucideArrowBigLeft />
              {t('common.back')}
            </Button>
            {inspectionSource === InspectionSource.DocMind && (
              <Badge variant="secondary">DocMind에서 연 인덱싱 검사</Badge>
            )}
          </div>
        </PageHeader>
      )}

      {inspectionError ? (
        <section
          role="alert"
          className="m-5 rounded-lg border border-border-button p-5"
        >
          <h2 className="font-semibold">문서를 불러오지 못했습니다.</h2>
          <p className="mt-2 text-sm text-text-secondary">
            {inspectionError.message}
          </p>
          <p className="mt-1 text-sm text-text-secondary">
            문서가 삭제되었거나 접근 권한이 변경되었을 수 있습니다.
          </p>
          <Button
            className="mt-4"
            variant="outline"
            onClick={retryInspection}
            disabled={loading}
          >
            다시 불러오기
          </Button>
        </section>
      ) : (
        <Card
          className={cn(
            'flex-1 h-0 min-h-0 p-0 bg-transparent shadow-none',
            embedded ? 'm-0 rounded-none border-0' : 'mx-5 mb-5',
          )}
        >
          <CardContent
            ref={setLayoutContainer}
            className="p-0 h-full min-w-0 flex"
          >
            <PanelGroup
              ref={panelGroupRef}
              direction={narrowLayout ? 'vertical' : 'horizontal'}
              className="flex h-full w-full flex-1 min-w-0 data-[panel-group-direction=vertical]:flex-col"
            >
              <ResizablePanel defaultSize={narrowLayout ? 70 : 40} minSize={30}>
                <article className="@container h-full min-w-0 flex flex-col">
                  <DocumentViewSwitch
                    documentInfo={documentInfo}
                    fileType={fileType}
                    highlights={highlights}
                    setWidthAndHeight={setWidthAndHeight}
                    url={fileUrl}
                    onChunkIdsChange={handleChunkIdsChange}
                  />
                </article>
              </ResizablePanel>

              <ResizableHandle
                withHandle
                className={
                  narrowLayout
                    ? 'bg-border-button h-[0.5px]'
                    : 'bg-border-button w-[0.5px]'
                }
              />

              <ResizablePanel defaultSize={narrowLayout ? 30 : 60} minSize={30}>
                <article className="h-full flex flex-col">
                  <header className="flex-0 p-5 pb-2.5 border-b-0.5 border-b-border-button">
                    <h2 className="text-[24px]">{t('chunk.chunkResult')}</h2>
                    <div className="text-[14px] text-text-secondary">
                      {t('chunk.chunkResultTip')}
                    </div>
                  </header>

                  <Spin spinning={loading} className="flex-1 h-0" size="large">
                    <div className="relative @container h-full px-5 pb-5 overflow-hidden flex flex-col">
                      <div
                        className="
                        sticky top-0 z-[1] bg-bg-base space-y-4 py-5
                        @4xl:flex @4xl:justify-between @4xl:items-center
                        @4xl:space-y-0 @4xl:gap-4
                      "
                        role="toolbar"
                      >
                        <ChunkResultBar
                          className="@4xl:order-2"
                          handleInputChange={handleInputChange}
                          searchString={searchString}
                          changeChunkTextMode={changeChunkTextMode}
                          createChunk={showChunkUpdatingModal}
                          available={available}
                          selectAllChunk={selectAllChunk}
                          handleSetAvailable={handleSetAvailable}
                          readOnly={readOnly}
                        />

                        {!readOnly && (
                          <CheckboxSets
                            className="h-8"
                            selectAllChunk={selectAllChunk}
                            switchChunk={handleSwitchChunk}
                            removeChunk={handleRemoveChunk}
                            checked={selectedChunkIds.length === data.length}
                            selectedChunkIds={selectedChunkIds}
                          />
                        )}
                      </div>

                      <div className="space-y-4 flex-1 overflow-y-auto min-h-0">
                        {chunkList.map((item) => (
                          <ChunkCard
                            item={item}
                            key={item.chunk_id}
                            editChunk={showChunkUpdatingModal}
                            checked={selectedChunkIds.some(
                              (x) => x === item.chunk_id,
                            )}
                            handleCheckboxClick={handleSingleCheckboxClick}
                            switchChunk={handleSwitchChunk}
                            clickChunkCard={handleChunkCardClick}
                            selected={item.chunk_id === selectedChunkId}
                            textMode={textMode}
                            t={dataUpdatedAt}
                            readOnly={readOnly}
                          />
                        ))}
                      </div>

                      <footer className="mt-5">
                        <RAGFlowPagination
                          pageSize={pagination.pageSize}
                          current={pagination.current}
                          total={total}
                          onChange={(page, pageSize) => {
                            onPaginationChange(page, pageSize);
                          }}
                        />
                      </footer>
                    </div>
                  </Spin>
                </article>
              </ResizablePanel>
            </PanelGroup>
          </CardContent>
        </Card>
      )}

      {!readOnly && chunkUpdatingVisible && (
        <CreatingModal
          doc_id={documentId}
          chunkId={chunkId}
          hideModal={hideChunkUpdatingModal}
          visible={chunkUpdatingVisible}
          loading={chunkUpdatingLoading}
          onOk={onChunkUpdatingOk}
          parserId={documentInfo.parser_id}
        />
      )}
    </main>
  );
}

export default Chunk;
