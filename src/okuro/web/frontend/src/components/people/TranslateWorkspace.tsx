/**
 * TranslateWorkspace — the edge detail pane.
 *
 * Paste source on top, hit Translate, see the recipient-tuned rewrite.
 * Mutating call goes through peopleApi.translate → bridge_invoke server-side;
 * every attempt is logged to translation_log which in turn drives the edge
 * stats on the graph.
 */

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Loader2, Wand2, Copy, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { toast } from "@/components/ui/toast";
import { peopleApi, type GraphNode, type TranslateResult } from "@/lib/people-api";

export function TranslateWorkspace({
  person,
  onTranslated,
}: {
  person: GraphNode;
  onTranslated?: (r: TranslateResult) => void;
}) {
  const [source, setSource] = useState("");
  const [context, setContext] = useState("");
  const [result, setResult] = useState<TranslateResult | null>(null);
  const [copied, setCopied] = useState(false);

  const mutation = useMutation({
    mutationFn: () =>
      peopleApi.translate(person.id, source, context.trim() || undefined),
    onSuccess: (data) => {
      setResult(data);
      if (data.success) {
        onTranslated?.(data);
      } else {
        toast.error(data.error || "Translation failed");
      }
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : "Unknown error";
      toast.error(msg);
    },
  });

  const canSubmit = source.trim().length > 0 && !mutation.isPending;

  async function copyResult() {
    if (!result?.translated) return;
    try {
      await navigator.clipboard.writeText(result.translated);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Copy failed");
    }
  }

  return (
    <div className="flex h-full flex-col gap-3">
      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-subtle">
          Translate for
        </div>
        <div className="mt-0.5 text-sm font-semibold text-fg">
          {person.display_name}
          {person.role && (
            <span className="ml-1.5 text-xs font-normal text-fg-subtle">
              · {person.role}
            </span>
          )}
        </div>
      </div>

      <div>
        <label className="text-[10px] font-medium uppercase tracking-wider text-fg-subtle">
          Your message (source)
        </label>
        <Textarea
          value={source}
          onChange={(e) => setSource(e.target.value)}
          placeholder="Paste or type what you'd normally send…"
          rows={6}
          className="mt-1"
        />
      </div>

      <div>
        <label className="text-[10px] font-medium uppercase tracking-wider text-fg-subtle">
          Channel / context (optional)
        </label>
        <Input
          value={context}
          onChange={(e) => setContext(e.target.value)}
          placeholder="e.g. email, Slack DM, all-hands brief"
          className="mt-1"
        />
      </div>

      <div className="flex items-center gap-2">
        <Button
          size="sm"
          disabled={!canSubmit}
          onClick={() => mutation.mutate()}
        >
          {mutation.isPending ? (
            <>
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              Translating…
            </>
          ) : (
            <>
              <Wand2 className="mr-1 h-3.5 w-3.5" />
              Translate
            </>
          )}
        </Button>
        {result?.success && (
          <div className="text-[10px] text-fg-subtle">
            {result.provider}/{result.model} · {result.duration_ms}ms
          </div>
        )}
      </div>

      {result?.translated && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="mb-1 flex items-center justify-between">
            <label className="text-[10px] font-medium uppercase tracking-wider text-fg-subtle">
              Translated
            </label>
            <Button variant="ghost" size="sm" onClick={copyResult}>
              {copied ? (
                <>
                  <Check className="mr-1 h-3 w-3" />
                  Copied
                </>
              ) : (
                <>
                  <Copy className="mr-1 h-3 w-3" />
                  Copy
                </>
              )}
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-auto rounded border border-border bg-surface-subtle p-3 text-sm text-fg whitespace-pre-wrap">
            {result.translated}
          </div>
        </div>
      )}
    </div>
  );
}
