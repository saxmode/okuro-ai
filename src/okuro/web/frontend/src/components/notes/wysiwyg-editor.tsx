import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Crepe } from "@milkdown/crepe";
import { editorViewCtx } from "@milkdown/kit/core";
import { callCommand, replaceAll } from "@milkdown/kit/utils";
import { TextSelection } from "@milkdown/kit/prose/state";
import {
  createCodeBlockCommand,
  insertHrCommand,
  toggleEmphasisCommand,
  toggleInlineCodeCommand,
  toggleLinkCommand,
  toggleStrongCommand,
  wrapInBlockquoteCommand,
  wrapInBulletListCommand,
  wrapInHeadingCommand,
  wrapInOrderedListCommand,
} from "@milkdown/kit/preset/commonmark";
import { toggleStrikethroughCommand } from "@milkdown/kit/preset/gfm";
import { redoCommand, undoCommand } from "@milkdown/kit/plugin/history";

import "@milkdown/crepe/theme/common/style.css";
import "@milkdown/crepe/theme/nord-dark.css";
// Loaded last so its okuro-token overrides win over the nord-dark theme.
import "./wysiwyg-theme.css";

import { NoteFormatToolbar, type ToolbarCmd } from "./note-format-toolbar";
import { imageFilesFrom, uploadImageToMarkdown } from "@/lib/note-images";
import { tokenizeApiSrc } from "@/lib/slides-api";

// Reuses the NoteEditor handle shape so the notes page can dictate into
// whichever editor is mounted.
export interface WysiwygEditorHandle {
  insertAtCursor: (text: string) => void;
  focus: () => void;
}

interface Props {
  value: string;
  onChange: (value: string) => void;
  /** Owning note id — pasted/dropped images are associated with it. */
  noteId?: string | null;
  placeholder?: string;
  className?: string;
  /** Show the persistent formatting bar (user-toggleable). Default true. */
  showToolbar?: boolean;
}

/**
 * WYSIWYG markdown surface (Milkdown Crepe). The rendered document IS the
 * editor — markdown stays the source of truth (round-trips to the DB and RAG
 * unchanged), so [[wikilinks]] and ![[drawing:id]] embeds survive as literal
 * text. Lifecycle is imperative (mirrors note-editor.tsx): the instance is
 * created once and external value changes are diffed in without clobbering
 * the caret. Crepe's own selection toolbar is disabled — we ship a persistent
 * bar instead, the notes analogue of okuro-flow's edit bar.
 */
export const WysiwygEditor = forwardRef<WysiwygEditorHandle, Props>(
  function WysiwygEditor({ value, onChange, noteId, placeholder, className, showToolbar = true }, ref) {
    const wrapRef = useRef<HTMLDivElement | null>(null);
    const hostRef = useRef<HTMLDivElement | null>(null);
    const crepeRef = useRef<Crepe | null>(null);
    const [ready, setReady] = useState(false);

    const onChangeRef = useRef(onChange);
    onChangeRef.current = onChange;
    const noteIdRef = useRef(noteId);
    noteIdRef.current = noteId;
    // Last markdown we emitted or pushed in — lets the value effect tell an
    // external change apart from an echo of the user's own typing.
    const lastValueRef = useRef(value);

    // Run a ProseMirror text insertion at the caret. `back` leaves the caret
    // that many chars from the end of the inserted text (e.g. inside [[ ]]).
    const insertText = (text: string, back = 0) => {
      const crepe = crepeRef.current;
      if (!crepe) return;
      crepe.editor.action((ctx) => {
        const view = ctx.get(editorViewCtx);
        const tr = view.state.tr.insertText(text);
        if (back > 0) {
          const pos = tr.selection.from - back;
          tr.setSelection(TextSelection.create(tr.doc, pos));
        }
        view.dispatch(tr);
        view.focus();
      });
    };

    const call = (key: Parameters<typeof callCommand>[0], payload?: unknown) => {
      const crepe = crepeRef.current;
      if (!crepe) return;
      crepe.editor.action(callCommand(key, payload));
      crepe.editor.action((ctx) => ctx.get(editorViewCtx).focus());
    };

    // Link needs an href — toggleLinkCommand throws without one. Prompt for the
    // URL; wrap the selection, or insert the URL as its own linked text when
    // nothing is selected.
    const applyLink = () => {
      const crepe = crepeRef.current;
      if (!crepe) return;
      const href = window.prompt("Link URL")?.trim();
      if (!href) return;
      crepe.editor.action((ctx) => {
        const view = ctx.get(editorViewCtx);
        const { state } = view;
        if (state.selection.empty) {
          const from = state.selection.from;
          const tr = state.tr.insertText(href, from);
          tr.setSelection(TextSelection.create(tr.doc, from, from + href.length));
          view.dispatch(tr);
        }
      });
      crepe.editor.action(callCommand(toggleLinkCommand.key, { href }));
      crepe.editor.action((ctx) => ctx.get(editorViewCtx).focus());
    };

    const runCmd = (cmd: ToolbarCmd) => {
      switch (cmd) {
        case "bold": return call(toggleStrongCommand.key);
        case "italic": return call(toggleEmphasisCommand.key);
        case "strike": return call(toggleStrikethroughCommand.key);
        case "code": return call(toggleInlineCodeCommand.key);
        case "h1": return call(wrapInHeadingCommand.key, 1);
        case "h2": return call(wrapInHeadingCommand.key, 2);
        case "h3": return call(wrapInHeadingCommand.key, 3);
        case "bullet": return call(wrapInBulletListCommand.key);
        case "ordered": return call(wrapInOrderedListCommand.key);
        case "quote": return call(wrapInBlockquoteCommand.key);
        case "codeblock": return call(createCodeBlockCommand.key);
        case "link": return applyLink();
        case "hr": return call(insertHrCommand.key);
        case "undo": return call(undoCommand.key);
        case "redo": return call(redoCommand.key);
        case "wikilink": return insertText("[[]]", 2);
      }
    };

    useImperativeHandle(ref, () => ({
      insertAtCursor: (text: string) => insertText(text),
      focus: () =>
        crepeRef.current?.editor.action((ctx) => ctx.get(editorViewCtx).focus()),
    }));

    // Mount once. Crepe.create() is async, so guard against unmount-before-ready.
    useEffect(() => {
      if (!hostRef.current) return;
      let disposed = false;
      const crepe = new Crepe({
        root: hostRef.current,
        defaultValue: value,
        features: {
          // Our persistent bar replaces Crepe's floating selection toolbar.
          [Crepe.Feature.Toolbar]: false,
          // Off: the image-block feature wraps images in a resizable widget,
          // rewrites alt to a size ratio, and gives each image its own width —
          // which fights the note-width scribble embeds. Plain commonmark <img>
          // renders consistently and honors our CSS.
          [Crepe.Feature.ImageBlock]: false,
        },
        featureConfigs: {
          [Crepe.Feature.Placeholder]: {
            text: placeholder ?? "Write… use [[Title]] to link notes.",
          },
        },
      });
      crepe.on((listener) => {
        listener.markdownUpdated((_ctx, markdown) => {
          lastValueRef.current = markdown;
          onChangeRef.current(markdown);
        });
      });
      crepe.create().then(() => {
        if (disposed) {
          void crepe.destroy();
          return;
        }
        crepeRef.current = crepe;
        setReady(true);
      });
      return () => {
        disposed = true;
        setReady(false);
        crepeRef.current = null;
        void crepe.destroy();
      };
      // Mount once; value/placeholder sync via the effect + refs.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Sync external value (switching notes, dictation via parent) without
    // resetting the caret when the change came from the editor itself.
    useEffect(() => {
      if (!ready) return;
      const crepe = crepeRef.current;
      if (!crepe) return;
      if (value === lastValueRef.current) return;
      if (value === crepe.getMarkdown()) return;
      lastValueRef.current = value;
      crepe.editor.action(replaceAll(value));
    }, [value, ready]);

    // Paste/drop an image → upload → insert ![alt](url). Capture phase so we
    // preempt Crepe's own image handling; markdown stays the source of truth.
    useEffect(() => {
      const host = hostRef.current;
      if (!host || !ready) return;
      const onImages = (dt: DataTransfer | null, ev: Event) => {
        const files = imageFilesFrom(dt);
        if (files.length === 0) return;
        ev.preventDefault();
        ev.stopPropagation();
        files.forEach((file) => {
          void uploadImageToMarkdown(file, noteIdRef.current)
            .then((md) => insertText(`\n${md}\n`))
            .catch(() => {
              /* best-effort — a failed upload just inserts nothing. */
            });
        });
      };
      const onPaste = (e: ClipboardEvent) => onImages(e.clipboardData, e);
      const onDrop = (e: DragEvent) => onImages(e.dataTransfer, e);
      host.addEventListener("paste", onPaste, true);
      host.addEventListener("drop", onDrop, true);
      return () => {
        host.removeEventListener("paste", onPaste, true);
        host.removeEventListener("drop", onDrop, true);
      };
      // insertText is stable (defined in the component body); ready gates mount.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [ready]);

    // Embedded scribbles/images point at bearer-gated /api/ URLs. An <img> can't
    // send the Authorization header, so ProseMirror renders them broken. Rewrite
    // the *displayed* src to carry ?token= (the node's own src attr — hence the
    // saved markdown — stays clean, so no token leaks into the note body/RAG).
    useEffect(() => {
      const host = hostRef.current;
      if (!host || !ready) return;
      const patch = () => {
        host.querySelectorAll("img").forEach((img) => {
          const raw = img.getAttribute("src") || "";
          if (raw.startsWith("/api/") && !raw.includes("token=")) {
            const t = tokenizeApiSrc(raw);
            if (t && t !== raw) img.setAttribute("src", t);
          }
        });
      };
      patch();
      const obs = new MutationObserver(patch);
      obs.observe(host, { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });
      return () => obs.disconnect();
    }, [ready]);

    return (
      <div ref={wrapRef} className={className} style={{ position: "relative", minHeight: 0 }}>
        <div
          ref={hostRef}
          className="milkdown-host overflow-y-auto"
          style={{ position: "absolute", inset: 0 }}
        />
        {showToolbar && <NoteFormatToolbar onCmd={runCmd} disabled={!ready} containerRef={wrapRef} />}
      </div>
    );
  },
);
