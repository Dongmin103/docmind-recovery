import DOMPurify from 'dompurify';
import './index.less';

// Source presentation has no active URLs, media, controls or source-supplied CSS.
const SourceDisplayConfig = {
  ALLOWED_TAGS: [
    'p',
    'br',
    'hr',
    'div',
    'span',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'strong',
    'b',
    'em',
    'i',
    'u',
    's',
    'del',
    'blockquote',
    'ul',
    'ol',
    'li',
    'pre',
    'code',
    'a',
    'sup',
    'sub',
    'table',
    'thead',
    'tbody',
    'tfoot',
    'tr',
    'th',
    'td',
    'caption',
    'colgroup',
    'col',
  ],
  ALLOWED_ATTR: ['rowspan', 'colspan', 'scope', 'span', 'start', 'title'],
  ALLOW_DATA_ATTR: true,
  ALLOW_ARIA_ATTR: false,
  FORBID_TAGS: [
    'script',
    'style',
    'svg',
    'math',
    'iframe',
    'object',
    'embed',
    'img',
  ],
};

export default function ChunkContent({
  content,
  html = false,
}: {
  content: string;
  html?: boolean;
}) {
  const isHtml = html || /<\/?[a-z][^>]*>/i.test(content);
  return (
    <div className="chunk-content min-w-0 max-w-full overflow-x-auto text-base leading-7 text-text-primary">
      {isHtml ? (
        <div
          className="whitespace-pre-wrap break-words"
          dangerouslySetInnerHTML={{
            __html: DOMPurify.sanitize(content, SourceDisplayConfig),
          }}
        />
      ) : (
        <div className="whitespace-pre-wrap break-words">{content}</div>
      )}
    </div>
  );
}
