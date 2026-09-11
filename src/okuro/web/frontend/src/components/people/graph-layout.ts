/**
 * Graph layout — radial placement around the `me` node.
 *
 * Pure math, no side effects. Given N person nodes, returns positions for
 * each node plus the center ("me"). Angle starts at the top (−π/2) so the
 * first neighbour sits directly above. Circle radius scales with N so the
 * graph stays readable from 1 to ~30 people; past that we stop scaling and
 * accept visual crowding.
 */

import type { GraphNode } from "@/lib/people-api";

export type PositionedNode = {
  id: string;
  x: number;
  y: number;
};

export type RadialLayout = {
  me: PositionedNode;
  people: PositionedNode[];
  radius: number;
};

const MIN_RADIUS = 160;
const BASE_RADIUS = 220;
const MAX_RADIUS = 420;

export function layoutRadial(people: GraphNode[]): RadialLayout {
  const n = people.length;
  const radius =
    n <= 1
      ? MIN_RADIUS
      : Math.min(MAX_RADIUS, BASE_RADIUS + Math.max(0, n - 4) * 18);

  const me: PositionedNode = { id: "me", x: 0, y: 0 };

  const placed: PositionedNode[] = people.map((p, i) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(1, n);
    return {
      id: p.id,
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
    };
  });

  return { me, people: placed, radius };
}
