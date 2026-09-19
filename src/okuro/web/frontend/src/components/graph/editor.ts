// <!-- AGENT_HEADER
// role: code
// purpose: ENTRY POINT · the save coordinator shared by the two editor leaves
//   — debounce, one-write-at-a-time, the empty-canvas clobber guard, the
//   no-op-revision guard, base_rev conflict adoption, stale-save cancellation.
// AGENT_HEADER_END -->
//
// THIS ENTRY IS WHERE THE ADDRESS DEFECT WAS FIXED ONCE. The coordinator used
// to call `window.history.replaceState` itself; it now takes an injected
// `syncId` callback, so a late write from a departing pane goes through the
// router instead of stamping this document's id onto the arriving leaf.
export {
  DEBOUNCE_MS,
  useGraphEditor,
  type GraphEditorApi,
  type GraphEditorOptions,
  type SaveBody,
  type SavedDoc,
  type SaveState,
} from "./editor/use-graph-editor";
