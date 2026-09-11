import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { toast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import { redlineApi, type RedlineComment, type RedlineListing } from "@/lib/redline-api";
import type { Anchor } from "@/redline/anchor";
import { RedlineDeckStage } from "@/redline/deck-stage";
import type { OverlayHandle } from "@/redline/overlay-core";
import {
  messageFromFrame,
  type FrameToParent,
  type ParentToFrame,
  type RenderComment,
} from "@/redline/protocol";

/**
 * /redline/:document_id — document left, comments right.
 *
 * The split is not decoration; it is the security boundary. An html document
 * is served into an iframe with `sandbox="allow-scripts"` and NO
 * `allow-same-origin`, under REDLINE_CSP, behind a per-document capability
 * token in its own URL. That frame therefore has an OPAQUE origin, which has
 * two consequences this file is built around:
 *
 *   1. `event.origin` is the string `"null"`. The only accepted validation is
 *      `event.source === iframeRef.current?.contentWindow`.
 *   2. The frame cannot fetch (`connect-src 'none'`) and never sees the global
 *      bearer. THIS page performs every write.
 *
 * A PRISM DECK takes the other path. A DeckDoc is not html and only the SPA's
 * own deck runtime renders one, so it mounts in this page's tree and the
 * overlay attaches to the shadow root that runtime creates — same mechanism,
 * same five tiers, a shadow root instead of a document. Both paths speak the
 * same message shapes, so everything below the stage is one implementation.
 *
 * WHICH VERSION IS SHOWN. The switcher loads a version's own comment list, and
 * the stage shows that version's own BYTES whenever they still exist —
 * true for every artifact and prism version, because okuro wrote them into the
 * version's own directory. A past FILE version kept its hash and not its
 * content, so it renders nothing and draws no bubbles: painting its anchors
 * onto today's bytes would be exactly the cross-version re-anchoring measured
 * to survive 0 of 5 times. Nothing is ever re-anchored across versions in
 * either case.
 */

type Filter = "open" | "done" | "all";

function post(frame: HTMLIFrameElement | null, message: ParentToFrame): void {
  // targetOrigin "*" because the frame's origin is the string "null" and no
  // other value can ever match it. Nothing secret travels this way.
  frame?.contentWindow?.postMessage(message, "*");
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "border px-2 py-0.5 text-xs",
        active
          ? "border-fg bg-surface-elevated text-fg"
          : "border-border text-tertiary hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}

export function RedlinePage() {
  const { document_id: documentId = "" } = useParams<{ document_id: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  // The deck path has no frame to postMessage into — the overlay is right
  // here, so the panel calls it.
  const deckOverlayRef = useRef<OverlayHandle | null>(null);

  const [addMode, setAddMode] = useState(false);
  const [filter, setFilter] = useState<Filter>("open");
  const [pending, setPending] = useState<Anchor | null>(null);
  const [draft, setDraft] = useState("");
  const [highlight, setHighlight] = useState<string | null>(null);
  const [resolving, setResolving] = useState<string | null>(null);
  const [note, setNote] = useState("");

  const seqParam = searchParams.get("v");

  const docsQuery = useQuery({
    queryKey: ["redline", "documents"],
    queryFn: () => redlineApi.documents(),
  });
  const doc = docsQuery.data?.documents.find((d) => d.id === documentId);

  // Opening is what mints the serve token. It is also the ONLY recovery from a
  // 401 in the frame, because the token store is in-process and dies with the
  // API — so this runs on every mount rather than being cached across one.
  const openQuery = useQuery({
    queryKey: ["redline", "open", documentId],
    enabled: Boolean(doc),
    gcTime: 0,
    queryFn: () => redlineApi.open(doc!.kind, doc!.ref),
  });

  const versionsQuery = useQuery({
    queryKey: ["redline", "versions", documentId],
    enabled: Boolean(documentId),
    queryFn: () => redlineApi.versions(documentId),
  });

  const versions = versionsQuery.data?.versions ?? [];
  const currentSeq = openQuery.data?.version_seq ?? null;
  const selectedSeq = seqParam ? Number(seqParam) : currentSeq;
  const selected = versions.find((v) => v.seq === selectedSeq) ?? null;
  const isCurrent = selectedSeq !== null && selectedSeq === currentSeq;

  // A past version needs its OWN token: the credential is scoped to one
  // (document, seq) pair, and its bytes live in that version's own directory.
  // Only asked for when the version says it still has bytes, so a hash-only
  // file version never mints a token nobody can use.
  const pastQuery = useQuery({
    queryKey: ["redline", "open", documentId, selectedSeq],
    enabled: Boolean(doc) && !isCurrent && Boolean(selected?.renderable),
    gcTime: 0,
    queryFn: () => redlineApi.open(doc!.kind, doc!.ref, selectedSeq),
  });

  const shown = isCurrent ? openQuery.data : pastQuery.data;

  const listQuery = useQuery<RedlineListing>({
    queryKey: ["redline", "comments", documentId, selected?.id ?? null, filter],
    enabled: Boolean(documentId) && Boolean(selected?.id ?? openQuery.data?.version_id),
    queryFn: () =>
      redlineApi.comments(
        documentId,
        selected?.id ?? openQuery.data?.version_id ?? null,
        filter,
      ),
  });

  const comments = useMemo(() => listQuery.data?.comments ?? [], [listQuery.data]);
  const counts = listQuery.data?.counts ?? { open: 0, done: 0 };

  const refreshComments = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["redline", "comments", documentId] });
    void queryClient.invalidateQueries({ queryKey: ["redline", "versions", documentId] });
  }, [queryClient, documentId]);

  // ── frame ↔ panel ──────────────────────────────────────────────────

  // Posted unconditionally, and again on every `redline:ready`.
  //
  // Gating this on a "the frame is ready" flag was wrong in a way that looked
  // right: the overlay tag sits before </body>, so it runs and announces
  // itself BEFORE the iframe's own load event. A flag reset in onLoad
  // therefore cleared readiness after the only announcement, and no comment
  // list ever reached the frame again. Posting into a not-yet-listening frame
  // is harmless; missing the one announcement is not.
  // Bubbles are drawn when the stage is showing the SAME version the panel is
  // listing — nothing else. That is one comparison rather than "is it the
  // current one", because for an artifact and a deck a PAST version is showing
  // its own bytes and its own anchors resolve exactly against them.
  const showsListedVersion = Boolean(
    shown?.renderable && selected?.id && shown?.version_id === selected.id,
  );

  const payload: RenderComment[] = useMemo(
    () =>
      showsListedVersion
        ? comments.map((c) => ({
            commentId: c.id,
            seq: c.seq,
            anchor: c.anchor ?? null,
            status: c.status,
          }))
        : [],
    [comments, showsListedVersion],
  );

  const pushComments = useCallback(() => {
    post(iframeRef.current, { type: "redline:render", comments: payload });
  }, [payload]);

  // One handler for both stages. The frame reaches it through postMessage; the
  // deck overlay reaches it by calling it, because it runs in this very page.
  const handleMessage = useCallback(
    (data: FrameToParent) => {
      switch (data.type) {
        case "redline:ready":
          pushComments();
          break;
        case "redline:anchor":
          setPending(data.anchor);
          setAddMode(false);
          break;
        case "redline:bubbleClick":
          setHighlight(data.commentId);
          document
            .getElementById(`redline-item-${data.commentId}`)
            ?.scrollIntoView({ block: "center", behavior: "smooth" });
          break;
        case "redline:mode":
          setAddMode(Boolean(data.add));
          break;
        case "redline:key":
          if (data.key === "Escape") setAddMode(false);
          if (data.key === "n") nextOpen();
          break;
        default:
          break;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pushComments, comments],
  );

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      // The ONLY accepted validation. `event.origin` is the string "null".
      const data = messageFromFrame(event, iframeRef.current);
      if (!data) return;
      handleMessage(data);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [handleMessage]);

  useEffect(() => {
    pushComments();
  }, [pushComments]);

  useEffect(() => {
    post(iframeRef.current, { type: "redline:mode", add: addMode });
  }, [addMode]);

  useEffect(() => {
    if (!highlight) return;
    const timer = window.setTimeout(() => setHighlight(null), 600);
    return () => window.clearTimeout(timer);
  }, [highlight]);

  const focusComment = useCallback((comment: RedlineComment) => {
    setHighlight(comment.id);
    post(iframeRef.current, { type: "redline:scrollTo", commentId: comment.id });
    deckOverlayRef.current?.scrollToComment(comment.id);
  }, []);

  const nextOpen = useCallback(() => {
    const open = comments.filter((c) => c.status === "open");
    if (!open.length) return;
    const index = open.findIndex((c) => c.id === highlight);
    const target = open[(index + 1) % open.length] ?? open[0];
    if (target) {
      focusComment(target);
      document
        .getElementById(`redline-item-${target.id}`)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [comments, highlight, focusComment]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement | null)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (event.key === "Escape") {
        if (pending) setPending(null);
        else setAddMode(false);
      } else if (event.key === "c" || event.key === "C") {
        setAddMode((v) => !v);
      } else if (event.key === "n" || event.key === "N") {
        nextOpen();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pending, nextOpen]);

  // ── writes ─────────────────────────────────────────────────────────

  async function saveComment() {
    // The version being LOOKED AT, which for an artifact or a deck may be a
    // past one showing its own bytes. A comment belongs to the version whose
    // element it was placed on, and that is this one.
    const versionId = shown?.version_id ?? openQuery.data?.version_id;
    if (!versionId || !pending || !draft.trim()) return;
    try {
      await redlineApi.create(versionId, draft.trim(), pending);
      setPending(null);
      setDraft("");
      refreshComments();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "could not save the comment");
    }
  }

  async function markDone(commentId: string) {
    if (!note.trim()) {
      toast.error("marking done requires a note — the schema refuses an empty one");
      return;
    }
    try {
      const res = await redlineApi.resolve(commentId, note.trim());
      if (res.warning) toast.message(res.warning);
      setResolving(null);
      setNote("");
      refreshComments();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "could not resolve");
    }
  }

  async function reopen(commentId: string) {
    try {
      await redlineApi.reopen(commentId);
      refreshComments();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "could not reopen");
    }
  }

  // ── render ─────────────────────────────────────────────────────────

  if (docsQuery.isLoading) return <div className="p-4 text-sm text-tertiary">Loading…</div>;
  if (!documentId) {
    const docs = docsQuery.data?.documents ?? [];
    if (!docs.length) {
      return (
        <EmptyState
          title="nothing opened yet"
          description="redline_open(kind='file', ref='<absolute path under redline.roots>')"
        />
      );
    }
    return (
      <div className="flex flex-col">
        {docs.map((d) => (
          <a
            key={d.id}
            href={`/redline/${d.id}`}
            className="border-b border-border-subtle px-3 py-2 text-sm text-fg hover:bg-surface-elevated"
          >
            <div>{d.title}</div>
            <div className="truncate text-xs text-tertiary">
              {d.kind} · {d.ref} · v{d.version_seq ?? "?"}
            </div>
          </a>
        ))}
      </div>
    );
  }
  if (!doc) {
    return (
      <EmptyState
        title="unknown document"
        description="Open one with redline_open(kind='file', ref='<absolute path>')."
      />
    );
  }

  const serveUrl = shown?.url ?? null;
  const deckUrl = shown?.deck_url ?? null;
  const stageNote = shown?.note ?? (selected && !selected.renderable ? "" : null);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-3 border-b border-border px-3 py-2">
        <Button
          size="xs"
          variant={addMode ? "default" : "outline"}
          onClick={() => setAddMode((v) => !v)}
        >
          {addMode ? "picking — Esc to stop" : "C  add"}
        </Button>
        <select
          className="border border-border bg-surface px-2 py-1 text-xs text-fg"
          value={selectedSeq ?? ""}
          onChange={(e) => setSearchParams({ v: e.target.value })}
        >
          {versions.map((v) => (
            <option key={v.id} value={v.seq}>
              v{v.seq} · {v.captured_at?.slice(0, 10)} ·{" "}
              {v.is_current
                ? "current"
                : v.renderable
                  ? "past · readable"
                  : "past · hash only"}
            </option>
          ))}
        </select>
        <div className="flex gap-1">
          <Chip active={filter === "open"} onClick={() => setFilter("open")}>
            Open ({counts.open})
          </Chip>
          <Chip active={filter === "done"} onClick={() => setFilter("done")}>
            Done ({counts.done})
          </Chip>
          <Chip active={filter === "all"} onClick={() => setFilter("all")}>
            All
          </Chip>
        </div>
        <div className="ml-auto truncate text-xs text-tertiary">
          redline · {doc.title}
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1 bg-surface-subtle">
          {deckUrl ? (
            // A deck is not html: it renders in THIS tree, and the overlay
            // attaches to the shadow root the deck runtime creates.
            <RedlineDeckStage
              deckUrl={deckUrl}
              versionSeq={shown?.version_seq ?? 0}
              comments={payload}
              addMode={addMode}
              onMessage={handleMessage}
              onOverlay={(overlay) => {
                deckOverlayRef.current = overlay;
              }}
            />
          ) : serveUrl ? (
            <iframe
              ref={iframeRef}
              title={doc.title}
              src={serveUrl}
              // No allow-same-origin. The frame must not reach this page's
              // origin, its storage, or its bearer.
              sandbox="allow-scripts"
              className="h-full w-full border-0 bg-white"
            />
          ) : (
            <div className="p-4 text-sm text-tertiary">
              {stageNote
                ? stageNote
                : openQuery.isError
                  ? "could not open the document — is its source still where it was?"
                  : "opening…"}
            </div>
          )}
        </div>

        <div
          ref={panelRef}
          className="flex w-[380px] shrink-0 flex-col overflow-y-auto border-l border-border"
        >
          {!isCurrent && (
            <div className="border-b border-border px-3 py-2 text-xs text-tertiary">
              {showsListedVersion ? (
                <>
                  v{selectedSeq} is a past version, shown from its OWN stored bytes —
                  so its bubbles land on the elements they were placed on. Read-only
                  history; nothing is re-anchored across versions.
                </>
              ) : (
                <>
                  v{selectedSeq} kept its content hash and not its bytes, so there is
                  nothing to render and no bubbles are drawn. Its comments are listed
                  here with the excerpt each one captured — act on that.
                </>
              )}
            </div>
          )}
          {doc.kind === "prism" && (
            <div className="border-b border-border px-3 py-2 text-xs text-tertiary">
              This is a deck. Each comment's element lives inside the deck's shadow
              root, which only a browser reaches — so the server reports
              <span className="text-fg"> unresolved</span> rather than exact, and the
              bubble you see is this page's own verdict.
            </div>
          )}

          {pending && (
            <div className="border-b border-border p-3">
              <div className="mb-1 truncate text-xs text-tertiary">
                {pending.chain.length
                  ? pending.chain
                      .slice()
                      .reverse()
                      .map((r) => r.tag.toLowerCase())
                      .join(" › ")
                  : ""}
              </div>
              <textarea
                autoFocus
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={3}
                placeholder="what is wrong with this element?"
                className="w-full border border-border bg-surface p-2 text-sm text-fg"
              />
              <div className="mt-2 flex gap-2">
                <Button size="xs" onClick={() => void saveComment()} disabled={!draft.trim()}>
                  save
                </Button>
                <Button
                  size="xs"
                  variant="outline"
                  onClick={() => {
                    setPending(null);
                    setDraft("");
                  }}
                >
                  cancel
                </Button>
              </div>
            </div>
          )}

          {comments.length === 0 && !pending && (
            <div className="p-4 text-sm text-tertiary">
              No {filter === "all" ? "" : filter} comments on this version. Press C and
              click an element on the page.
            </div>
          )}

          {comments.map((comment) => (
            <div
              key={comment.id}
              id={`redline-item-${comment.id}`}
              onClick={() => focusComment(comment)}
              className={cn(
                "cursor-pointer border-b border-border-subtle p-3",
                highlight === comment.id && "ring-1 ring-fg",
              )}
            >
              <div className="flex items-center gap-2 text-xs text-tertiary">
                <span className="text-fg">#{comment.seq}</span>
                <span>{comment.status}</span>
                <span>·</span>
                <span>{comment.anchor_state}</span>
                <span>·</span>
                <span className="truncate">by {comment.identified_by}</span>
              </div>
              <div className="mt-1 truncate text-xs text-tertiary">{comment.path}</div>
              <div className="mt-1 text-sm text-fg">{comment.body}</div>
              {comment.element && (
                <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all border border-border-subtle p-1 text-xs text-tertiary">
                  {comment.element.slice(0, 240)}
                </pre>
              )}
              {comment.file && comment.line && (
                <div className="mt-1 text-xs text-tertiary">
                  {comment.file}:{comment.line}:{comment.col}
                </div>
              )}
              {comment.status === "done" && comment.done_note && (
                <div className="mt-1 text-xs text-tertiary">done: {comment.done_note}</div>
              )}
              <div className="mt-2 flex gap-2" onClick={(e) => e.stopPropagation()}>
                {comment.status === "open" ? (
                  resolving === comment.id ? (
                    <>
                      <input
                        autoFocus
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                        placeholder="what did you change?"
                        className="min-w-0 flex-1 border border-border bg-surface px-2 py-1 text-xs text-fg"
                      />
                      <Button size="xs" onClick={() => void markDone(comment.id)}>
                        done
                      </Button>
                    </>
                  ) : (
                    <Button
                      size="xs"
                      variant="outline"
                      onClick={() => {
                        setResolving(comment.id);
                        setNote("");
                      }}
                    >
                      mark done
                    </Button>
                  )
                ) : (
                  <Button size="xs" variant="outline" onClick={() => void reopen(comment.id)}>
                    reopen
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
