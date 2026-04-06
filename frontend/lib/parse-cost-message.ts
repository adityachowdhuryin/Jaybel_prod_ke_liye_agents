/**
 * Parse cost-agent assistant text (Source + Result + JSON) for table rendering.
 * Matches agents/cost_agent/main.py task_stream format.
 */

const RESULT_MARKER = "\n\nResult (amounts in INR ₹ where applicable):\n";

/** Preferred column order; remaining keys sorted alphabetically. */
const COLUMN_PRIORITY = [
  "date",
  "usage_date",
  "service_name",
  "sku_description",
  "project_id",
  "region",
  "environment",
  "cost_inr",
  "total_inr",
  "currency",
];

export type ParsedCostMessage =
  | {
      kind: "table";
      preamble: string;
      noteBeforeJson: string;
      columns: string[];
      rows: Record<string, string>[];
      footer: string;
    }
  | {
      kind: "kv";
      preamble: string;
      noteBeforeJson: string;
      entries: [string, string][];
      footer: string;
    }
  | { kind: "plain"; text: string };

function deriveColumnOrder(keys: string[]): string[] {
  const set = new Set(keys);
  const ordered: string[] = [];
  for (const k of COLUMN_PRIORITY) {
    if (set.has(k)) {
      ordered.push(k);
      set.delete(k);
    }
  }
  const rest = Array.from(set).sort((a, b) => a.localeCompare(b));
  return [...ordered, ...rest];
}

/** Merge "Region" vs "region", stray keys, etc., so table columns align. */
function normalizeRow(record: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(record)) {
    const kn = k.trim().toLowerCase().replace(/\s+/g, "_");
    const val = v === null || v === undefined ? "" : String(v).trim();
    const prev = out[kn];
    if (prev === undefined || prev === "") {
      out[kn] = val;
    }
  }
  return out;
}

/**
 * Find first balanced JSON object or array starting at `[` or `{`, respecting strings.
 */
function extractFirstJson(s: string): {
  value: unknown;
  restBefore: string;
  restAfter: string;
} | null {
  const start = s.search(/[[{]/);
  if (start === -1) return null;

  const stack: string[] = [];
  const pair: Record<string, string> = { "[": "]", "{": "}" };
  let inString = false;
  let escape = false;

  for (let i = start; i < s.length; i++) {
    const ch = s[i];
    if (inString) {
      if (escape) {
        escape = false;
      } else if (ch === "\\") {
        escape = true;
      } else if (ch === '"') {
        inString = false;
      }
      continue;
    }
    if (ch === '"') {
      inString = true;
      continue;
    }
    if (ch === "[" || ch === "{") {
      stack.push(pair[ch]);
      continue;
    }
    if (ch === "]" || ch === "}") {
      if (stack.length === 0 || stack[stack.length - 1] !== ch) {
        return null;
      }
      stack.pop();
      if (stack.length === 0) {
        const slice = s.slice(start, i + 1);
        try {
          return {
            value: JSON.parse(slice),
            restBefore: s.slice(0, start),
            restAfter: s.slice(i + 1),
          };
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

export function parseCostAssistantMessage(raw: string): ParsedCostMessage {
  const idx = raw.indexOf(RESULT_MARKER);
  if (idx === -1) {
    return { kind: "plain", text: raw };
  }

  const preamble = raw.slice(0, idx).trimEnd();
  const afterMarker = raw.slice(idx + RESULT_MARKER.length);

  const extracted = extractFirstJson(afterMarker);
  if (!extracted) {
    return { kind: "plain", text: raw };
  }

  const { value, restBefore, restAfter } = extracted;
  const noteBeforeJson = restBefore.trim();
  const footer = restAfter.trim();

  if (Array.isArray(value)) {
    if (value.length === 0) {
      return {
        kind: "table",
        preamble,
        noteBeforeJson,
        columns: [],
        rows: [],
        footer,
      };
    }
    const allObjects = value.every(
      (x) => x !== null && typeof x === "object" && !Array.isArray(x)
    );
    if (!allObjects) {
      return { kind: "plain", text: raw };
    }
    const rows = value.map((row) =>
      normalizeRow(row as Record<string, unknown>)
    );
    const allKeys = new Set<string>();
    for (const r of rows) {
      Object.keys(r).forEach((k) => allKeys.add(k));
    }
    const columns = deriveColumnOrder(Array.from(allKeys));
    return {
      kind: "table",
      preamble,
      noteBeforeJson,
      columns,
      rows,
      footer,
    };
  }

  if (value !== null && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    const keys = deriveColumnOrder(Object.keys(obj));
    const entries: [string, string][] = keys.map((k) => [
      k,
      obj[k] === null || obj[k] === undefined ? "" : String(obj[k]),
    ]);
    return {
      kind: "kv",
      preamble,
      noteBeforeJson,
      entries,
      footer,
    };
  }

  return { kind: "plain", text: raw };
}
