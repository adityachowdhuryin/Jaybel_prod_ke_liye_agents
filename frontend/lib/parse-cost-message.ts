/**
 * Parse cost-agent assistant text (Source + Result + JSON) for table rendering.
 * Matches orchestrator Agent Engine bridge output format.
 */

const RESULT_MARKER = "\n\nResult (amounts in INR ₹ where applicable):\n";
const COST_PAYLOAD_MARKER = "COST_PAYLOAD_JSON:\n";

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
  "total_usd",
  "trace_id",
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

function _formatClarification(
  question: string,
  options: string[],
  meta?: { clarificationKind?: string; missingSlots?: string[] },
): string {
  const cleanQuestion = question.trim();
  const cleanOptions = options.map((o) => o.trim()).filter(Boolean);
  let body =
    cleanOptions.length > 0
      ? `${cleanQuestion}\nOptions:\n${cleanOptions.map((o) => `- ${o}`).join("\n")}`
      : cleanQuestion;
  const kind = meta?.clarificationKind?.trim();
  const missing = (meta?.missingSlots ?? []).map((s) => String(s).trim()).filter(Boolean);
  const extras: string[] = [];
  if (kind) extras.push(`Type: ${kind.replace(/_/g, " ")}`);
  if (missing.length) extras.push(`Still need: ${missing.join(", ").replace(/_/g, " ")}`);
  if (extras.length) body += `\n\n${extras.join(" · ")}`;
  return body;
}

function parseStructuredCostPayload(raw: string): ParsedCostMessage | null {
  const markerIdx = raw.indexOf(COST_PAYLOAD_MARKER);
  if (markerIdx === -1) return null;
  const jsonPart = raw.slice(markerIdx + COST_PAYLOAD_MARKER.length).trim();
  const extracted = extractFirstJson(jsonPart);
  if (!extracted || extracted.value === null || typeof extracted.value !== "object") {
    return null;
  }
  const payload = extracted.value as Record<string, unknown>;
  const responseType = String(payload.response_type ?? "").trim().toLowerCase();
  if (responseType === "clarification") {
    const question = String(payload.question ?? "Please clarify your request.");
    const optionsRaw = payload.options;
    const options = Array.isArray(optionsRaw)
      ? optionsRaw.map((x) => String(x))
      : [];
    const clarificationKind = String(payload.clarification_kind ?? "").trim();
    const missingRaw = payload.missing_slots;
    const missingSlots = Array.isArray(missingRaw)
      ? missingRaw.map((x) => String(x))
      : [];
    return {
      kind: "plain",
      text: _formatClarification(question, options, {
        clarificationKind: clarificationKind || undefined,
        missingSlots: missingSlots.length ? missingSlots : undefined,
      }),
    };
  }
  if (responseType === "error") {
    const detail = String(payload.detail ?? "I cannot verify this from current data.");
    const hint = String(payload.hint ?? "").trim();
    return {
      kind: "plain",
      text: hint ? `${detail}\nHint: ${hint}` : detail,
    };
  }
  if (responseType === "result") {
    const data = payload.data;
    if (Array.isArray(data)) {
      const allObjects = data.every(
        (x) => x !== null && typeof x === "object" && !Array.isArray(x)
      );
      if (!allObjects) {
        return { kind: "plain", text: raw };
      }
      const rows = data.map((row) => normalizeRow(row as Record<string, unknown>));
      const allKeys = new Set<string>();
      for (const r of rows) {
        Object.keys(r).forEach((k) => allKeys.add(k));
      }
      return {
        kind: "table",
        preamble: "Cost query results",
        noteBeforeJson: "",
        columns: deriveColumnOrder(Array.from(allKeys)),
        rows,
        footer: "",
      };
    }
    if (data !== null && typeof data === "object") {
      const obj = data as Record<string, unknown>;
      const keys = deriveColumnOrder(Object.keys(obj));
      const entries: [string, string][] = keys.map((k) => [
        k,
        obj[k] === null || obj[k] === undefined ? "" : String(obj[k]),
      ]);
      return {
        kind: "kv",
        preamble: "Cost query result details",
        noteBeforeJson: "",
        entries,
        footer: "",
      };
    }
    return { kind: "plain", text: raw };
  }
  return null;
}

function parseRankedCostList(raw: string): ParsedCostMessage | null {
  const lines = raw.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const rows: Record<string, string>[] = [];
  for (const line of lines) {
    if (!/^\d+\.\s+/.test(line)) continue;
    const body = line.replace(/^\d+\.\s+/, "").trim();
    const amountMatch =
      body.match(/(?:INR|₹)\s*([-+]?[\d,]+(?:\.\d+)?(?:e[-+]?\d+)?)/i) ||
      body.match(/([-+]?[\d,]+(?:\.\d+)?(?:e[-+]?\d+)?)\s*(?:INR|₹)\s*$/i);
    if (!amountMatch) continue;
    const amount = amountMatch[1]?.replace(/,/g, "").trim();
    if (!amount) continue;
    const idx = body.toLowerCase().indexOf(amountMatch[0].toLowerCase());
    let service = idx > -1 ? body.slice(0, idx).trim() : body.trim();
    service = service
      .replace(/[:\-–]\s*$/g, "")
      .replace(/^\*+/, "")
      .replace(/\*+$/g, "")
      .trim();
    if (!service) continue;
    rows.push({
      service_name: service,
      cost_inr: amount,
    });
  }
  if (!rows.length) return null;
  const preamble = raw.split(/\r?\n\r?\n/)[0]?.trim() ?? "";
  return {
    kind: "table",
    preamble,
    noteBeforeJson: "",
    columns: ["service_name", "cost_inr"],
    rows,
    footer: "",
  };
}

function parseBulletServiceList(raw: string): ParsedCostMessage | null {
  const rawLower = raw.toLowerCase();
  if (
    !rawLower.includes("service") ||
    rawLower.includes("clarification_required") ||
    rawLower.includes("options:")
  ) {
    return null;
  }
  const lines = raw.split(/\r?\n/);
  const rows: Record<string, string>[] = [];
  for (const line of lines) {
    const m = line.match(/^\s*[*-]\s+(.+?)\s*$/);
    if (!m) continue;
    rows.push({ service_name: m[1].trim() });
  }
  if (!rows.length) return null;
  const preamble = raw.split(/\r?\n\r?\n/)[0]?.trim() ?? "";
  return {
    kind: "table",
    preamble,
    noteBeforeJson: "",
    columns: ["service_name"],
    rows,
    footer: "",
  };
}

function parseInlineServiceSentence(raw: string): ParsedCostMessage | null {
  const compact = raw.replace(/\s+/g, " ").trim();
  if (/clarification_required|options:/i.test(compact)) return null;
  if (!/services?/i.test(compact) || !compact.includes(",")) return null;
  const colonIdx = compact.indexOf(":");
  if (colonIdx < 0 || colonIdx === compact.length - 1) return null;
  const listPart = compact.slice(colonIdx + 1).trim().replace(/\.$/, "");
  const normalized = listPart.replace(/\s+and\s+/gi, ", ");
  const items = normalized
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
  if (items.length < 3) return null;
  return {
    kind: "table",
    preamble: compact.slice(0, colonIdx + 1).trim(),
    noteBeforeJson: "",
    columns: ["service_name"],
    rows: items.map((service_name) => ({ service_name })),
    footer: "",
  };
}

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

/** Normalize agent/orchestrator variants so COST_PAYLOAD_JSON parses reliably. */
function normalizeCostPayload(raw: string): string {
  const trimmed = raw.trimStart();
  const loose = /^COST_PAYLOAD_JSON:\s*/i.exec(trimmed);
  if (loose && loose.index === 0) {
    const rest = trimmed.slice(loose[0].length).trimStart();
    return `${COST_PAYLOAD_MARKER}${rest}`;
  }
  if (/^\{[\s\S]*"response_type"[\s\S]*\}/.test(trimmed)) {
    try {
      const j = JSON.parse(trimmed) as Record<string, unknown>;
      const rt = String(j.response_type ?? "").toLowerCase();
      if (rt === "clarification" || rt === "error" || rt === "result") {
        return `${COST_PAYLOAD_MARKER}${trimmed}`;
      }
    } catch {
      /* ignore */
    }
  }
  return raw;
}

export function parseCostAssistantMessage(raw: string): ParsedCostMessage {
  const normalized = normalizeCostPayload(raw);
  const structured = parseStructuredCostPayload(normalized);
  if (structured) return structured;

  if (raw.includes("CLARIFICATION_REQUIRED:") || raw.includes("\nOptions:\n- ")) {
    return { kind: "plain", text: raw };
  }

  const idx = raw.indexOf(RESULT_MARKER);
  if (idx === -1) {
    const ranked = parseRankedCostList(raw);
    if (ranked) return ranked;
    const services = parseBulletServiceList(raw);
    if (services) return services;
    const inlineServices = parseInlineServiceSentence(raw);
    if (inlineServices) return inlineServices;
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
