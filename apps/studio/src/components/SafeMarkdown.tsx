import type { ReactNode } from "react";

interface SafeMarkdownProps {
  text: string;
}

type BlockNode =
  | { kind: "code"; lang: string; content: string }
  | { kind: "heading"; level: 1 | 2 | 3; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "paragraph"; text: string };

function isSafeHttpUrl(rawHref: string): boolean {
  try {
    const parsed = new URL(rawHref);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const tokenPattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g;
  let lastIndex = 0;
  let partIdx = 0;

  for (const match of text.matchAll(tokenPattern)) {
    const index = match.index ?? 0;
    if (index > lastIndex) {
      nodes.push(text.slice(lastIndex, index));
    }
    const token = match[0];
    const key = `${keyPrefix}-${partIdx++}`;
    if (token.startsWith("`") && token.endsWith("`")) {
      nodes.push(
        <code key={key} className="md-inline-code">
          {token.slice(1, -1)}
        </code>,
      );
    } else if (token.startsWith("**") && token.endsWith("**")) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("*") && token.endsWith("*")) {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>);
    } else if (token.startsWith("[")) {
      const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      if (linkMatch && isSafeHttpUrl(linkMatch[2])) {
        nodes.push(
          <a
            key={key}
            href={linkMatch[2]}
            target="_blank"
            rel="noopener noreferrer"
            className="md-link"
          >
            {linkMatch[1]}
          </a>,
        );
      } else {
        nodes.push(<span key={key}>{linkMatch ? linkMatch[1] : token}</span>);
      }
    } else {
      nodes.push(token);
    }
    lastIndex = index + token.length;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes;
}

function parseBlocks(source: string): BlockNode[] {
  const lines = source.split(/\r?\n/);
  const blocks: BlockNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.trimStart().startsWith("```")) {
      const lang = line.trimStart().slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      if (i < lines.length && lines[i].trimStart().startsWith("```")) {
        i++;
      }
      blocks.push({ kind: "code", lang, content: codeLines.join("\n") });
      continue;
    }

    if (!line.trim()) {
      i++;
      continue;
    }

    const headingMatch = /^(#{1,3})\s+(.*)$/.exec(line);
    if (headingMatch) {
      const level = headingMatch[1].length as 1 | 2 | 3;
      blocks.push({ kind: "heading", level, text: headingMatch[2] });
      i++;
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ""));
        i++;
      }
      blocks.push({ kind: "list", ordered: false, items });
      continue;
    }

    if (/^\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ""));
        i++;
      }
      blocks.push({ kind: "list", ordered: true, items });
      continue;
    }

    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !lines[i].trimStart().startsWith("```") &&
      !/^(#{1,3})\s+/.test(lines[i]) &&
      !/^[-*]\s+/.test(lines[i]) &&
      !/^\d+\.\s+/.test(lines[i])
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    blocks.push({ kind: "paragraph", text: paraLines.join("\n") });
  }

  return blocks;
}

/**
 * Pure React JSX streaming markdown renderer (never uses raw HTML injection).
 */
export function SafeMarkdown({ text }: SafeMarkdownProps) {
  const blocks = parseBlocks(text);

  return (
    <div className="safe-markdown">
      {blocks.map((block, idx) => {
        const key = `block-${idx}`;
        if (block.kind === "code") {
          return (
            <pre key={key} className="md-code-block" data-lang={block.lang || undefined}>
              <code>{block.content}</code>
            </pre>
          );
        }
        if (block.kind === "heading") {
          const Tag = block.level === 1 ? "h3" : block.level === 2 ? "h4" : "h5";
          return (
            <Tag key={key} className="md-heading">
              {renderInline(block.text, key)}
            </Tag>
          );
        }
        if (block.kind === "list") {
          const ListTag = block.ordered ? "ol" : "ul";
          return (
            <ListTag key={key} className="md-list">
              {block.items.map((item, itemIdx) => (
                <li key={`${key}-${itemIdx}`}>{renderInline(item, `${key}-${itemIdx}`)}</li>
              ))}
            </ListTag>
          );
        }
        return (
          <p key={key} className="md-paragraph">
            {renderInline(block.text, key)}
          </p>
        );
      })}
    </div>
  );
}
