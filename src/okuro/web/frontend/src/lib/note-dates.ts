import { parseApiDate } from "@/lib/format";
// SQLite datetime('now') is UTC "YYYY-MM-DD HH:MM:SS" with no tz marker; JS would
// read the bare string as local time, so normalise to an explicit UTC instant.
export function parseTs(s?: string): Date | null {
  if (!s) return null;
  const d = parseApiDate(s.trim());
  return isNaN(d.getTime()) ? null : d;
}

/** dd.mm.yyyy — the note header shows dates, not clock times. */
export function dmy(s?: string): string | null {
  const d = parseTs(s);
  if (!d) return null;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()}`;
}
