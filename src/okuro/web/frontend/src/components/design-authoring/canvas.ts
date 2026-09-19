/**
 * WHERE A PREVIEW GETS ITS GROUND — page-neutral, because two pages ask it.
 *
 * `preview-frame.tsx` renders a TREE against a MODEL. Everything in this file is
 * the arithmetic between those two: which ground the root sits on, which ground
 * each node descends to, and which resolved ground a key names. It was inline in
 * `pages/design-engine.tsx`, which meant the only surface that could mount the
 * frame was that page — the class the authoring-core migration ends.
 *
 * NOTHING HERE COMPUTES A COLOUR. `transitions` is the engine's own table, read
 * off the resolved model; a key is looked up, never derived. That is the charter
 * rule about a projection that computes being a second authority, applied to the
 * one place a page could most easily have re-derived the descent by hand.
 */

import { useMemo } from "react";

import type { ResolvedModel, TreeNode } from "@/components/design-engine/types";

/** A canvas with a root and nothing placed in it — what a page starts from. */
export const EMPTY_TREE: TreeNode = {
  id: "root",
  request: null,
  groundKey: "",
  children: [],
};

/**
 * Re-key a tree against a model.
 *
 * The tree stores REQUESTS, never colours. Ground keys are looked up in the
 * engine's transition table on every model change, so a colour edit re-keys the
 * whole tree without the builder's structure moving at all.
 */
export function retree(
  node: TreeNode,
  parentKey: string,
  transitions: Record<string, Record<string, string>>,
): TreeNode {
  const key = node.request
    ? (transitions[parentKey]?.[node.request] ?? parentKey)
    : parentKey;
  return {
    ...node,
    groundKey: key,
    children: node.children.map((child) => retree(child, key, transitions)),
  };
}

export interface CanvasGround {
  /** The resolved ground key the canvas root sits on. `""` until the model lands. */
  rootKey: string;
  /** The tree, re-keyed against the model. `null` while there is no root yet. */
  keyedTree: TreeNode | null;
  /** Every resolved ground by its key — the lookup an inspector or a gate needs. */
  groundsByKey: Record<string, ResolvedModel["grounds"][number]>;
  /** The engine's own descent table, ground key -> request -> ground key. */
  transitions: Record<string, Record<string, string>>;
}

export interface CanvasGroundInput {
  model: ResolvedModel | null;
  /** The user's choice. Only `light` and `dark` are a user's to make. */
  appearance: "light" | "dark";
  /**
   * A BUILDER PLACEMENT at the root — brand-full or a signal. `null` means the
   * root is whatever the appearance says, which is the answer a reader gets
   * without touching anything.
   */
  rootPlacement: string | null;
  /** Defaults to `EMPTY_TREE`: a root and nothing in it. */
  tree?: TreeNode;
}

/**
 * ONE ANSWER FOR "WHAT GROUND IS THE CANVAS ON", for every page that has a canvas.
 *
 * The fallback chain is the part worth keeping: a placement that names no root
 * falls back to the appearance's root rather than to the first root in the list,
 * because the appearance is the only thing a USER chose and a silent fallback to
 * something else would paint a ground nobody asked for.
 */
export function useCanvasGround({
  model,
  appearance,
  rootPlacement,
  tree = EMPTY_TREE,
}: CanvasGroundInput): CanvasGround {
  const transitions = useMemo(() => {
    const table: Record<string, Record<string, string>> = {};
    for (const ground of model?.grounds ?? []) table[ground.key] = ground.transitions;
    return table;
  }, [model]);

  const rootKey = useMemo(() => {
    if (!model) return "";
    const wanted = rootPlacement ?? appearance;
    return (
      model.roots.find((r) => r.id === wanted)?.key ??
      model.roots.find((r) => r.id === appearance)?.key ??
      ""
    );
  }, [model, rootPlacement, appearance]);

  const keyedTree = useMemo(
    () => (rootKey ? retree({ ...tree, groundKey: rootKey }, rootKey, transitions) : null),
    [tree, rootKey, transitions],
  );

  const groundsByKey = useMemo(() => {
    const map: Record<string, ResolvedModel["grounds"][number]> = {};
    for (const ground of model?.grounds ?? []) map[ground.key] = ground;
    return map;
  }, [model]);

  return { rootKey, keyedTree, groundsByKey, transitions };
}

/**
 * EVERY STAGE REVEALED AT ONCE, named by the ENGINE rather than by a page.
 *
 * `preview-frame.tsx` gates each `.reveal` block on `body[data-ready~="<stage>"]`,
 * and the growth choreography on `/design-engine` walks that list one beat at a
 * time. A page that mounts the canvas as a STAGE rather than as a demonstration
 * wants the finished state immediately — and it must never show the frame's
 * loading overlay, which lifts only once `foregrounds` has arrived.
 *
 * `descent` is deliberately EXCLUDED. It is the beat that gives the per-node
 * "acts" bar pointer events, and that bar is a tree-editing affordance: a page
 * with no tree editor would render seven controls that accept a click and do
 * nothing, which is the shape the charter calls a control that cannot know it is
 * inert. A canvas with no children loses nothing else by leaving it out.
 *
 * Read off the payload the server sends, never typed here, so a stage added to
 * `growth.STAGES` reaches every page that uses this without an edit.
 */
export function revealAll(stages: { key: string }[]): string {
  return stages
    .map((stage) => stage.key)
    .filter((key) => key !== "descent")
    .join(" ");
}
