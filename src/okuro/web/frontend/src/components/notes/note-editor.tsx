import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { EditorView, keymap, placeholder as cmPlaceholder } from "@codemirror/view";
import { EditorState } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import {
  autocompletion,
  completionKeymap,
  type CompletionContext,
  type CompletionResult,
} from "@codemirror/autocomplete";
import { imageFilesFrom, uploadImageToMarkdown } from "@/lib/note-images";

export interface NoteEditorHandle {
  /** Insert text at the current cursor (used by voice dictation). */
  insertAtCursor: (text: string) => void;
  focus: () => void;
}

interface NoteEditorProps {
  value: string;
  onChange: (value: string) => void;
  /** Note titles offered as [[wikilink]] autocompletions. */
  linkOptions: string[];
  /** Owning note id — pasted/dropped images are associated with it. */
  noteId?: string | null;
  placeholder?: string;
  className?: string;
}

/** [[ ]] autocomplete: fires while typing inside an unclosed [[…, completing
 *  to an existing note title and closing the brackets. */
function wikilinkSource(getOptions: () => string[]) {
  return (context: CompletionContext): CompletionResult | null => {
    const before = context.matchBefore(/\[\[([^\]\n]*)$/);
    if (!before) return null;
    const typed = before.text.slice(2).toLowerCase();
    const options = getOptions()
      .filter((o) => o.toLowerCase().includes(typed))
      .slice(0, 20)
      .map((o) => ({ label: o, type: "text", apply: `${o}]]` }));
    if (options.length === 0 && typed.length === 0) return null;
    return { from: before.from + 2, options, validFor: /^[^\]\n]*$/ };
  };
}

const theme = EditorView.theme(
  {
    "&": {
      backgroundColor: "transparent",
      color: "var(--color-fg-primary)",
      height: "100%",
      fontSize: "0.875rem",
    },
    ".cm-scroller": { fontFamily: "var(--font-mono)", lineHeight: "1.65", overflow: "auto" },
    ".cm-content": { padding: "1rem", caretColor: "var(--color-accent)" },
    ".cm-cursor": { borderLeftColor: "var(--color-accent)" },
    "&.cm-focused": { outline: "none" },
    ".cm-gutters": { display: "none" },
    ".cm-activeLine": { backgroundColor: "transparent" },
    ".cm-tooltip": {
      backgroundColor: "var(--color-surface-elevated)",
      border: "1px solid var(--color-border)",
      borderRadius: "var(--radius-md, 2rem)",
      boxShadow: "var(--shadow-md)",
    },
    ".cm-tooltip-autocomplete > ul > li": { padding: "2px 8px", fontFamily: "var(--font-mono)" },
    ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
      backgroundColor: "var(--color-accent-subtle)",
      color: "var(--color-fg-primary)",
    },
  },
  { dark: true },
);

/** Controlled CodeMirror 6 markdown editor. The view is created once; external
 *  value changes are diffed in so user typing is never clobbered. */
export const NoteEditor = forwardRef<NoteEditorHandle, NoteEditorProps>(
  function NoteEditor({ value, onChange, linkOptions, noteId, placeholder, className }, ref) {
    const hostRef = useRef<HTMLDivElement | null>(null);
    const viewRef = useRef<EditorView | null>(null);
    const onChangeRef = useRef(onChange);
    const optionsRef = useRef(linkOptions);
    const noteIdRef = useRef(noteId);
    onChangeRef.current = onChange;
    optionsRef.current = linkOptions;
    noteIdRef.current = noteId;

    // Paste/drop an image → upload → insert ![alt](url) at the caret. Async, so
    // we snapshot the insertion point and splice the markdown in when it lands.
    const insertImages = (view: EditorView, files: File[]) => {
      files.forEach((file) => {
        void uploadImageToMarkdown(file, noteIdRef.current)
          .then((md) => {
            const pos = view.state.selection.main.head;
            const text = (pos > 0 ? "\n" : "") + md + "\n";
            view.dispatch({
              changes: { from: pos, insert: text },
              selection: { anchor: pos + text.length },
            });
          })
          .catch(() => {
            /* best-effort — a failed upload just inserts nothing. */
          });
      });
    };

    useImperativeHandle(ref, () => ({
      insertAtCursor: (text: string) => {
        const view = viewRef.current;
        if (!view) return;
        const pos = view.state.selection.main.head;
        view.dispatch({
          changes: { from: pos, insert: text },
          selection: { anchor: pos + text.length },
        });
        view.focus();
      },
      focus: () => viewRef.current?.focus(),
    }));

    useEffect(() => {
      if (!hostRef.current) return;
      const view = new EditorView({
        parent: hostRef.current,
        state: EditorState.create({
          doc: value,
          extensions: [
            history(),
            keymap.of([...defaultKeymap, ...historyKeymap, ...completionKeymap]),
            markdown(),
            autocompletion({ override: [wikilinkSource(() => optionsRef.current)] }),
            EditorView.lineWrapping,
            cmPlaceholder(placeholder ?? "Write markdown… use [[Title]] to link notes."),
            theme,
            EditorView.updateListener.of((u) => {
              if (u.docChanged) onChangeRef.current(u.state.doc.toString());
            }),
            EditorView.domEventHandlers({
              paste: (event, view) => {
                const files = imageFilesFrom(event.clipboardData);
                if (files.length === 0) return false;
                event.preventDefault();
                insertImages(view, files);
                return true;
              },
              drop: (event, view) => {
                const files = imageFilesFrom(event.dataTransfer);
                if (files.length === 0) return false;
                event.preventDefault();
                insertImages(view, files);
                return true;
              },
            }),
          ],
        }),
      });
      viewRef.current = view;
      return () => {
        view.destroy();
        viewRef.current = null;
      };
      // Mount once; value/options/placeholder sync via the effects + refs below.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Sync external value (e.g. switching notes, dictation via parent) without
    // resetting the cursor when the change originated from the editor itself.
    useEffect(() => {
      const view = viewRef.current;
      if (!view) return;
      const current = view.state.doc.toString();
      if (value !== current) {
        view.dispatch({ changes: { from: 0, to: current.length, insert: value } });
      }
    }, [value]);

    return <div ref={hostRef} className={className} />;
  },
);
