import ChunkContent from '@/components/chunk-content';
import HighLightMarkdown from '@/components/highlight-markdown';

export default function SearchResultContent({ content }: { content: string }) {
  const table = /<table(?:\s[^>]*)?>/i.test(content);
  return (
    <div className="docmind-snippet" data-rich={table || undefined}
      role={table ? 'region' : undefined} aria-label={table ? '문서 관련 표' : undefined}
      tabIndex={table ? 0 : undefined}>
      {table ? <ChunkContent content={content} html /> : <HighLightMarkdown>{content}</HighLightMarkdown>}
    </div>
  );
}
