import type { Element, Root, RootContent } from 'hast';
import type { MouseEvent } from 'react';
import { memo } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';

// A deliberately small display vocabulary: no images, controls, styles or embeds.
const DisplaySchema = {
  tagNames: [
    'p',
    'br',
    'hr',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'strong',
    'em',
    'del',
    'blockquote',
    'ul',
    'ol',
    'li',
    'pre',
    'code',
    'a',
    'span',
    'div',
    'table',
    'thead',
    'tbody',
    'tfoot',
    'tr',
    'th',
    'td',
    'sup',
    'sub',
  ],
  attributes: {
    a: ['href', 'title'],
    ol: ['start'],
    th: ['colSpan', 'rowSpan'],
    td: ['colSpan', 'rowSpan'],
  },
  protocols: { href: ['http', 'https', 'mailto'] },
  strip: ['script', 'style', 'iframe', 'object', 'embed', 'svg', 'math'],
};

function citationReferences({ numbers }: { numbers: Set<number> }) {
  return (tree: Root) => {
    const visit = (parent: Root | Element) => {
      if (
        parent.type === 'element' &&
        ['a', 'code', 'pre'].includes(parent.tagName)
      )
        return;
      const children: RootContent[] = [];
      for (const child of parent.children) {
        if (child.type === 'element') visit(child);
        if (child.type !== 'text') {
          children.push(child);
          continue;
        }
        let offset = 0;
        for (const match of child.value.matchAll(
          /(?<!\[)\[([1-9]\d*)\](?!\])/g,
        )) {
          const number = Number(match[1]);
          if (!numbers.has(number)) continue;
          const index = match.index!;
          if (index > offset)
            children.push({
              type: 'text',
              value: child.value.slice(offset, index),
            });
          children.push({
            type: 'element',
            tagName: 'span',
            properties: { dataCitation: number },
            children: [{ type: 'text', value: match[0] }],
          });
          offset = index + match[0].length;
        }
        if (offset < child.value.length)
          children.push({ type: 'text', value: child.value.slice(offset) });
      }
      parent.children = children;
    };
    visit(tree);
  };
}

function AnswerMarkdown({
  content,
  citationNumbers = [],
  onCitation,
}: {
  content: string;
  citationNumbers?: number[];
  onCitation?: (number: number, trigger: HTMLButtonElement) => void;
}) {
  const numbers = new Set(
    citationNumbers.filter(
      (number) => Number.isSafeInteger(number) && number > 0,
    ),
  );
  const selectCitation = (event: MouseEvent<HTMLButtonElement>) => {
    const number = Number(event.currentTarget.dataset.citation);
    if (numbers.has(number)) onCitation?.(number, event.currentTarget);
  };
  return (
    <div className="docmind-answer-markdown min-w-0 text-base leading-7 text-text-primary">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[
          rehypeRaw,
          [rehypeSanitize, DisplaySchema],
          [citationReferences, { numbers }],
        ]}
        components={{
          span: ({ node, children }) =>
            node?.properties.dataCitation && onCitation ? (
              <button
                type="button"
                data-citation={node.properties.dataCitation}
                onClick={selectCitation}
                aria-label={`근거 ${node.properties.dataCitation} 확인`}
                aria-haspopup="dialog"
                className="mx-0.5 rounded bg-accent-primary/10 px-1.5 text-sm font-medium text-accent-primary hover:bg-accent-primary/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
              >
                {children}
              </button>
            ) : (
              <span>{children}</span>
            ),
          a: ({ href, children }) =>
            href ? (
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="text-accent-primary underline"
              >
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          table: ({ children }) => (
            <div
              role="region"
              aria-label="표"
              tabIndex={0}
              className="my-4 max-w-full overflow-x-auto rounded-lg border border-border-button focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-primary"
            >
              <table className="w-full border-collapse text-left text-sm">
                {children}
              </table>
            </div>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export default memo(
  AnswerMarkdown,
  (previous, next) =>
    previous.content === next.content &&
    previous.onCitation === next.onCitation &&
    JSON.stringify(previous.citationNumbers) ===
      JSON.stringify(next.citationNumbers),
);
