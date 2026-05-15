"use client";

import { useMemo } from "react";
import { cn } from "@/lib/utils";
import {
  parseCostAssistantMessage,
  type ParsedCostMessage,
} from "@/lib/parse-cost-message";

function humanizeKey(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function isNumericish(s: string): boolean {
  if (!s.trim()) return false;
  return /^-?[\d,]+(\.\d+)?$/.test(s.trim().replace(/,/g, ""));
}

function TextBlock({
  className,
  children,
}: {
  className?: string;
  children: string;
}) {
  if (!children.trim()) return null;
  return (
    <p className={cn("whitespace-pre-wrap break-words text-sm", className)}>
      {children}
    </p>
  );
}

function CostTable({
  columns,
  rows,
}: {
  columns: string[];
  rows: Record<string, string>[];
}) {
  if (columns.length === 0 && rows.length === 0) {
    return (
      <p className="text-sm italic text-muted-foreground">No rows returned.</p>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border/80 bg-background/50">
      <table className="w-full min-w-[16rem] border-collapse text-sm">
        <caption className="sr-only">Cost query results</caption>
        <thead>
          <tr className="border-b border-border bg-muted/50 text-left">
            {columns.map((col) => (
              <th
                key={col}
                scope="col"
                className="whitespace-nowrap px-3 py-2 font-semibold text-foreground"
              >
                {humanizeKey(col)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={i}
              className="border-b border-border/60 last:border-0 hover:bg-muted/20"
            >
              {columns.map((col) => {
                const cell = row[col] ?? "";
                return (
                  <td
                    key={col}
                    className={cn(
                      "max-w-[20rem] px-3 py-2 align-top break-words",
                      isNumericish(cell) && "text-right font-mono tabular-nums"
                    )}
                  >
                    {cell}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function KeyValueTable({ entries }: { entries: [string, string][] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border/80 bg-background/50">
      <table className="w-full min-w-[12rem] border-collapse text-sm">
        <caption className="sr-only">Cost result details</caption>
        <tbody>
          {entries.map(([k, v]) => (
            <tr
              key={k}
              className="border-b border-border/60 last:border-0 hover:bg-muted/20"
            >
              <th
                scope="row"
                className="whitespace-nowrap px-3 py-2 align-top font-medium text-muted-foreground"
              >
                {humanizeKey(k)}
              </th>
              <td
                className={cn(
                  "px-3 py-2 break-words",
                  isNumericish(v) && "text-right font-mono tabular-nums"
                )}
              >
                {v}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function renderParsed(p: ParsedCostMessage) {
  switch (p.kind) {
    case "plain":
      return <TextBlock>{p.text}</TextBlock>;
    case "table":
      return (
        <div className="flex flex-col gap-3">
          <TextBlock className="text-muted-foreground">{p.preamble}</TextBlock>
          {p.noteBeforeJson ? (
            <TextBlock className="text-muted-foreground">
              {p.noteBeforeJson}
            </TextBlock>
          ) : null}
          <CostTable columns={p.columns} rows={p.rows} />
          {p.footer ? (
            <TextBlock className="text-muted-foreground">{p.footer}</TextBlock>
          ) : null}
        </div>
      );
    case "kv":
      return (
        <div className="flex flex-col gap-3">
          <TextBlock className="text-muted-foreground">{p.preamble}</TextBlock>
          {p.noteBeforeJson ? (
            <TextBlock className="text-muted-foreground">
              {p.noteBeforeJson}
            </TextBlock>
          ) : null}
          <KeyValueTable entries={p.entries} />
          {p.footer ? (
            <TextBlock className="text-muted-foreground">{p.footer}</TextBlock>
          ) : null}
        </div>
      );
    default:
      return null;
  }
}

type CostResultViewProps = {
  content: string;
  /** While streaming, avoid parsing partial JSON — show raw text. */
  deferStructured: boolean;
};

export function CostResultView({
  content,
  deferStructured,
}: CostResultViewProps) {
  const parsed = useMemo(() => {
    if (deferStructured || !content) {
      return null;
    }
    return parseCostAssistantMessage(content);
  }, [content, deferStructured]);

  if (deferStructured || !parsed) {
    return (
      <p className="whitespace-pre-wrap break-words text-sm">{content}</p>
    );
  }

  if (parsed.kind === "plain") {
    return (
      <p className="whitespace-pre-wrap break-words text-sm">{parsed.text}</p>
    );
  }

  return renderParsed(parsed);
}
