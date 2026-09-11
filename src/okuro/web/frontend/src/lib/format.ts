/** Format seconds into human-readable duration. */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
}

/** Format bytes into human-readable size. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Parse a timestamp from the okuro API.
 *
 * Okuro's DB stores timestamps via SQLite `datetime('now')`, which emits a
 * naive-UTC string like `"2026-04-21 00:29:55"` — no `Z`, no offset. Left
 * to browser default parsing, `new Date()` treats the space-separated form
 * as LOCAL time on Chrome and as Invalid Date on Safari. Both are wrong
 * for our data. Normalise: replace the separator with `T`, append `Z`
 * when no explicit timezone is present.
 */
export function parseApiDate(iso: string): Date {
  if (!iso) return new Date(NaN);
  let s = iso.includes("T") ? iso : iso.replace(" ", "T");
  if (!/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) s += "Z";
  return new Date(s);
}

/** Format ISO timestamp to relative age string. */
export function formatAge(iso: string): string {
  const diff = (Date.now() - parseApiDate(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

/** Format ISO timestamp to short time (HH:MM:SS) in the user's locale. */
export function formatTime(iso: string): string {
  return parseApiDate(iso).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Shorten a task ID for display. */
export function shortId(id: string): string {
  // task-20260413-140023 → 0413-1400
  const m = id.match(/task-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/);
  if (m) return `${m[2]}${m[3]}-${m[4]}${m[5]}`;
  return id.slice(0, 12);
}

/** Compute elapsed seconds from a started_at ISO timestamp. */
export function elapsedSince(iso: string): number {
  return Math.max(0, (Date.now() - parseApiDate(iso).getTime()) / 1000);
}

const DOW_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const DOW_FULL = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function formatClock(h: string, m: string): string {
  const hh = h.padStart(2, "0");
  const mm = m.padStart(2, "0");
  return `${hh}:${mm}`;
}

/**
 * Translate common cron expressions into plain English.
 * Unrecognized patterns fall through to the raw expression so the user
 * can still eyeball them.
 */
export function humanizeCron(expr: string): string {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return expr;
  const m = parts[0]!;
  const h = parts[1]!;
  const dom = parts[2]!;
  const mon = parts[3]!;
  const dow = parts[4]!;

  // Exact every-minute
  if (m === "*" && h === "*" && dom === "*" && mon === "*" && dow === "*") {
    return "Every minute";
  }
  // Every N minutes: */5 * * * *
  if (m.startsWith("*/") && h === "*" && dom === "*" && mon === "*" && dow === "*") {
    return `Every ${m.slice(2)} minutes`;
  }
  // Hourly at :MM
  if (/^\d+$/.test(m) && h === "*" && dom === "*" && mon === "*" && dow === "*") {
    return m === "0" ? "Every hour" : `Every hour at :${m.padStart(2, "0")}`;
  }
  // Every N hours at :MM — e.g. "45 */6 * * *"
  if (/^\d+$/.test(m) && h.startsWith("*/") && dom === "*" && mon === "*" && dow === "*") {
    const step = h.slice(2);
    const at = m === "0" ? "" : ` at :${m.padStart(2, "0")}`;
    return `Every ${step} hours${at}`;
  }
  // Daily at HH:MM
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === "*" && mon === "*" && dow === "*") {
    return `Daily at ${formatClock(h, m)}`;
  }
  // Weekly on a single day: 0 5 * * 1
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === "*" && mon === "*" && /^\d$/.test(dow)) {
    return `Every ${DOW_FULL[+dow % 7]} at ${formatClock(h, m)}`;
  }
  // Weekdays: 0 5 * * 1-5
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === "*" && mon === "*" && dow === "1-5") {
    return `Weekdays at ${formatClock(h, m)}`;
  }
  // Weekends
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === "*" && mon === "*" && (dow === "0,6" || dow === "6,0")) {
    return `Weekends at ${formatClock(h, m)}`;
  }
  // Multi-day: 0 5 * * 1,3,5
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === "*" && mon === "*" && /^\d(,\d)+$/.test(dow)) {
    const days = dow.split(",").map((d) => DOW_NAMES[+d % 7]).join(", ");
    return `${days} at ${formatClock(h, m)}`;
  }
  // Monthly on day N: 0 5 1 * *
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && /^\d+$/.test(dom) && mon === "*" && dow === "*") {
    const suffix = (n: number) => {
      if (n >= 11 && n <= 13) return "th";
      const last = n % 10;
      return last === 1 ? "st" : last === 2 ? "nd" : last === 3 ? "rd" : "th";
    };
    const d = +dom;
    return `Monthly on the ${d}${suffix(d)} at ${formatClock(h, m)}`;
  }

  return expr;
}

const SYSTEMD_DOW: Record<string, string> = {
  mon: "Monday",
  tue: "Tuesday",
  wed: "Wednesday",
  thu: "Thursday",
  fri: "Friday",
  sat: "Saturday",
  sun: "Sunday",
};

/**
 * Translate a systemd ``OnCalendar``/``OnBootSec`` schedule into plain
 * English — the timer-panel counterpart to {@link humanizeCron}.
 *
 * Handles the shapes systemd's ``TimersCalendar=`` field actually emits for
 * okuro's units: ``*-*-* 02:00:00`` (daily), ``Sun *-*-* 03:00:00``
 * (weekly), ``*-*-01 03:00:00`` (monthly), and the relative
 * ``2min after boot`` form. Anything else falls through to the raw string so
 * the user can still eyeball it.
 */
export function humanizeSystemdCalendar(expr: string): string {
  const raw = expr.trim();
  if (!raw) return raw;

  // Relative-to-boot form is already readable — normalise spacing only.
  const boot = raw.match(/^(.+?)\s+after boot$/i);
  if (boot) return `${boot[1]} after boot`;

  // systemd shorthands. These are documented aliases, not date expressions,
  // so they never reach the pattern match below.
  const shorthand = SYSTEMD_SHORTHAND[raw.toLowerCase()];
  if (shorthand) return shorthand;

  // Weekday RANGE or list before the date: "Mon..Fri *-*-* 09:00:00",
  // "Mon,Wed *-*-* 09:00:00". Handled ahead of the single-day pattern, which
  // only understands one three-letter day.
  const ranged = raw.match(
    /^([A-Za-z]{3}(?:\.\.|,)[A-Za-z]{3}(?:,[A-Za-z]{3})*)\s+(.*)$/,
  );
  if (ranged) {
    const days = humanizeDowSet(ranged[1]!);
    const rest = humanizeSystemdCalendar(ranged[2]!);
    if (days && rest.startsWith("Daily at ")) {
      return `${days} at ${rest.slice("Daily at ".length)}`;
    }
  }

  // Step syntax in the hour field: "*-*-* 00/6:00:00" = every 6 hours.
  const step = raw.match(/^\*-\*-\*\s+(\d{1,2})\/(\d{1,2}):(\d{2})(?::\d{2})?$/);
  if (step) {
    const start = +step[1]!;
    const every = +step[2]!;
    const min = step[3]!;
    const at = min === "00" ? "" : ` at :${min}`;
    const from = start === 0 ? "" : ` from ${String(start).padStart(2, "0")}:00`;
    return `Every ${every} hours${at}${from}`;
  }

  // [DOW ]YYYY-MM-DD HH:MM[:SS]
  const m = raw.match(
    /^(?:([A-Za-z]{3})(?:,[A-Za-z]{3})*\s+)?(\*|\d{4})-(\*|\d{2})-(\*|\d{2})\s+(\d{2}):(\d{2})(?::\d{2})?$/,
  );
  if (!m) return raw;
  const dow = m[1];
  const mon = m[3]!;
  const dom = m[4]!;
  const at = `${m[5]}:${m[6]}`;

  if (dow) {
    const day = SYSTEMD_DOW[dow.toLowerCase()];
    if (day) return `Every ${day} at ${at}`;
  }
  if (dom !== "*") return `Monthly on the ${+dom} at ${at}`;
  if (mon !== "*") return `Yearly in month ${+mon} at ${at}`;
  return `Daily at ${at}`;
}

/** systemd's documented calendar aliases. */
const SYSTEMD_SHORTHAND: Record<string, string> = {
  minutely: "Every minute",
  hourly: "Every hour",
  daily: "Daily at 00:00",
  midnight: "Daily at 00:00",
  weekly: "Every Monday at 00:00",
  monthly: "Monthly on the 1st at 00:00",
  quarterly: "Quarterly at 00:00",
  semiannually: "Twice a year at 00:00",
  yearly: "Yearly on 1 January at 00:00",
  annually: "Yearly on 1 January at 00:00",
};

/** "Mon..Fri" / "Mon,Wed,Fri" -> "Weekdays" / "Mondays, Wednesdays and Fridays". */
function humanizeDowSet(spec: string): string | null {
  const order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  let keys: string[];
  if (spec.includes("..")) {
    const [a, b] = spec.toLowerCase().split("..");
    const i = order.indexOf(a!.trim());
    const j = order.indexOf(b!.trim());
    if (i < 0 || j < 0 || j < i) return null;
    keys = order.slice(i, j + 1);
  } else {
    keys = spec.toLowerCase().split(",").map((d) => d.trim());
    if (keys.some((k) => !order.includes(k))) return null;
  }
  if (keys.length === 5 && keys.every((k) => order.indexOf(k) < 5)) return "Weekdays";
  if (keys.length === 2 && keys.includes("sat") && keys.includes("sun")) return "Weekends";
  if (keys.length === 7) return "Daily";
  const names = keys.map((k) => `${SYSTEMD_DOW[k]}s`);
  if (names.some((n) => n === "undefineds")) return null;
  return names.length === 1
    ? names[0]!
    : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

const MEMORY_TOPIC_LABELS: Record<string, string> = {
  gotcha: "Lesson",
  convention: "Convention",
  decision: "Decision",
  learning: "Learning",
  architecture: "Architecture note",
  followups: "Follow-up",
  followup: "Follow-up",
};

/**
 * Map a raw memory topic (gotcha/architecture/etc.) to a display label.
 * Unknown topics are title-cased and hyphens replaced with spaces.
 */
export function memoryTopicLabel(topic: string | null | undefined): string {
  if (!topic) return "Note";
  const key = topic.trim().toLowerCase();
  if (MEMORY_TOPIC_LABELS[key]) return MEMORY_TOPIC_LABELS[key];
  return key
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Clean up the `proposed_what` string on reminder suggestion cards.
 * The backend tacks " (open 3d)" onto the tail — strip it for display.
 */
export function cleanSuggestionTitle(text: string): string {
  return text.replace(/\s*\(open \d+d\)\s*$/i, "").trim();
}

/**
 * Clean up the `reason` string on reminder suggestion cards.
 * The backend prepends "Unresolved for 3d. Full: " — strip it.
 */
export function cleanSuggestionReason(text: string): string {
  return text
    .replace(/^Unresolved for \d+d\.\s*/i, "")
    .replace(/^Full:\s*/i, "")
    .trim();
}

/**
 * Strip the `orch-` prefix that okuro uses for orchestrator subagents.
 * `orch-frontend-engineer` → `frontend-engineer`. Returns `—` for
 * null/empty/`unknown` so the UI doesn't surface junk labels.
 */
export function displayAgent(agent: string | null | undefined): string {
  if (!agent) return "—";
  const s = agent.trim();
  if (!s || s.toLowerCase() === "unknown") return "—";
  return s.replace(/^orch-/, "");
}

/**
 * Render a kebab-case role/kind id as words a reader doesn't have to parse
 * ("linux-audio-engineer" -> "linux audio engineer"). Mirrors gate_messages.py's
 * `roles_txt` construction (`.replace("-", " ")`) so BE and FE agree on the
 * same role — evidence inventory: BlockerCard rendered raw role ids in
 * `font-mono`, "the same way the subtask_approval branch renders a role — a
 * card should never make the reader parse kebab-case" (ROCK-SOLID v5 P1.5).
 */
export function humanizeSlug(id: string): string {
  return id.replace(/-/g, " ");
}

/**
 * The human-readable summary line of a subtask description — the FE
 * counterpart of gate_messages.py's `_summary_line` (ROCK-SOLID v5 P1.5).
 *
 * A subtask `description` is a multi-step agent brief: a summary line, then
 * numbered instructions addressed to the agent. Printing it raw (as
 * ApprovalBanner did) shows the reader step 1 of someone else's instructions
 * — deliberately the first LINE, not the first sentence: real briefs open
 * with a context clause and put the actual work in sentence two.
 */
export function summaryLine(text: string | null | undefined, cap = 200): string {
  let body = (text ?? "").trim();
  // Numbered steps start the machine-facing half — cut there.
  body = body.split(/\n\s*\d+[.)]\s/)[0]!.trim();
  body = body.split("\n")[0]!.trim().replace(/[\s:]+$/, "");
  if (body.length <= cap) return body;
  return `${body.slice(0, cap - 1).trimEnd()}…`;
}
