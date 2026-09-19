// SPDX-License-Identifier: Apache-2.0
/**
 * THE SHELL'S ICONOGRAPHY — one glyph per topic, one per leaf, one per panel row.
 *
 * ===========================================================================
 * THE BRIEF'S PREMISE WAS WRONG AND THIS FILE IS WHAT REPLACES IT.
 * ===========================================================================
 * C1 was dispatched as "use the REAL per-leaf icons the live navigation used —
 * the old nav-bar.tsx / NAV_TREE icon set". THERE IS NO SUCH SET. Measured at
 * `3985db7b9^`, the commit that deleted the old chrome:
 *
 *   components/shell/nav-bar.tsx      ChevronRight, Moon, Sun     (367 lines)
 *   components/shell/app-shell.tsx    PanelLeftOpen
 *   components/shell/mobile-top-bar   Menu, Search, Sparkles
 *   components/shell/okuro-thinker    Loader2, X
 *   components/shell/window-chrome    ChevronLeft, ChevronRight
 *
 * `NAV_TREE`'s own type is `{ to, label, create? }` — there is no icon field and
 * never was. The live navigation was a TEXT accordion: five uppercase group
 * labels revealing uppercase leaf labels. So every glyph below is NEW, and the
 * whole table is a proposal for the owner rather than a restoration of something
 * that existed. That is why it is one file with one comment instead of 41
 * scattered imports.
 *
 * ===========================================================================
 * WHY LUCIDE, AND WHY THAT IS NOT A NEW LITERAL UNDER D1
 * ===========================================================================
 * D1 (`685cc4b4`) says nothing styling lives outside okuro-ds. An icon is not a
 * styling value — it is an asset, and the frontend already declares its source:
 * `components.json` sets `"icons": "lucide"` and `package.json:63` pins
 * `lucide-react ^0.468.0`. So this file adds no vocabulary; it names members of
 * a set the app already ships. Every name below was verified to EXIST in the
 * installed 0.468.0 rather than assumed — `Prism` does not, which is why PRISM
 * carries `Gem`.
 *
 * SIZE AND COLOUR ARE THE STYLESHEET'S, NOT THE COMPONENT'S. Each icon renders
 * with no `size` prop and no colour; `shell.css` sizes the `svg` to 100% of its
 * 16px slot and the slot's `currentColor` carries the kit's ink tier. Passing
 * `size={16}` would have written `--sh-icon`'s value into 41 call sites, which
 * is the literal D1 actually forbids.
 *
 * ===========================================================================
 * THE ARRAYS ARE PARALLEL TO `ia.ts` AND `icons.test.ts` ENFORCES IT
 * ===========================================================================
 * `leaves[i]` belongs to `kids[i]`. A leaf added to the IA without an icon here
 * fails a test rather than rendering an empty square, which is the same
 * discipline `titles` already has. Order is the direction law; do not sort.
 */

import {
  Activity,
  Archive,
  Bot,
  BookOpen,
  Box,
  Brain,
  Cable,
  CalendarClock,
  ChevronDown,
  ChevronUp,
  Cpu,
  FolderKanban,
  Gauge,
  Gem,
  GitBranch,
  GraduationCap,
  Hammer,
  HeartPulse,
  Image,
  Inbox,
  Info,
  Layers,
  Library,
  Lightbulb,
  ListChecks,
  Mic,
  Newspaper,
  NotebookPen,
  Palette,
  Presentation,
  ScanSearch,
  Send,
  Server,
  Settings,
  Shapes,
  Share2,
  Sparkles,
  Sunrise,
  TrendingUp,
  Users,
  Waves,
  Workflow,
  Zap,
  type LucideIcon,
} from "lucide-react";

import type { TopicId } from "./ia";

/**
 * THE TOPIC GLYPH — the one in `.bar-ico`, the fixed point of the whole
 * transformation. It is the only icon that is visible in BOTH bar variants, so
 * it has to read at 16px against a 48px rail and still make sense beside an
 * expanded 836px section.
 */
export const TOPIC_ICONS: Readonly<Record<TopicId, LucideIcon>> = {
  start: Sunrise,   // the day's entry point — INBOX + NOW
  know: BookOpen,   // the library; BRAIN below takes the brain
  work: Hammer,     // work being done, not a calendar of it
  deliver: Send,    // "ship it to the right shape"
  system: Cpu,      // the machine under the product
};

/**
 * THE LEAF GLYPHS, parallel to `Topic.kids`.
 *
 * Chosen so no glyph repeats anywhere in the set — 41 slots, 41 distinct
 * pictograms — because a repeated icon in a rail of 8 is worse than a generic
 * one. Two near-collisions were resolved deliberately: KNOW's topic glyph moved
 * to `BookOpen` so `Brain` could belong to the BRAIN leaf, and PRISM took `Gem`
 * because lucide 0.468.0 ships no `Prism`.
 */
export const LEAF_ICONS: Readonly<Record<TopicId, readonly LucideIcon[]>> = {
  // INBOX, NOW
  start: [Inbox, Zap],
  // REPOS, CORTEX, NOTES, KNOWLEDGE, BRAIN, LESSONS, CORPORA
  know: [GitBranch, ScanSearch, NotebookPen, Library, Brain, GraduationCap, Archive],
  // BRIDGE, WORKFLOWS, FLOW, AGENTS, TASKS, PROJECTS
  work: [Cable, Workflow, Share2, Bot, ListChecks, FolderKanban],
  // RESONANCE, ASSETS, MEDIA, PODCAST, STUDIO, SLIDES, PEOPLE, PRISM
  deliver: [Waves, Shapes, Image, Mic, Sparkles, Presentation, Users, Gem],
  // ABOUT, SETTINGS, STACKS, MODELS, SCHEDULED, HEALTH, SERVICES, DESIGN
  system: [Info, Settings, Layers, Box, CalendarClock, HeartPulse, Server, Palette],
};

/**
 * THE PANEL'S FIVE ICONS — PROPOSED, AND THE ONE SET WITH NO BEHAVIOUR YET.
 *
 * These five slots have never had an action: they are `<i>` elements, they are
 * not focusable, and nothing in the shell or the live app records what they were
 * meant to open. Figma draws five boxes and stops there.
 *
 * SO THIS IS AN INVENTION AND IT IS LABELLED ONE. The five glyphs answer the
 * only documented candidate list there is — the four panels the OLD sidebar
 * carried, which `p3 KNOW/BRAIN` enumerated as Sessions / Telemetry / Progress
 * / Thoughts — plus the daily brief, which the panel's own first row already
 * names. They stay UNFOCUSABLE (C4), so an icon here promises nothing it cannot
 * do; it replaces a blank square with a hint. The owner rules the meanings.
 */
export const PANEL_ICONS: readonly LucideIcon[] = [
  Newspaper,    // the daily brief
  Activity,     // sessions / live activity
  Gauge,        // telemetry
  Lightbulb,    // thoughts
  // PROGRESS. It was `Layers` and the distinctness test caught it colliding
  // with SYSTEM's STACKS leaf — the comment claimed "the family, not the
  // glyph", which was simply wrong. That is the assertion earning its keep.
  TrendingUp,
];

/**
 * THE PANEL ROWS' DISCLOSURE ARROWS.
 *
 * They were the literal characters `^` and `v` in the markup — a caret and a
 * lowercase vee standing in for a chevron, at the row's own 10px type size and
 * in the row's own typeface. That is the one place in the shell where a glyph's
 * shape depended on the kit's FONT rather than on the kit's tokens, so under a
 * kit whose face has a different caret the arrow changed shape. Real chevrons
 * are 16px paths that cannot drift.
 */
export const ROW_CHEVRON = { up: ChevronUp, down: ChevronDown } as const;

/** Exported for the parity test — the count that must match `kids.length`. */
export function leafIcon(topic: TopicId, leafIndex: number): LucideIcon {
  const set = LEAF_ICONS[topic];
  return set[leafIndex] ?? set[0] ?? TOPIC_ICONS[topic];
}
