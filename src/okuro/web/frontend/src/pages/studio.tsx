// <!-- AGENT_HEADER
// role: code
// purpose: Studio page — okuro's outcome-based image generation surface. The
//   user picks a STYLE (never a model), types a plain prompt, and gets an image
//   filed in a library. Radical external simplicity (DP02): no ComfyUI, models,
//   workflows, nodes, samplers, or checkpoints are ever shown. Setup + generate
//   run as SSE-progress jobs (Gaps A + C).
//
//   NOT edition-gated, deliberately. The edition axis gates LOCAL INFERENCE, not
//   this page: on air (no GPU) the cloud styles still render through the bridge,
//   and the local styles report setup_needed. Each panel degrades on its own —
//   Text mode shows "No local models installed", local styles ask for setup —
//   so hiding the whole page would remove the one engine an air box CAN use.
//   (A stale comment here used to claim "Nav is edition-gated (advanced/pro)";
//   nav-bar.tsx has never gated it.)
// AGENT_HEADER_END -->
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { cn } from "@/lib/utils";
import { AlertTriangle, Loader2, Sparkles, RefreshCw, Cpu, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Progress } from "@/components/ui/progress";
import { Segmented } from "@/components/ui/segmented";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { toast } from "@/components/ui/toast";
import { PageHeader } from "@/components/shell/page-header";
import { CommercialBadge } from "@/components/models/commercial-badge";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { MediaLightbox } from "@/components/assets/media-lightbox";
import { mediaApi } from "@/lib/assets-api";
import {
  studioApi,
  type StudioPreset,
  type ProgressEvent,
  type GenerateResult,
  type StudioImage,
  type ChatMessage,
} from "@/lib/studio-api";

/** A library tile's image. Resolves through the ASSET STORE by id — the same
 *  route the Assets page uses, and the only one that finds bytes wherever the
 *  bucket put them. Reconstructing `_output_dir()/<filename>` (the old path)
 *  silently 404'd every asset stored outside the generations dir. */
function LibraryImg({ img }: { img: StudioImage }) {
  return (
    <img
      src={mediaApi.fileUrl(img.id)}
      alt={img.title || img.name}
      loading="lazy"
      className="aspect-square w-full rounded-md border border-border-subtle bg-surface-subtle object-contain"
    />
  );
}

/** <img> that resolves the tokenized studio image URL (src can't send a header).
 *  Used for the results of the generation currently on screen — those files were
 *  just written to the output dir. Library tiles use <LibraryImg> instead. */
function StudioImg({ name, alt }: { name: string; alt: string }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    studioApi.imageSrc(name).then((u) => live && setSrc(u));
    return () => {
      live = false;
    };
  }, [name]);
  if (!src) return <div className="aspect-square animate-pulse rounded-md bg-surface-elevated" />;
  return (
    <img
      src={src}
      alt={alt}
      loading="lazy"
      className="aspect-square w-full rounded-md border border-border-subtle object-cover"
    />
  );
}

export function StudioPage() {
  const qc = useQueryClient();
  const [presetId, setPresetId] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [tier, setTier] = useState<string>("balanced");
  const [busy, setBusy] = useState<null | "setup" | "generate">(null);
  const [progress, setProgress] = useState<ProgressEvent | null>(null);
  const [results, setResults] = useState<string[]>([]); // image URLs from this session
  const [modelId, setModelId] = useState<string | null>(null); // explicit model override (feature C)
  const [genResult, setGenResult] = useState<GenerateResult | null>(null); // last result → prompt plan
  const [mode, setMode] = useState<"image" | "text">("image"); // Image gen vs test-a-local-LLM
  const [genJob, setGenJob] = useState<string | null>(null); // running job id → cancel
  // Asset IDs, not filenames: a bucket-owned tile (an illustration) has no file
  // in the generations dir, so a filename reference would 404 for something the
  // library happily shows.
  const [refs, setRefs] = useState<string[]>([]); // library asset ids (cloud only)
  // Text control (cloud only): free → model decides (and invents); exact → only
  // this wording; none → no text at all.
  const [textMode, setTextMode] = useState<"free" | "exact" | "none">("free");
  const [literalText, setLiteralText] = useState("");

  const presetsQ = useQuery({
    queryKey: ["studio", "presets"],
    queryFn: () => studioApi.presets("image"),
  });
  const presets = presetsQ.data?.presets ?? [];
  const preset = useMemo(
    () => presets.find((p) => p.id === presetId) ?? null,
    [presets, presetId],
  );

  const readinessQ = useQuery({
    queryKey: ["studio", "readiness", presetId],
    queryFn: () => studioApi.readiness(presetId!),
    enabled: !!presetId,
  });
  const ready = readinessQ.data?.state === "ready";

  const [libQuery, setLibQuery] = useState("");
  const libraryQ = useQuery({
    queryKey: ["studio", "library", libQuery],
    queryFn: () => studioApi.library(60, libQuery),
  });
  const libImages = useMemo(() => libraryQ.data?.images ?? [], [libraryQ.data]);
  // Which library tile the shared media inspector is open on (null = closed).
  const [libIdx, setLibIdx] = useState<number | null>(null);
  const libKey = ["studio", "library", libQuery];

  // Tag edits + deletes made from the inspector patch the cached list in place,
  // so the open popup's index stays valid instead of jumping on a refetch.
  const patchLibTags = (id: string, tags: string[]) =>
    qc.setQueryData<{ images: StudioImage[] }>(libKey, (prev) =>
      prev ? { images: prev.images.map((i) => (i.id === id ? { ...i, tags } : i)) } : prev,
    );
  const dropLibImage = (id: string) => {
    const next = libImages.filter((i) => i.id !== id);
    qc.setQueryData<{ images: StudioImage[] }>(libKey, { images: next });
    setLibIdx((cur) => (cur === null || cur >= next.length ? null : cur));
  };

  // Default the tier to the preset's default when a style is picked.
  useEffect(() => {
    if (preset) setTier(preset.default_tier);
  }, [preset]);

  // Which engine will actually render — a style can offer both, so this comes
  // from the resolved choice, never from the preset. Editing and the text
  // control are cloud-only capabilities.
  const modelsQ = useQuery({
    queryKey: ["studio", "models", presetId],
    queryFn: () => studioApi.models(presetId!),
    enabled: !!presetId,
  });
  const resolvedEngine = modelsQ.data?.choices.find(
    (c) => c.model_id === (modelId ?? modelsQ.data?.resolved?.model_id),
  )?.engine ?? modelsQ.data?.resolved?.engine;
  const isCloud = resolvedEngine === "bridge";

  // Engine toggle: shown only when this style can actually run BOTH ways here.
  // Without it the cloud renderer is reachable only by opening the model
  // disclosure panel, so on a machine with local models installed it is
  // effectively invisible — the local choice always wins the default.
  const runnable = modelsQ.data?.choices.filter((c) => c.installed) ?? [];
  const canPickEngine =
    runnable.some((c) => c.engine === "comfy") &&
    runnable.some((c) => c.engine === "bridge");

  function pickEngine(engine: string) {
    if (engine === "bridge") {
      setModelId(runnable.find((c) => c.engine === "bridge")?.model_id ?? null);
      return;
    }
    // Back to local: prefer the curated recommendation, else anything local.
    const local =
      runnable.find((c) => c.engine === "comfy" && c.recommended) ??
      runnable.find((c) => c.engine === "comfy");
    setModelId(local?.model_id ?? null);
  }

  function pick(p: StudioPreset) {
    setPresetId(p.id);
    setResults([]);
    setProgress(null);
    setModelId(null); // fall back to the style's default model
    setGenResult(null);
    setRefs([]); // references belong to the style that was open, not the next one
  }

  function toggleRef(id: string) {
    setRefs((cur) =>
      cur.includes(id) ? cur.filter((n) => n !== id) : [...cur, id],
    );
  }

  async function runSetup() {
    if (!preset) return;
    setBusy("setup");
    setProgress({ phase: "engine", message: "Starting setup…" });
    try {
      const { job_id } = await studioApi.startSetup({ preset: preset.id, consent: true });
      await studioApi.streamSetup(job_id, {
        onProgress: (ev) => setProgress(ev),
        onDone: () => {
          setBusy(null);
          setProgress(null);
          toast.success("Ready to generate");
          readinessQ.refetch();
        },
        onError: (m) => {
          setBusy(null);
          setProgress(null);
          toast.error("Setup failed", { description: m });
        },
      });
    } catch (e) {
      setBusy(null);
      setProgress(null);
      toast.error("Setup failed", { description: String(e) });
    }
  }

  async function generate() {
    if (!preset || !prompt.trim()) return;
    setBusy("generate");
    setProgress({ phase: "generate", message: "Starting…" });
    setResults([]);
    setGenResult(null);
    try {
      const { job_id } = await studioApi.startGenerate({
        preset: preset.id,
        prompt: prompt.trim(),
        tier,
        model_id: modelId, // null → okuro auto-picks the style's default
        reference_images: refs.length ? refs : null, // set → edit, not generate
        // "" is meaningful (no text at all), so only "free" sends null.
        literal_text:
          !isCloud || textMode === "free"
            ? null
            : textMode === "none"
              ? ""
              : literalText.trim(),
      });
      setGenJob(job_id);
      const finish = () => {
        setBusy(null);
        setProgress(null);
        setGenJob(null);
      };
      await studioApi.streamGenerate(job_id, {
        onProgress: (ev) => setProgress(ev),
        onDone: (r) => {
          finish();
          setResults(r?.images ?? []);
          setGenResult(r ?? null);
          qc.invalidateQueries({ queryKey: ["studio", "library"] });
        },
        onError: (m) => {
          finish();
          toast.error("Generation failed", { description: m });
        },
        onCancelled: () => {
          finish();
          toast("Generation cancelled");
        },
      });
    } catch (e) {
      setBusy(null);
      setProgress(null);
      setGenJob(null);
      toast.error("Generation failed", { description: String(e) });
    }
  }

  async function cancelGenerate() {
    if (genJob) await studioApi.cancelGenerate(genJob).catch(() => {});
  }

  const tierOptions = (preset?.tiers ?? ["fast", "balanced", "high"]).map((t) => ({
    value: t,
    label: t,
  }));

  return (
    <div className="page-shell space-y-10">
      <PageHeader
        title="Studio"
        subtitle={
          mode === "text"
            ? "Prompt an installed local model and read its reply."
            : "Pick a style, describe it, and okuro makes the image."
        }
        right={
          <Button
            variant="outline"
            size="sm"
            onClick={() => libraryQ.refetch()}
            disabled={libraryQ.isFetching}
          >
            <RefreshCw className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
            Refresh
          </Button>
        }
      />

      <Segmented
        ariaLabel="Studio mode"
        value={mode}
        onChange={(v) => setMode(v as "image" | "text")}
        options={[
          { label: "Image", value: "image" },
          { label: "Text", value: "text" },
        ]}
      />

      {mode === "text" ? (
        <TextPanel />
      ) : (
      <>
      {/* Style picker */}
      <section className="space-y-3">
        <SectionLabel>Style</SectionLabel>
        {presetsQ.isLoading ? (
          <div className="h-24 animate-pulse rounded-md border border-border-subtle bg-surface-elevated" />
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            {presets.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => pick(p)}
                aria-pressed={presetId === p.id}
                className={
                  "flex flex-col items-start gap-1 rounded-lg border p-4 text-left transition-colors " +
                  (presetId === p.id
                    ? "border-accent bg-accent-subtle"
                    : "border-border-subtle bg-surface-subtle hover:border-border")
                }
              >
                <span className="text-2xl" aria-hidden="true">
                  {p.icon}
                </span>
                <span className="text-sm font-medium text-fg">{p.label}</span>
                <span className="text-xs text-fg-subtle">{p.description}</span>
              </button>
            ))}
          </div>
        )}
      </section>

      {/* Compose / setup */}
      {preset && (
        <section className="space-y-4">
          <SectionLabel>Create</SectionLabel>

          {readinessQ.isLoading ? (
            <div className="h-32 animate-pulse rounded-md border border-border-subtle bg-surface-elevated" />
          ) : !ready ? (
            readinessQ.data?.self_service === false ? (
              /* Cloud style: okuro cannot fix this — the user installs and signs
                 in to their own CLI. Offering a "set up" button here would be a
                 lie, so this branch only tells them what to do and re-checks. */
              <div className="space-y-3 rounded-lg border border-border-subtle bg-surface-subtle p-6">
                <p className="text-sm text-fg">{readinessQ.data.message}</p>
                <p className="text-xs text-fg-subtle">
                  The {preset.label} style renders in the cloud on your own CLI
                  subscription — no GPU and no download, but okuro can&apos;t sign in
                  for you.
                </p>
                <Button
                  variant="outline"
                  onClick={() => readinessQ.refetch()}
                  disabled={readinessQ.isFetching}
                >
                  <RefreshCw
                    className={cn("mr-1 h-4 w-4", readinessQ.isFetching && "animate-spin")}
                    aria-hidden="true"
                  />
                  Check again
                </Button>
              </div>
            ) : (
              <div className="space-y-3 rounded-lg border border-border-subtle bg-surface-subtle p-6">
                <p className="text-sm text-fg">
                  {readinessQ.data?.message ?? "Set up image generation"} to use the{" "}
                  {preset.label} style.
                </p>
                <p className="text-xs text-fg-subtle">
                  okuro will install the generation engine and download a model — this
                  runs once and can take a few minutes.
                </p>
                {busy === "setup" && progress ? (
                  <ProgressBar ev={progress} />
                ) : (
                  <Button onClick={runSetup} disabled={busy !== null}>
                    <Sparkles className="mr-1 h-4 w-4" aria-hidden="true" />
                    Set up local generation
                  </Button>
                )}
              </div>
            )
          ) : (
            <div className="space-y-4">
              <Textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder={
                  refs.length
                    ? "Describe the change — e.g. “make the teapot deep blue”…"
                    : `Describe your ${preset.label.toLowerCase()} image…`
                }
                rows={3}
                disabled={busy === "generate"}
              />

              {isCloud && canPickEngine && (
                // Only when they turned OFF a working local path: the cloud is a
                // shared daily allowance, and spending it while a free GPU sits
                // idle should be a visible decision, not a silent one.
                <p className="text-xs text-fg-subtle">
                  Cloud rendering uses a shared daily allowance (~13 images) and
                  takes about a minute. Your GPU is free and unlimited.
                </p>
              )}

              {isCloud && (
                <>
                  <TextControl
                    mode={textMode}
                    value={literalText}
                    onMode={setTextMode}
                    onValue={setLiteralText}
                    disabled={busy === "generate"}
                  />
                  <ReferencePicker
                    library={libImages}
                    selected={refs}
                    onToggle={toggleRef}
                    disabled={busy === "generate"}
                  />
                  {textMode === "free" && (
                    <p className="flex items-start gap-2 text-xs text-fg-subtle">
                      <AlertTriangle
                        className="mt-0.5 h-3.5 w-3.5 shrink-0"
                        aria-hidden="true"
                      />
                      <span>
                        Left free, this model invents text — headings, dates, even
                        real-looking names and credits. Pin the wording above if the
                        image is going anywhere real.
                      </span>
                    </p>
                  )}
                </>
              )}
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-4">
                  <div className="flex items-center gap-2">
                    <span className="text-xs uppercase tracking-wide text-fg-subtle">
                      Quality
                    </span>
                    <Segmented options={tierOptions} value={tier} onChange={setTier} />
                  </div>
                  {canPickEngine && (
                    <div className="flex items-center gap-2">
                      <span className="text-xs uppercase tracking-wide text-fg-subtle">
                        Render
                      </span>
                      <Segmented
                        ariaLabel="Where to render this image"
                        value={resolvedEngine ?? "comfy"}
                        onChange={pickEngine}
                        options={[
                          { label: "On my GPU", value: "comfy" },
                          { label: "Cloud", value: "bridge" },
                        ]}
                      />
                    </div>
                  )}
                </div>
                <Button onClick={generate} disabled={busy !== null || !prompt.trim()}>
                  {busy === "generate" ? (
                    <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden="true" />
                  ) : (
                    <Sparkles className="mr-1 h-4 w-4" aria-hidden="true" />
                  )}
                  Generate
                </Button>
              </div>
              {busy === "generate" && progress && (
                <div className="flex items-center gap-3">
                  <div className="flex-1">
                    <ProgressBar ev={progress} />
                  </div>
                  <Button variant="outline" size="sm" onClick={cancelGenerate}>
                    Cancel
                  </Button>
                </div>
              )}

              {/* Opt-in transparency: which model runs + how the prompt is shaped */}
              <ModelAndPrompt
                presetId={preset.id}
                modelId={modelId}
                onSelect={setModelId}
                result={genResult}
              />
            </div>
          )}

          {/* This-session results */}
          {results.length > 0 && (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
              {results.map((url) => (
                <StudioImg key={url} name={url.split("/").pop()!} alt={prompt} />
              ))}
            </div>
          )}
        </section>
      )}

      {/* Library */}
      <section className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <SectionLabel>Library</SectionLabel>
          <input
            type="search"
            value={libQuery}
            onChange={(e) => setLibQuery(e.target.value)}
            placeholder="Search title…"
            aria-label="Search the media library by title"
            className="w-48 rounded-md border border-border-subtle bg-surface-elevated px-3 py-1.5 text-sm text-fg placeholder:text-fg-subtle focus:outline-none focus:ring-1 focus:ring-border"
          />
        </div>
        {libraryQ.isLoading ? (
          <div className="h-40 animate-pulse rounded-md border border-border-subtle bg-surface-elevated" />
        ) : libImages.length === 0 ? (
          <EmptyState
            title={libQuery ? "No matches" : "No images yet"}
            description={
              libQuery
                ? "Nothing matches that search — try another term."
                : "Your generated images will appear here."
            }
          />
        ) : (
          <div className="grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-6">
            {libImages.map((img, i) => (
              <button
                key={img.id}
                type="button"
                onClick={() => setLibIdx(i)}
                title={img.title || img.name}
                aria-label={`Open details for ${img.title || img.name}`}
                className="overflow-hidden rounded-md transition-opacity hover:opacity-80"
              >
                <LibraryImg img={img} />
              </button>
            ))}
          </div>
        )}
      </section>

      {/* The SAME detail popup the Assets page opens — one inspector, one bucket. */}
      {libIdx !== null && libImages[libIdx] && (
        <MediaLightbox
          items={libImages}
          index={libIdx}
          onClose={() => setLibIdx(null)}
          onIndex={setLibIdx}
          onPatchTags={patchLibTags}
          onDelete={dropLibImage}
        />
      )}
      </>
      )}
    </div>
  );
}

/** Control what text the image may render.
 *
 * Left free, the cloud model invents captions, dates, venues and real-looking
 * credits. Pinning the wording is the only thing measured to stop it — and the
 * constraint has to name the allowed string, so it needs a field, not a toggle. */
function TextControl({
  mode,
  value,
  onMode,
  onValue,
  disabled,
}: {
  mode: "free" | "exact" | "none";
  value: string;
  onMode: (m: "free" | "exact" | "none") => void;
  onValue: (v: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs uppercase tracking-wide text-fg-subtle">Text</span>
        <Segmented
          ariaLabel="What text may appear in the image"
          value={mode}
          onChange={(v) => onMode(v as "free" | "exact" | "none")}
          options={[
            { label: "Model decides", value: "free" },
            { label: "Exact wording", value: "exact" },
            { label: "No text", value: "none" },
          ]}
        />
      </div>
      {mode === "exact" && (
        <input
          type="text"
          value={value}
          onChange={(e) => onValue(e.target.value)}
          disabled={disabled}
          placeholder="The only words allowed in the image — e.g. STUDIO"
          aria-label="Exact text allowed in the image"
          className="w-full rounded-md border border-border-subtle bg-surface-elevated px-3 py-2 text-sm text-fg placeholder:text-fg-subtle focus:outline-none focus:ring-1 focus:ring-border"
        />
      )}
    </div>
  );
}

/** Pick library images to edit from — selecting any turns generate into edit.
 *
 * Sends library NAMES, never paths: the server resolves them inside its own
 * generations dir, so the browser can't point the cloud provider at a file it
 * shouldn't read. */
function ReferencePicker({
  library,
  selected,
  onToggle,
  disabled,
}: {
  library: StudioImage[];
  selected: string[];
  onToggle: (id: string) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (library.length === 0) return null;

  return (
    <div className="space-y-2">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="text-xs uppercase tracking-wide text-fg-subtle hover:text-fg"
      >
        Edit an existing image{selected.length > 0 ? ` (${selected.length})` : ""}
      </button>

      {selected.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {selected.map((id) => {
            const img = library.find((i) => i.id === id);
            const label = img?.title || img?.name || id;
            return (
              <button
                key={id}
                type="button"
                onClick={() => onToggle(id)}
                disabled={disabled}
                className="flex items-center gap-1 rounded-full border border-accent bg-accent-subtle px-2 py-1 text-xs text-fg"
              >
                <span className="max-w-[24rem] truncate">{label}</span>
                <X className="h-3 w-3" aria-hidden="true" />
                <span className="sr-only">Remove {label} as a reference</span>
              </button>
            );
          })}
        </div>
      )}

      {open && (
        <div className="grid max-h-56 grid-cols-4 gap-2 overflow-y-auto rounded-md border border-border-subtle bg-surface-subtle p-2 sm:grid-cols-6 lg:grid-cols-8">
          {library.map((img) => {
            const on = selected.includes(img.id);
            return (
              <button
                key={img.id}
                type="button"
                onClick={() => onToggle(img.id)}
                disabled={disabled}
                aria-pressed={on}
                title={img.title || img.name}
                className={cn(
                  "overflow-hidden rounded border-2 transition-colors",
                  on ? "border-accent" : "border-transparent hover:border-border",
                )}
              >
                <LibraryImg img={img} />
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ProgressBar({ ev }: { ev: ProgressEvent }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-xs text-fg-subtle">
        <span>{ev.message}</span>
        {typeof ev.pct === "number" && <span>{ev.pct}%</span>}
      </div>
      <Progress value={ev.pct ?? undefined} />
    </div>
  );
}

/**
 * Opt-in "Model & prompt" disclosure (feature C). Collapsed by default so the
 * outcome-first flow stays uncluttered; expanding reveals which model this style
 * will run, a switch to any other installed model, curated cross-family
 * suggestions, and — after a generation — how the prompt was shaped for the model.
 */
function ModelAndPrompt({
  presetId,
  modelId,
  onSelect,
  result,
}: {
  presetId: string;
  modelId: string | null;
  onSelect: (id: string | null) => void;
  result: GenerateResult | null;
}) {
  const modelsQ = useQuery({
    queryKey: ["studio", "models", presetId],
    queryFn: () => studioApi.models(presetId),
  });
  const data = modelsQ.data;
  const choices = data?.choices ?? [];
  const installed = choices.filter((c) => c.installed);
  const suggestions = choices.filter((c) => !c.installed);
  const activeId = modelId ?? data?.resolved?.model_id ?? null;
  const active = installed.find((c) => c.model_id === activeId) ?? null;
  const plan = result?.prompt_plan;
  const summary = data?.resolved ? (active?.label ?? data.resolved.label) : "…";

  return (
    <details className="rounded-lg border border-border-subtle bg-surface-subtle">
      <summary className="flex cursor-pointer items-center justify-between gap-2 px-4 py-3 text-sm">
        <span className="flex items-center gap-2 text-fg">
          <Cpu className="h-4 w-4 text-fg-subtle" aria-hidden="true" />
          Model &amp; prompt
        </span>
        <span className="truncate text-xs text-fg-subtle">{summary}</span>
      </summary>

      <div className="space-y-4 border-t border-border-subtle px-4 py-4">
        {/* Which model runs — switchable across installed models */}
        <div className="space-y-2">
          <span className="text-xs uppercase tracking-wide text-fg-subtle">Model</span>
          {installed.length === 0 ? (
            <p className="text-xs text-fg-subtle">
              No model installed yet — set one up above.
            </p>
          ) : (
            <select
              value={activeId ?? ""}
              onChange={(e) => onSelect(e.target.value || null)}
              className="w-full rounded-md border border-border-subtle bg-surface px-3 py-2 text-sm text-fg"
            >
              {installed.map((c) => (
                <option key={c.model_id!} value={c.model_id!}>
                  {c.label}
                  {c.recommended ? " — recommended" : ""}
                </option>
              ))}
            </select>
          )}
          {active?.rationale && <p className="text-xs text-fg-subtle">{active.rationale}</p>}
        </div>

        {/* Curated models not yet installed — the per-style suggestions */}
        {suggestions.length > 0 && (
          <div className="space-y-1.5">
            <span className="text-xs uppercase tracking-wide text-fg-subtle">
              Other models for this style
            </span>
            <ul className="space-y-1.5">
              {suggestions.map((c) => (
                <li key={c.label} className="flex items-baseline gap-2 text-xs">
                  <span className="font-medium text-fg">{c.label}</span>
                  <span className="flex-1 text-fg-subtle">{c.rationale}</span>
                  <span className="shrink-0 rounded bg-surface px-1.5 py-0.5 text-[10px] text-fg-subtle">
                    not installed
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* How the prompt was optimized for the model that ran */}
        {plan && (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-xs uppercase tracking-wide text-fg-subtle">
                Optimized prompt
              </span>
              {plan.syntax && (
                <span className="rounded bg-accent-subtle px-1.5 py-0.5 text-[10px] text-fg">
                  {plan.syntax}
                </span>
              )}
            </div>
            <p className="rounded-md border border-border-subtle bg-surface px-3 py-2 text-xs text-fg">
              {plan.prompt}
            </p>
            {plan.negative && (
              <p className="text-xs text-fg-subtle">
                <span className="uppercase tracking-wide">Negative:</span> {plan.negative}
              </p>
            )}
            {plan.notes.length > 0 && (
              <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-fg-subtle">
                {plan.notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </details>
  );
}

/** Studio Text mode — prompt an installed local LLM and read its reply. */
function shortModelName(id: string): string {
  return id.replace(/^text\./, "").replace(/\.gguf$/i, "");
}

function fmtUptime(s: number | null): string {
  if (!s || s < 0) return "";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function TextPanel() {
  const qc = useQueryClient();
  const [bundleId, setBundleId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  const modelsQ = useQuery({
    queryKey: ["studio", "text-models"],
    queryFn: () => studioApi.textModels(),
    refetchInterval: busy ? false : 8000,
  });
  const enginesQ = useQuery({
    queryKey: ["studio", "text-engines"],
    queryFn: () => studioApi.textEngines(),
    refetchInterval: 3000,
  });
  const models = modelsQ.data?.models ?? [];
  const engines = enginesQ.data?.engines ?? [];
  const loaded = new Set(engines.map((e) => e.model_id));
  const selected = models.find((m) => m.bundle_id === bundleId);

  useEffect(() => {
    if (!bundleId && models.length) setBundleId(models[0]!.bundle_id);
  }, [models, bundleId]);

  // Keep the newest message in view as tokens stream in.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  async function send() {
    const text = input.trim();
    if (!bundleId || !text || busy) return;
    setError(null);
    setInput("");
    // Append the user turn + an empty assistant turn we stream tokens into.
    const history: ChatMessage[] = [...messages, { role: "user", content: text }];
    setMessages([...history, { role: "assistant", content: "" }]);
    setBusy(true);
    setStatus(loaded.has(bundleId) ? "Thinking…" : "Loading model onto GPU…");

    const appendToken = (delta: string) =>
      setMessages((prev) => {
        const copy = prev.slice();
        const last = copy[copy.length - 1];
        if (last && last.role === "assistant") {
          copy[copy.length - 1] = { ...last, content: last.content + delta };
        }
        return copy;
      });

    try {
      const { job_id } = await studioApi.startTextChat({ bundle_id: bundleId, messages: history });
      await studioApi.streamText(job_id, {
        onProgress: (ev) => {
          if (ev.phase === "token") {
            setStatus(null);
            appendToken(ev.message);
          } else {
            setStatus(ev.message || ev.phase);
          }
        },
        onDone: () => {
          setBusy(false);
          setStatus(null);
          qc.invalidateQueries({ queryKey: ["studio", "text-engines"] });
          qc.invalidateQueries({ queryKey: ["studio", "text-models"] });
        },
        onError: (msg) => {
          setBusy(false);
          setStatus(null);
          setError(msg);
        },
      });
    } catch (e) {
      setBusy(false);
      setStatus(null);
      setError(String(e));
    }
  }

  if (modelsQ.isLoading) {
    return <div className="h-24 animate-pulse rounded-md border border-border-subtle bg-surface-elevated" />;
  }
  if (models.length === 0) {
    return (
      <EmptyState
        title="No local models installed"
        description="Pull an LLM from the Models page, then chat with it here."
      />
    );
  }

  return (
    <div className="space-y-4">
      {/* Controls: which model, what's loaded, unload. */}
      <div className="flex flex-wrap items-center gap-3">
        <select
          value={bundleId ?? ""}
          onChange={(e) => setBundleId(e.target.value)}
          disabled={busy}
          className="rounded-md border border-border-subtle bg-surface-subtle px-3 py-1.5 text-sm text-fg"
        >
          {models.map((m) => (
            <option key={m.bundle_id} value={m.bundle_id}>
              {loaded.has(m.bundle_id) ? "● " : "○ "}
              {m.display_name}
              {m.parameters ? ` · ${m.parameters}` : ""}
            </option>
          ))}
        </select>
        {selected && (
          <CommercialBadge
            status={selected.commercial_status}
            licenseId={selected.license_id}
          />
        )}
        {engines.map((e) => (
          <span
            key={e.model_id}
            className="flex items-center gap-2 rounded-md border border-border-subtle bg-surface-subtle px-2.5 py-1 text-xs text-fg-muted"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-success" aria-hidden="true" />
            <span className="text-fg">{shortModelName(e.model_id)}</span>
            <span className="text-3xs text-fg-subtle">
              GPU{e.gpu_index}
              {e.uptime_s != null ? ` · ${fmtUptime(e.uptime_s)}` : ""}
            </span>
            <button
              type="button"
              onClick={async () => {
                await studioApi.textUnload(e.model_id);
                qc.invalidateQueries({ queryKey: ["studio", "text-engines"] });
                qc.invalidateQueries({ queryKey: ["studio", "text-models"] });
              }}
              className="inline-flex items-center gap-1 rounded border border-border-subtle px-1.5 py-0.5 text-3xs text-fg-subtle hover:border-error/40 hover:text-error"
              aria-label={`Unload ${shortModelName(e.model_id)}`}
              title="Unload — free this GPU VRAM"
            >
              <X className="h-3 w-3" aria-hidden="true" />
              Unload
            </button>
          </span>
        ))}
        {messages.length > 0 && (
          <Button variant="ghost" size="sm" onClick={() => setMessages([])} disabled={busy}>
            Clear
          </Button>
        )}
      </div>

      {selected &&
        selected.commercial_status &&
        selected.commercial_status !== "commercial" && (
          <p className="flex items-start gap-1.5 text-3xs text-fg-subtle">
            <AlertTriangle size={12} className="mt-0.5 shrink-0" />
            <span>
              {selected.commercial_status === "non_commercial"
                ? "Non-commercial licence — fine to test, but its output isn't licensed for a commercial product."
                : selected.commercial_status === "conditional"
                  ? "Community licence — commercial use only under a revenue cap; verify before shipping."
                  : "Licence not recognised — verify terms before using output commercially."}
              {selected.license_id ? ` (${selected.license_id})` : ""}
            </span>
          </p>
        )}

      {/* Conversation */}
      <div className="min-h-[32rem] space-y-3 rounded-md border border-border-subtle bg-surface-subtle/40 p-4">
        {messages.length === 0 ? (
          <p className="py-8 text-center text-sm text-fg-subtle">
            Chat with a local model — messages stream token by token.
            {!loaded.has(bundleId ?? "") ? " First message loads it onto the GPU (~20–40s)." : ""}
          </p>
        ) : (
          messages.map((m, i) => {
            const streaming =
              busy && i === messages.length - 1 && m.role === "assistant";
            return (
              <div
                key={i}
                className={cn(
                  "flex",
                  m.role === "user" ? "justify-end" : "justify-start",
                )}
              >
                <div
                  className={cn(
                    "max-w-[80%] select-text rounded-lg px-3 py-2 text-sm",
                    m.role === "user"
                      ? "whitespace-pre-wrap bg-accent-subtle text-fg"
                      : "border border-border-subtle bg-surface-elevated text-fg",
                  )}
                >
                  {m.role === "user" ? (
                    m.content
                  ) : streaming ? (
                    // Plain text while streaming (fast); formatted once complete.
                    <span className="whitespace-pre-wrap">
                      {m.content}
                      <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-fg-subtle align-middle" />
                    </span>
                  ) : m.content ? (
                    <MarkdownContent variant="compact">{m.content}</MarkdownContent>
                  ) : null}
                </div>
              </div>
            );
          })
        )}
        {status && (
          <div className="flex items-center gap-1.5 text-xs text-fg-subtle">
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            {status}
          </div>
        )}
        <div ref={endRef} />
      </div>

      {error && (
        <div className="rounded-md border border-error/40 bg-error/10 px-3 py-2 text-sm text-error">
          {error}
        </div>
      )}

      {/* Composer */}
      <div className="flex items-end gap-2">
        <Textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Message the model… (Enter to send, Shift+Enter for newline)"
          rows={2}
          disabled={busy}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <Button onClick={send} disabled={busy || !input.trim() || !bundleId}>
          {busy ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <Sparkles className="h-4 w-4" aria-hidden="true" />
          )}
        </Button>
      </div>
    </div>
  );
}
