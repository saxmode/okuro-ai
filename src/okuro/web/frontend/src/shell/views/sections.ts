// SPDX-License-Identifier: Apache-2.0
/**
 * WHAT SECTIONS EACH LEAF HAS — the table `?view=` resolves against.
 *
 * WHY THIS IS NOT IN THE VIEW FILE, which is the obvious place for it. The
 * router has to know a leaf's sections to turn `?view=gantt` into an index, and
 * the router lives in the ENTRY bundle. If `routes.ts` imported the view module
 * to read a `sections` export, Rollup would pull that view — and then all 31 —
 * into the entry chunk, and the per-leaf code splitting the registry exists for
 * would silently stop happening. Labels are a few bytes; a view is not.
 *
 * WHY NOT IN `ia.ts` EITHER. That file is verbatim from Figma and says so.
 * Sections are NOT in Figma — `Frame 173`/`174` are empty placeholders — so
 * putting them there would put interpretation inside the one file whose value
 * is that it contains none.
 *
 * SO A LEAF IS REGISTERED IN TWO NEIGHBOURING FILES: its module in
 * `registry.ts`, its sections here. The view imports its own row from here
 * rather than re-typing the labels, so there is still exactly one declaration.
 *
 * THE KEY IS `"<topic>/<leaf>"`, identical to `MODULES`. Leaf slugs are only
 * unique WITHIN a topic.
 */

import type { TopicId } from "../ia";

/**
 * The stand-in every un-ported leaf still wears. Four nonsense words on purpose
 * — they are unmistakably not a product vocabulary, so no one mistakes a
 * placeholder for a decision. A leaf loses them by declaring real sections
 * below, not by editing this.
 */
export const PLACEHOLDER_SECTIONS = ["ATOM", "BETOM", "CETOM", "DETOM"] as const;

/**
 * `"<topic>/<leaf>"` -> its section labels, in physical left-to-right order.
 * Order IS the direction law: a section to the right arrives from the right.
 *
 * WHAT p2 CHANGED HERE, AND WHAT IT DELIBERATELY LEFT ALONE.
 *
 * These lists were written in Phase 4 against the 31 SKETCH views, which are now
 * deleted and replaced by okuro's real pages. A section is therefore no longer a
 * visible strip at all: the one component that ever drew one, `SectionPlane`,
 * went with `views/placeholder.tsx` on 2026-09-17. `PLACEHOLDER_SECTIONS` below
 * outlived them both — `routes.ts` re-exports it as the slug set an unresolved
 * address falls back to, so it is routing data now, not a sketch.
 * For a mounted page a section list does exactly one thing — it tells the router
 * which index a `?view=` slug means. That index is read only where a mount
 * dispatches on it (`../mounts/`).
 *
 * CHANGED: the three leaves whose mounts dispatch — `work/tasks` (GANTT),
 * `deliver/prism` (DECK, REDLINE) and `system/settings`, which already declared
 * the three ruled slugs.
 *
 * LEFT ALONE: everything else. `know/brain` still declares SESSIONS, THOUGHTS,
 * MEMORY, PROGRESS — which are the real page's four `?tab=` values, not its
 * `?view=` values — so `?view=thoughts` resolves to index 1 and the page renders
 * at whatever `?tab=` says, exactly as it does today. That is dead vocabulary
 * rather than a defect, and converting `?tab=` to `?view=` is precisely the
 * per-leaf pass p3 exists for (31 URL-bound `?tab=` sub-views plus 11 that are
 * not addressable at all — inventory 7c43ed26 §6.5). Rewriting all 31 lists here
 * on a p2 agent's judgement would be that pass, done without the analysis and
 * without the owner.
 */
export const LEAF_SECTIONS: Record<string, readonly string[]> = {
  // TWO SECTIONS, RULED (START summary S2, 2026-09-15). The Inbox/Staging
  // toggle is the one sub-state on this leaf that is a PARTITION — a different
  // set of rows, which is the shape `?view=` is for. `kind` and `project` are
  // facets OVER one set and stay their own query params. The old single
  // `["INBOX"]` made `?view=` a no-op and left the partition unaddressable, so
  // a staged item could not be linked to at all.
  "start/inbox": ["SURFACED", "STAGING"],
  "start/now": ["NOW"],
  "know/repos": ["REPOS"],
  // THE THREE MODES PLUS THE ROOTS PANEL, RULED (KNOW summary Q7 / Q-C1).
  // The old `["INDEX","ROOTS"]` named nothing the page has: the modes lived in
  // `useState`, so a code search could not be linked and a reload lost the
  // mode AND the query, while `?view=roots` resolved to index 1 and silently
  // did nothing. `INDEX` is retired — it was never a view. SEMANTIC is first
  // because it is the page's default, so no address moves.
  "know/cortex": ["SEMANTIC", "CODE", "ROUTE", "ROOTS"],
  // THE THREE EDITOR SURFACES PLUS THE GRAPH — and ARCHIVED IS RETIRED
  // BECAUSE IT DOES NOT EXIST. `notesApi.list` takes an `archived` flag
  // (`lib/api.ts:1353`) and `notes.tsx` never passes it, so `archived=false`
  // always and `?view=archived` addressed a view with no implementation.
  // LIVE is first because it is the page's own default (`useState("wysiwyg")`),
  // so the bare `/know/notes` opens what it always opened.
  "know/notes": ["LIVE", "SOURCE", "SPLIT", "GRAPH"],
  // FOUR LENSES, RULED (KNOW summary Q7, 2026-09-15). GALLERY · TABLE · GRAPH
  // · TIMELINE are four geometries over ONE node set, and the page held them in
  // `useState` — so no lens could be linked, bookmarked or reloaded. The old
  // single `["GRAPH"]` named the lens that is NOT the default, so `?view=graph`
  // resolved to index 0 and landed on GALLERY: dead vocabulary that pointed the
  // wrong way. GALLERY is first because it is the page's own default, so no
  // existing address moves.
  "know/knowledge": ["GALLERY", "TABLE", "GRAPH", "TIMELINE"],
  "know/brain": ["SESSIONS", "THOUGHTS", "MEMORY", "PROGRESS"],
  "know/lessons": ["CANDIDATE", "ACTIVE", "RETIRED"],
  "know/corpora": ["CORPORA"],
  "work/bridge": ["PROVIDERS"],
  // TWO SECTIONS, ruled jointly with FLOW (spec Q1 option A). The single
  // `["WORKFLOWS"]` addressed nothing — it was the leaf's own name, so
  // `?view=workflows` resolved to index 0 and rendered the gallery you were
  // already on. The blank canvas had `?new=1`, a parameter outside the shell's
  // grammar, and the document id had `?id=`; both are now the shell's own:
  // `/work/workflows/{id}` for a document, `?view=new` for a blank one.
  "work/workflows": ["GALLERY", "NEW"],
  // TWO SECTIONS, and the second one is new. `DIAGRAMS` addressed NOTHING:
  // there is no diagrams mode — Mermaid is a toggle inside the editor — so
  // `?view=diagrams` resolved to index 1 and rendered the identical gallery.
  // Dead IA vocabulary, and the kind that reads as a working page.
  //
  // The blank canvas had `?new=1`, a parameter outside the shell's grammar,
  // and a document had `?id=`. Ruled jointly with WORKFLOWS (FLOW spec Q1
  // option A): `/work/flow/{id}` for a document, `?view=new` for a blank one.
  // `?embed=1` STAYS a query — it is a MODE, not a section, and PRISM embeds
  // it cross-leaf.
  "work/flow": ["FLOWS", "NEW"],
  "work/agents": ["LIVE", "ROLES", "TOOLS"],
  // GANTT REPLACED SCHEDULES IN p2, and both halves of that were measured.
  // GANTT is a ruled address — `LEGACY_VIEWS` sends `/work/gantt` here
  // (`routes.ts:274`) and `mounts/work-tasks.tsx` now dispatches on it, which is
  // the "no change to the route table" that table's comment promised.
  // SCHEDULES went because it addressed nothing: it was a section of the Phase 4
  // sketch, and the real page renders its recurring definitions INLINE
  // (`pages/tasks.tsx:116-141`) rather than as a second view. It was never a
  // live URL either — live sub-views are `?tab=`, not `?view=` — so no bookmark
  // moves. The real scheduled-things leaf is SYSTEM/SCHEDULED.
  // SCHEDULES IS BACK, AND THIS TIME IT ADDRESSES SOMETHING — ruled (TASKS
  // spec Q3, 2026-09-15). p2 REMOVED a SCHEDULES section precisely because it
  // addressed nothing; the recurring definitions render inline below the run
  // list, which measures 8,238px tall, so nobody scrolls to them. AGENTS
  // already links here expecting to land on them (`?focus=<defId>`), which is
  // the use case that makes the label real rather than aspirational.
  //
  // It is NOT the same thing as SYSTEM/SCHEDULED: these are recurring TASK
  // DEFINITIONS, that leaf holds the daemon's timers. The distinction is named
  // in the UI rather than left to be inferred.
  "work/tasks": ["RUNS", "GANTT", "SCHEDULES"],
  // THE TIME WINDOW IS THE SECTION — ruled (WORK/PROJECTS spec Q3, 2026-09-15).
  // The two labels this replaces, REGISTERED and PROVISIONAL, addressed
  // NOTHING: the page has no registered-vs-provisional view and `provisional`
  // is a chip on a card (`projects.tsx:196`), so `?view=provisional` rendered
  // the identical page. Dead vocabulary in the IA.
  //
  // The window is the filter you actually change, and "what moved in 14 days"
  // is the address worth pasting. It lived in `useState`, so no view of the
  // board could be linked and a reload always returned to 14 days.
  "work/projects": ["TOUCHED", "QUARTER", "ALL"],
  // THE TWO MODES ARE THE TWO SECTIONS — Q-L5, ruled. `["SESSIONS"]` named a
  // surface the SHELL could not address: the two modes the page has — a guided
  // interview and the four-step manual flow — sat in `useState`, so the manual
  // flow (ingest, analyze, research, render) had no address at all and
  // `?view=sessions` resolved to index 0, the guided view. GUIDED is first
  // because it is the page's own default, so no address moves.
  //
  // ONE CORRECTION TO THE p3 SPEC, READ FROM SOURCE AND THEN MEASURED. The
  // spec says "the declared section SESSIONS renders nothing — no code lists
  // sessions and `/api/resonance/sessions` has no caller". Both halves are
  // wrong: `components/resonance/interview-session.tsx` calls `sessionList`
  // and renders a "Resume a session" list of each session's goal, status and
  // completeness inside the GUIDED view. Measured on this box:
  // `GET /api/resonance/sessions` answers 200 with `{sessions: []}`, so the
  // list is BUILT AND EMPTY — which is why it looks absent.
  //
  // SESSIONS is therefore retired because it is not a SECTION — it is a list
  // inside one — and not because the feature is missing. Q-L5's option B
  // ("build the session list when RESONANCE gets a feature pass") is already
  // done, which is worth knowing before anyone schedules it again.
  "deliver/resonance": ["GUIDED", "MANUAL"],
  "deliver/assets": ["ICONS"],
  // FOUR SECTIONS AND NOW THEY RESOLVE — MEDIA's D1, and p3's Q1 option B.
  // The four were already declared and addressed NOTHING: `kind` lived in
  // `useState("")` and the mount passed no view, so `/deliver/media?view=image`
  // rendered the All grid (measured). Two things changed: the mechanism, and
  // the ORDER. VIDEO and AUDIO were declared in the opposite order from the
  // chips the user already sees (`KINDS` = image, video, audio, icon,
  // illustration), and order IS the direction law (deb246b8) — a section to
  // the right must arrive from the right.
  //
  // ICON AND ILLUSTRATION STAY IN-PAGE CHIPS, which is the recommendation
  // rather than a ruling, so it is a question in the pass report. The reason
  // to stop at four: ICON duplicates the whole purpose of the ASSETS leaf two
  // labels along the same rail, and the bucket holds 0 icons and 2
  // illustrations against 29 images and 116 audio (measured) — a rail slot
  // that is always empty teaches the wrong thing about the rail.
  "deliver/media": ["ALL", "IMAGE", "VIDEO", "AUDIO"],
  "deliver/podcast": ["EPISODES"],
  // TWO TOOLS, TWO ADDRESSES — STUDIO's D1 and p3's Q1 option A. The page is
  // Image generation and a local-model chat over two disjoint endpoint
  // families, switched by a `useState` — so "open Studio's text chat" could
  // not be said as a URL. The old `["LIBRARY"]` named NEITHER tool and the
  // panel the page renders LAST: measured, `?view=library` rendered Image
  // mode. IMAGE is first because it is the page's own default, so no address
  // moves. LIBRARY is not a third section (p3's Q1 option B) because the
  // library is a RESULT view of Image mode and loses the generate-then-see-it
  // adjacency as a peer.
  "deliver/studio": ["IMAGE", "TEXT"],
  // THREE ADDRESSES, ruled onto the grammar FLOW and WORKFLOWS already took
  // (p3 SLIDES spec section 4). The leaf used to declare ONE section and open
  // `decks[0]` on arrival, so a deck could not be linked -- on the leaf whose
  // whole purpose is sending a deck to somebody. DECKS is the gallery at the
  // bare address, EDIT and PLAY are the two things you do to a deck that the
  // path names.
  "deliver/slides": ["DECKS", "EDIT", "PLAY"],
  // THE TWO TABS ARE TWO ADDRESSES — p3's Q3, ruled A. The page is a Radix
  // `Tabs` over a relationship GRAPH and a list/settings DIRECTORY, and the
  // choice lived in `defaultValue="graph"`, so "open the owner's sliders" could
  // not be said as a URL and browser Back did not leave the settings tab.
  //
  // THE DECLARED SECTION BEFORE WAS `["DIRECTORY"]`, WHICH NAMED THE TAB THAT
  // IS NOT THE DEFAULT — dead vocabulary that also pointed the wrong way:
  // `?view=directory` resolved to index 0, which is the GRAPH. GRAPH is first
  // now because it is the page's own default, so no existing address moves.
  //
  // NOT A THIRD `PROFILE` SECTION (p3's Q3 option B): a section that is
  // meaningless without an id is a new shell concept, and the charter puts
  // shell changes behind their own ruling. The person id belongs in the PATH,
  // which is the grammar `redline` already proves.
  "deliver/people": ["GRAPH", "DIRECTORY"],
  // TWO SECTIONS ADDED IN p2, BOTH RULED, AND ONE DELIBERATELY NOT ADDED.
  // DECK is `LEGACY_VIEWS`'s `/prism/deck` (`routes.ts:275`); REDLINE is
  // the owner's ruling of 2026-09-14 (memory 0e3cfeaa, "redline lives at
  // /deliver/prism?view=redline"). `mounts/deliver-prism.tsx` dispatches both.
  // GALLERY IS ABSENT ON PURPOSE — `prism-gallery` has no ruling ("I don't know.
  // Prism is work in progress"), so it stays at its legacy path and is named in
  // `routes.ts::UNHOMED` instead of being given a section nobody chose.
  "deliver/prism": ["DOCUMENTS", "DECK", "REDLINE"],
  "system/about": ["ABOUT"],
  // The slugs `embed`, `stt` and `tts` are load-bearing: LEGACY_VIEWS maps
  // /settings/embed|stt|tts onto ?view= with exactly these spellings, so a
  // rename here silently sends three live bookmarks back to section 0.
  "system/settings": ["GENERAL", "EMBED", "STT", "TTS"],
  // FOUR SECTIONS, RULED (Q-L1 option A, "the page's own tab order"). The old
  // `["PROFILES","BRANDS"]` was wrong three ways at once, all measured:
  // `?view=profiles` and `?view=entries` BOTH left the page on Brands; the two
  // declared slugs were in the opposite order from the rendered strip, which
  // under the direction law is a contradiction; and ENTRIES and REVIEW had no
  // section at all, so two of four sub-views were unaddressable.
  // BRANDS is first because it is the page's own default, so no address moves.
  "system/stacks": ["BRANDS", "PROFILES", "ENTRIES", "REVIEW"],
  // MODELS' SUB-VIEWS GET ADDRESSES — p3's Q-L1, the cheapest conversion in the
  // topic. The page toggles them with `ui/segmented`, already controlled, and
  // it never had a `?tab=` to migrate: the choice lived in `useState`, so
  // refresh, back/forward and bookmarking all dropped it and no panel but the
  // default could be linked to at all. The single `["MODELS"]` section it
  // declared before addressed nothing — measured: `?view=models` left the page
  // byte-identical at 4,039 characters.
  //
  // FIVE, NOT TWO, SINCE MAIN'S model-manager WORK LANDED. The leaf pass found
  // an Installed/Discover pair; main turned it into five tabs, one per question
  // the page exists for (Inventory Q1, Consumers Q2, Interesting Q3/Q5, Swap
  // plans Q4, plus the pre-existing registry). All five are sub-views by the
  // same test the other leaves used, so all five are addressable — the ruling
  // was about the MECHANISM, not about the two panels that happened to exist
  // when it was written.
  //
  // ORDER IS THE DIRECTION LAW: this is the page's own rendered strip order,
  // and INSTALLED stays first because it is still the page's default, so no
  // existing address moves.
  //
  // `pages/models.tsx` READS THIS LIST IN BOTH DIRECTIONS and never a pinned
  // index, so inserting a section here cannot silently start rendering a
  // different panel. `shell/tests/sections.test.ts` holds the other half of
  // that contract.
  "system/models": [
    "INSTALLED",
    "INVENTORY",
    "CONSUMERS",
    "INTERESTING",
    "SWAPS",
  ],
  // ONE SECTION, BECAUSE THE PAGE HAS ONE VIEW. The three slugs that stood
  // here — DAEMON, TIMERS, RECURRING — were Phase-4 sketch vocabulary and
  // addressed nothing: measured, `/system/scheduled?view=timers` left the URL
  // and the pane identical with all three panels still stacked, because the
  // page has no sub-view mechanism at all (no `useUrlTab`, no Radix Tabs, no
  // `useState` mode). They were also in a DIFFERENT ORDER from the rendered
  // panels (Recurring, Daemon, Timers), which under the direction law is a
  // contradiction rather than a label.
  //
  // NOT SPLIT INTO THREE REAL SECTIONS, which was p3's Q1 option A. Ruled B:
  // "One place for everything that runs on a clock, across three layers" is a
  // product decision recorded in the page's own header comment, and the column
  // collapse that made splitting attractive has its own one-line fix (the
  // identity-column floor in `components/schedule/cells.tsx`). Splitting the
  // IA to relieve a layout bug would be fixing the symptom one layer up.
  //
  // `["SCHEDULED"]` RATHER THAN `[]`, AND THAT IS NOT A PREFERENCE. Q-L1's
  // proposal column says `[]` for this leaf, and `[]` is REFUSED by this
  // module's own invariant — `shell/tests/sections.test.ts`, "gives every
  // declared leaf at least one section", `expect(sections.length)
  // .toBeGreaterThan(0)`. One section named for the leaf is also what every
  // other no-sub-view leaf declares (`system/services: ["SERVICES"]`,
  // `start/now: ["NOW"]`), and section 0 is the bare address, so nothing emits
  // a `?view=` for it and no URL changes meaning.
  "system/scheduled": ["SCHEDULED"],
  // THE FIVE DECLARED SECTIONS ARE NOW REAL — Q-L1, ruled. The list is
  // unchanged: it was already five-for-five in the page's own rendered order,
  // and the only thing missing was a mechanism. What it cost before, measured:
  // `?view=storage` and `?view=machine` both left the page on Services.
  //
  // TWO SPELLINGS DIVERGE FROM THE PAGE'S OLD `?tab=` VALUES AND BOTH ARE THE
  // BETTER NAMES: CHECKS rather than `services`, because SYSTEM has a SERVICES
  // *leaf* two labels along this rail and the tab is doctor checks; MACHINE
  // rather than `system`, because the topic is already SYSTEM. The two old
  // spellings keep resolving through `SECTION_ALIASES` above, so a live
  // `?tab=system` bookmark lands on MACHINE rather than silently on CHECKS.
  "system/health": ["CHECKS", "MACHINE", "STORAGE", "SCHEDULES", "CORTEX"],
  "system/services": ["SERVICES"],
  "system/design": ["KITS"],
};

/**
 * A SLUG A LEAF USED TO ANSWER TO, AND THE SECTION IT MEANS NOW.
 *
 * Every leaf pass so far could convert `?tab=` to `?view=` without touching a
 * spelling, because the page's tab values and the declared section names were
 * the same words (BRAIN: sessions/memory/progress/thoughts). HEALTH is the
 * first leaf where the spelling itself changes, and it changes for a reason
 * the shell can see: the page called its doctor-check tab `services` while
 * SYSTEM has a SERVICES *leaf* two labels along the same rail, and it called
 * its hardware tab `system` inside `/system/…`. Both new names are better.
 *
 * WITHOUT THIS TABLE A LIVE BOOKMARK DOES NOT ERROR, IT LIES. `viewIndexFrom`
 * resolves an unknown slug to 0, so `?tab=system` — the hardware tab — would
 * land silently on CHECKS and look like the page simply opened on its default.
 * That is the failure mode this project has now met three times: a wrong
 * answer that is indistinguishable from a right one.
 *
 * ONE DIRECTION ONLY. Nothing ever WRITES an alias — `pathWithView` emits the
 * canonical slug — so an alias can only ever be read off an incoming URL.
 */
export const SECTION_ALIASES: Record<string, Readonly<Record<string, string>>> = {
  "system/health": { services: "checks", system: "machine" },
  // MAIN RENAMED THE PANEL `Discover` -> `Interesting` (it now carries lineage
  // relation and placement fit, not just a catalog search), and the leaf pass
  // had already shipped `?view=discover` as an address. Nothing live points at
  // it — this branch has never been deployed — but a `?tab=discover` typed or
  // written down against the old spelling would resolve to 0 and land on
  // INSTALLED, which is the failure this table exists for: a wrong answer that
  // is indistinguishable from a right one.
  "system/models": { discover: "interesting" },
};

/** A leaf's sections, or the placeholder set while it has none. */
export function sectionsFor(topic: TopicId | string, leaf: string): readonly string[] {
  return LEAF_SECTIONS[`${topic}/${leaf}`] ?? PLACEHOLDER_SECTIONS;
}

/** The canonical slug an incoming one means, for this leaf. Identity by default. */
export function canonicalSectionSlug(
  topic: TopicId | string,
  leaf: string,
  slug: string,
): string {
  return SECTION_ALIASES[`${topic}/${leaf}`]?.[slug] ?? slug;
}

/** A section's URL spelling. `RUNS` -> `runs`, same rule as a leaf slug. */
export function sectionSlugs(topic: TopicId | string, leaf: string): string[] {
  return sectionsFor(topic, leaf).map((s) => s.toLowerCase());
}
