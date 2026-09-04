import { Fragment, type JSX } from 'react';

/**
 * A small Markdown renderer for the subset the minutes prompt asks for:
 * headings, lists, tables, bold/italic/code and horizontal rules. Model output
 * becomes React elements rather than injected HTML, so there is no parser or
 * sanitiser to ship and no path to inject markup.
 */
export function Markdown({ source }: { source: string }): JSX.Element {
  return <div className="markdown">{renderBlocks(source.split('\n'))}</div>;
}

function renderBlocks(lines: string[]): JSX.Element[] {
  const blocks: JSX.Element[] = [];
  let index = 0;
  let key = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) {
      index += 1;
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      const Tag = `h${Math.min(level + 1, 6)}` as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={key++}>{inline(heading[2])}</Tag>);
      index += 1;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      blocks.push(<hr key={key++} />);
      index += 1;
      continue;
    }

    if (line.trim().startsWith('|') && lines[index + 1]?.trim().startsWith('|')) {
      const table: string[] = [];
      while (index < lines.length && lines[index].trim().startsWith('|')) {
        table.push(lines[index]);
        index += 1;
      }
      blocks.push(<Table key={key++} rows={table} />);
      continue;
    }

    if (/^\s*([-*+])\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*([-*+])\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*([-*+])\s+/, ''));
        index += 1;
      }
      blocks.push(
        <ul key={key++}>
          {items.map((item, i) => (
            <li key={i}>{inline(item)}</li>
          ))}
        </ul>,
      );
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+[.)]\s+/, ''));
        index += 1;
      }
      blocks.push(
        <ol key={key++}>
          {items.map((item, i) => (
            <li key={i}>{inline(item)}</li>
          ))}
        </ol>,
      );
      continue;
    }

    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    blocks.push(<p key={key++}>{inline(paragraph.join(' '))}</p>);
  }

  return blocks;
}

function isBlockStart(line: string): boolean {
  return (
    /^#{1,6}\s/.test(line) ||
    /^\s*([-*+])\s+/.test(line) ||
    /^\s*\d+[.)]\s+/.test(line) ||
    line.trim().startsWith('|') ||
    /^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())
  );
}

function Table({ rows }: { rows: string[] }): JSX.Element {
  const cells = rows.map((row) =>
    row
      .trim()
      .replace(/^\|/, '')
      .replace(/\|$/, '')
      .split('|')
      .map((cell) => cell.trim()),
  );
  const [header, ...rest] = cells;
  const body = rest.filter((row) => !row.every((cell) => /^:?-{2,}:?$/.test(cell)));

  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {header.map((cell, i) => (
              <th key={i}>{inline(cell)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j}>{inline(cell)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g;

function inline(text: string): JSX.Element {
  const parts = text.split(INLINE).filter((part) => part !== '');
  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith('**') && part.endsWith('**')) {
          return <strong key={index}>{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith('`') && part.endsWith('`')) {
          return <code key={index}>{part.slice(1, -1)}</code>;
        }
        if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
          return <em key={index}>{part.slice(1, -1)}</em>;
        }
        return <Fragment key={index}>{part}</Fragment>;
      })}
    </>
  );
}
