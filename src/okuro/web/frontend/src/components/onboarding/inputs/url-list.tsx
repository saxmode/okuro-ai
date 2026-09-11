import { Plus, X } from "lucide-react";
import { clsx } from "clsx";

/**
 * Dynamic list of URL inputs. Always shows one empty row at the bottom; users
 * type, press Enter to commit, the list adds a new blank row. × removes a row.
 * Values are reported as a trimmed, non-empty list.
 *
 * Used by profile_sources (LinkedIn / GitHub / general URLs merged into one
 * free-form URL input) and design (URLs to scrape a visual identity from).
 */
export function UrlList({
  urls,
  onChange,
  placeholder = "https://…",
  max = 6,
}: {
  /** Controlled array — always contains one trailing empty string so the
   *  user can type the next URL without clicking anything. */
  urls: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  max?: number;
}) {
  const update = (idx: number, v: string) => {
    const next = [...urls];
    next[idx] = v;
    onChange(next);
  };

  const addRow = () => {
    if (urls.length >= max) return;
    onChange([...urls, ""]);
  };

  const removeRow = (idx: number) => {
    const next = urls.filter((_, i) => i !== idx);
    onChange(next.length ? next : [""]);
  };

  return (
    <div className="flex flex-col gap-2">
      {urls.map((u, i) => (
        <div key={i} className="flex items-center gap-2">
          <input
            type="url"
            value={u}
            onChange={(e) => update(i, e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                if (u.trim() && i === urls.length - 1 && urls.length < max) addRow();
              }
            }}
            placeholder={placeholder}
            className={clsx(
              "flex-1 bg-transparent border-0 border-b border-border-subtle",
              "focus:border-accent focus:outline-none text-base text-fg-primary",
              "placeholder:text-fg-disabled py-2",
            )}
          />
          <button
            type="button"
            onClick={() => removeRow(i)}
            disabled={urls.length === 1 && !urls[0]}
            aria-label={`Remove URL ${i + 1}`}
            className="rounded p-1 text-fg-tertiary hover:text-error disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            <X size={14} />
          </button>
        </div>
      ))}

      {urls.length < max && (
        <button
          type="button"
          onClick={addRow}
          className="self-start flex items-center gap-1.5 text-xs text-fg-tertiary hover:text-accent transition-colors pt-1"
        >
          <Plus size={14} />
          Add URL
        </button>
      )}
    </div>
  );
}
