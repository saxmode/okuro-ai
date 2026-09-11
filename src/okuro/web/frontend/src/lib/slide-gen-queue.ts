/**
 * Hands a slide-generation request from the (global) chat "slides" action to the
 * SlidesPage that mounts after navigation — mirrors flow-stream's queue. The
 * page consumes the pending topic on mount and runs generation.
 */
export type GenMode = "fast" | "quality";
interface PendingGen { topic: string; personId?: string; mode?: GenMode; brandId?: string }
let _pending: PendingGen | null = null;

export function queueSlideGen(topic: string, personId?: string, mode?: GenMode, brandId?: string): void {
  _pending = { topic, personId, mode, brandId };
}

export function takeSlideGen(): PendingGen | null {
  const p = _pending;
  _pending = null;
  return p;
}

// The deck currently open in the SlidesPage — so the global chat's "slides_edit"
// action knows which deck to edit/review without threading it through routing.
let _openDeckId: string | null = null;
export function setOpenDeckId(id: string | null): void {
  _openDeckId = id;
}
export function getOpenDeckId(): string | null {
  return _openDeckId;
}
