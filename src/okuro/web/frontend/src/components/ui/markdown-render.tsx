import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Heavy markdown renderer, isolated behind a dynamic import.
 *
 * react-markdown + remark-gfm drag in the full micromark/remark/mdast/hast
 * pipeline (~250 KB). Keeping it here — loaded only via React.lazy from
 * markdown-content.tsx — keeps that weight out of the entry chunk and off
 * the first-paint critical path (the Home inbox preview used to pull it in
 * eagerly). The styled component maps stay in markdown-content (they're
 * light) and are passed in as a prop.
 */
export default function MarkdownRender({
  children,
  components,
}: {
  children: string;
  components: Components;
}) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {children}
    </ReactMarkdown>
  );
}
