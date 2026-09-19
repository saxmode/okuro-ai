// SPDX-License-Identifier: Apache-2.0
/**
 * THE ADDRESS LAYER — the path is the truth, and this file is where it is read.
 *
 * The owner: *"router is crucial! what we've built now is only a content
 * organizer. it's not an architecture. architecture, reachability, linkability
 * etc. still needs to exist!"*
 *
 * Before this, sub-item selection was `Record<TopicId, number>` — an integer
 * index in memory. It could not express `/work/abc123`, did not survive a
 * reload, and could not be pasted to anyone. Every geometric proof was true and
 * none of it made the shell an application.
 *
 * THE GRAMMAR
 *
 *     /{topic}/{leaf}          the navigable surface — 27 of these
 *     /{topic}/{leaf}/{id}     detail below a leaf
 *     /                        resolves
 *     /{topic}                 resolves
 *
 * DETAIL NESTS ONE LEVEL DEEPER — `/work/tasks/abc123`, not `/work/abc123`.
 * The live app's `/work/:id` collides with `/work/{leaf}` the moment a leaf is
 * named like an id, and it collides SILENTLY. Reserving leaf slugs and falling
 * through to `:id` trades a one-time migration for a permanent latent failure,
 * so the redirects get paid now.
 *
 * CHROME IS NOT IN THE PATH. `panel` and `content` are view preferences, not
 * locations: a link you paste should take someone to the content, not impose
 * your panel state. They persist per-viewer instead.
 *
 * Everything here is a pure function of the path and the IA, so the whole
 * resolution table is unit-testable without a browser or a router.
 */

import type { TopicId } from "./ia";
import { IA, topicIndex } from "./ia";
import {
  PLACEHOLDER_SECTIONS,
  canonicalSectionSlug,
  sectionSlugs,
} from "./views/sections";

/** A leaf's slug is its label, lowercased. `AGENTS` -> `agents`. */
export function slugify(kid: string): string {
  return kid.toLowerCase();
}

export function leafSlugs(topic: TopicId): string[] {
  const t = IA.find((x) => x.id === topic);
  return t ? t.kids.map(slugify) : [];
}

/** The canonical path for a topic + leaf index. */
export function pathFor(topic: TopicId, leafIndex: number, id?: string): string {
  const slugs = leafSlugs(topic);
  const slug = slugs[leafIndex] ?? slugs[0];
  return id ? `/${topic}/${slug}/${id}` : `/${topic}/${slug}`;
}

/**
 * THE DEFAULT ROOT, ISOLATED IN ONE FUNCTION ON PURPOSE.
 *
 * Whether `/` lands on a fixed leaf or on wherever you were last is product
 * behaviour rather than architecture, and it is the owner's call. Building
 * against a fixed root keeps the routing layer unblocked; switching to
 * last-visited is a change to this function and nothing else.
 */
export const FALLBACK_ROOT: { topic: TopicId; leaf: number } = { topic: "start", leaf: 1 }; // /start/now

/**
 * `/` GOES TO WHEREVER YOU WERE LAST. The owner ruled it, against both my lean and
 * the datum that live `/` IS the NOW leaf — so this is a change in behaviour
 * rather than a preservation of it, chosen deliberately: he context-switches
 * between projects constantly and being returned to where he was is worth more
 * to him than `/` meaning a fixed thing.
 *
 * A FRESH VISITOR STILL NEEDS A DESTINATION, so with no memory this falls back
 * to `/start/now` — which is exactly what live `/` means today.
 *
 * THIS FUNCTION IS THE ONE DELIBERATELY NON-DETERMINISTIC PLACE IN THE ROUTER,
 * and it is kept isolated so that can be pointed at. Everything else resolves
 * the same way for everyone; only `/` depends on who is asking.
 */
export function resolveRoot(lastLocation?: string | null): string {
  if (lastLocation) {
    // `split` is typed as possibly-empty under `noUncheckedIndexedAccess`; a
    // split on a non-empty string always yields a first element, and `""` is a
    // valid pathname for the resolver anyway (it falls through to the root rule).
    const [path = "", search] = lastLocation.split("?");
    const r = resolveEntry(path, {}, search ? `?${search}` : "");
    // Only honour a remembered location that is still a real address — an IA
    // change can strand one, and sending someone to a redirect on every visit
    // is worse than sending them somewhere fixed.
    if (!r.redirect) return lastLocation;
  }
  return pathFor(FALLBACK_ROOT.topic, FALLBACK_ROOT.leaf);
}

export interface Resolved {
  /** set when the path is not canonical and the caller should Navigate(replace) */
  redirect?: string;
  topic: TopicId;
  leafIndex: number;
  /** the detail segment, when the path carries one */
  id?: string;
  /** which section `?view=` selects; 0 for a bare leaf */
  viewIndex: number;
}

function isTopic(x: string | undefined): x is TopicId {
  return !!x && IA.some((t) => t.id === x);
}

/**
 * Turn any pathname into a canonical location, or a redirect to one.
 *
 * `remembered` is the per-topic sub-item memory. It is a ROUTING INPUT, not a
 * second store: it is consulted exactly once, to fill in a bare `/work`, and
 * after the redirect the path is complete and authoritative. Nothing reads it
 * again, so it cannot drift from the URL.
 *
 * Resolution is forgiving rather than 404-ing, matching the eight legacy
 * redirects the live app already ships — those exist because okuro URLs get
 * bookmarked, which is the evidence that a dead link should land somewhere.
 */
export function resolveEntry(
  pathname: string,
  remembered: Partial<Record<TopicId, number>> = {},
  search = "",
  lastLocation?: string | null,
): Resolved {
  // A live URL is answered before anything else, so a bookmark never falls
  // through to the forgiving unknown-path rule and lands somewhere generic.
  // `/work` and `/system` are NOT in that table: each is both a live path and a
  // redesign topic id, and the two readings are indistinguishable from the URL
  // alone, so they fall through to the bare-topic rule until that is ruled on.
  const legacy = legacyRedirect(pathname, search);
  if (legacy && legacy !== pathname) {
    // THE REQUEST'S QUERY SURVIVES THE REDIRECT. See `mergeSearch` for the rule
    // and for what it cost not to have it.
    const to = mergeSearch(legacy, search);
    // A target may carry a query (`/work/gantt` -> `/work/tasks?view=gantt`).
    // Recursing on the raw string would hand `tasks?view=gantt` to the leaf
    // matcher as a slug and silently miss, so the query is split off here and
    // passed as `search` — which is where the section is read from anyway.
    const qm = to.indexOf("?");
    const lPath = qm < 0 ? to : to.slice(0, qm);
    const lSearch = qm < 0 ? "" : to.slice(qm);
    return { ...resolveEntry(lPath, remembered, lSearch), redirect: to };
  }

  const [rawTopic, rawLeaf, rawId] = pathname.split("/").filter(Boolean);

  if (!isTopic(rawTopic)) {
    const to = resolveRoot(lastLocation);
    const r = resolveEntry(to);
    return { ...r, redirect: to };
  }

  const topic = rawTopic;
  const slugs = leafSlugs(topic);

  if (!rawLeaf) {
    // Bare topic: the memory's one and only job.
    const leaf = remembered[topic] ?? 0;
    const to = pathFor(topic, leaf);
    return { redirect: to, topic, leafIndex: slugs[leaf] ? leaf : 0, viewIndex: 0 };
  }

  const leafIndex = slugs.indexOf(rawLeaf);
  if (leafIndex < 0) {
    // Unknown leaf — forgiving, to the topic's first.
    const to = pathFor(topic, 0);
    return { redirect: to, topic, leafIndex: 0, viewIndex: 0 };
  }

  return {
    topic,
    leafIndex,
    id: rawId,
    // Resolved against THIS leaf's sections — `?view=gantt` means something
    // under `work/tasks` and nothing under `know/notes`, and the router is the
    // only place that knows which leaf is being addressed.
    viewIndex: viewIndexFrom(search, topic, slugs[leafIndex]),
  };
}

/**
 * THE LEGACY TABLE — every live URL, mapped to its redesign address.
 *
 * READ FROM `frontend/src/components/shell/nav-bar.tsx` NAV_TREE, not inferred
 * from leaf names, and that mattered: live **PODCAST is at `/media`** while
 * **MEDIA is at `/assets?view=media`**. Mapping `/media -> /deliver/media` from
 * the name alone would have sent every PODCAST bookmark to the wrong view, and
 * it would have looked right in review.
 *
 * These exist because okuro URLs get bookmarked — the live app already ships
 * eight redirects for exactly that reason, which is the evidence that a dead
 * link has to land somewhere rather than 404.
 *
 * `/work` and `/system` ARE HERE, and they are the table's two exceptions: each
 * is both a live path and a redesign topic id, so the bare-topic rule and the
 * bookmark disagree. The owner ruled the bookmark wins.
 */
export const LEGACY: Record<string, string> = {
  // START — live `/` is NOW, and resolveRoot already sends `/` to /start/now
  "/inbox": "/start/inbox",
  "/signals": "/start/inbox",
  "/reminders": "/start/inbox",
  "/todos": "/start/inbox",
  // KNOW
  "/brain": "/know/brain",
  "/knowledge": "/know/knowledge",
  "/notes": "/know/notes",
  "/cortex": "/know/cortex",
  "/repos": "/know/repos",
  // WORK — `/work` is the live TASKS list, and the bookmark meaning wins over
  // the bare-topic rule. Listed explicitly so the exception is VISIBLE rather
  // than emergent: it makes /work behave unlike /know and /deliver, and that is
  // an accepted trade — a bookmark landing somewhere else is a worse failure
  // than an inconsistency you can read in one line of this table.
  "/work": "/work/tasks",
  "/tasks": "/work/tasks",
  "/agents": "/work/agents",
  "/roles": "/work/agents",
  "/dashboard": "/work/agents",
  "/flow": "/work/flow",
  "/workflows": "/work/workflows",
  "/bridge": "/work/bridge",
  // DELIVER — note /media is PODCAST, not MEDIA
  "/prism": "/deliver/prism",
  "/slides": "/deliver/slides",
  "/studio": "/deliver/studio",
  "/media": "/deliver/podcast",
  "/assets": "/deliver/assets",
  "/resonance": "/deliver/resonance",
  "/people": "/deliver/people",
  // SYSTEM — same exception as /work: legacy `/system` redirected to HEALTH,
  // so a `/system` bookmark means health, not the topic's first leaf.
  "/system": "/system/health",
  "/services": "/system/services",
  "/health": "/system/health",
  "/scheduled": "/system/scheduled",
  "/models": "/system/models",
  "/stack": "/system/stacks",
  "/settings": "/system/settings",
  "/about": "/system/about",

  // HOMED 2026-09-14. These four had no Figma home and fell through to
  // `/start/now` via the forgiving unknown-path rule; the owner placed them, so
  // the bookmarks now land where the leaf actually lives.
  "/projects": "/work/projects",
  "/lessons": "/know/lessons",
  "/corpora": "/know/corpora",
  "/ds-engine-codex": "/system/design",

  // ADDED IN p2, AND IT IS `main`'S OWN DECISION EXPRESSED IN THIS GRAMMAR.
  // `/design-engine` is the OLD brand-authoring page. `main` deleted it in wave
  // 6 (`4e3cbe641`, "/design-engine ist geloescht, die Autorenflaeche ist EINE")
  // and left `<Route path="design-engine" element={<Navigate
  // to="/ds-engine-codex" replace />} />` in its place, with the reasoning that
  // this app already has a convention for a route whose FUNCTION moved. This
  // branch predates that commit and still carries the page, so the same
  // redirect is written here instead: `/design-engine` -> `/ds-engine-codex` ->
  // `/system/design`, collapsed to one hop. No leaf hosts the old page, and no
  // nav entry ever pointed at it. See the p2 report, Q6 — the branch is 26
  // commits behind `main` and that is the orchestrator's call, not this file's.
  "/design-engine": "/system/design",
};

/**
 * SECTION-SHAPED ORPHANS — a live path whose tail names a VIEW of a leaf rather
 * than an id under it. The `?view=` grammar is what made these expressible.
 *
 * A SEPARATE TABLE FROM `LEGACY`, and the separation is the point. LEGACY's
 * invariant is that every value is a canonical path, which a unit test asserts
 * by resolving each one. Putting `"/work/tasks?view=gantt"` in there breaks the
 * invariant silently: the value still type-checks as a string, but anything
 * treating it as a pathname splits `tasks?view=gantt` into a leaf slug that
 * matches nothing. The test caught exactly that. Two tables keep one honest
 * rule each — LEGACY holds paths, this holds paths plus a section.
 *
 * `/work/gantt` is the single judgement call here: gantt is a second way to
 * look at TASKS, so it maps onto TASKS rather than becoming a leaf of its own.
 * The other four are mechanical.
 *
 * THESE SELECT SECTION 0 UNTIL THEIR LEAF DECLARES THE MATCHING SECTION, which
 * is expected rather than broken and is now a LIVE mechanism rather than a
 * promise: `viewIndexFrom` resolves against the addressed leaf's own sections
 * (`views/sections.ts`), so the day TASKS declares a `gantt` section this table
 * starts selecting it with no change here. `work/tasks` declares RUNS and
 * SCHEDULES today, so `?view=gantt` still lands on RUNS — the right leaf,
 * section 0, exactly as designed.
 */
export const LEGACY_VIEWS: Record<string, string> = {
  "/work/gantt": "/work/tasks?view=gantt",
  "/prism/deck": "/deliver/prism?view=deck",
  "/settings/embed": "/system/settings?view=embed",
  "/settings/stt": "/system/settings?view=stt",
  "/settings/tts": "/system/settings?view=tts",
  // RULED BY THE OWNER 2026-09-14 (memory 0e3cfeaa): "redline lives at
  // /deliver/prism?view=redline&id=… — deep link preserved; DELIVER stays at
  // 8/8." The bare route is a section of PRISM; the document form is
  // `LEGACY_ID_VIEWS` below, because an id in the query would be the one thing
  // this grammar does not do.
  "/redline": "/deliver/prism?view=redline",
};

/**
 * A LIVE PATH WHOSE TAIL IS AN ID *AND* WHOSE DESTINATION IS A SECTION.
 *
 * `/redline/{document_id}` is the only member today, and it needs its own table
 * because it is the intersection of the two above: `LEGACY_PREFIXES` moves a
 * subtree but cannot add `?view=`, and `LEGACY_VIEWS` adds a section but matches
 * a whole path. Writing it into either one would break that table's single
 * honest rule, which is the same reason `LEGACY_VIEWS` was split out of
 * `LEGACY`.
 *
 * THE ID GOES IN THE PATH, and this is the one place p2 departs from the
 * ruling's literal spelling. The owner wrote `?view=redline&id=…`; the shell's
 * grammar is `/{topic}/{leaf}/{id}` with `?view=` as the ONLY thing that ever
 * lives in the query (memory 2b036d78, fact 3), and `pathFor` can only address
 * the path form — a second id spelling would make one of the two unreachable
 * from the shell's own navigation. The deep link the ruling protects survives
 * either way, so the grammar wins and the divergence is reported rather than
 * buried. Flagged in the p2 report.
 */
export const LEGACY_ID_VIEWS: Record<string, { to: string; view: string }> = {
  "/redline": { to: "/deliver/prism", view: "redline" },
};

/**
 * Prefixes whose SUBTREE moves with them, so a deep link keeps its tail:
 * `/repos/abc` -> `/know/repos/abc`. Only listed where the tail is genuinely an
 * id; a tail that is a named sub-view is the open section-vs-id question.
 */
export const LEGACY_PREFIXES: Record<string, string> = {
  "/repos": "/know/repos",
  "/slides": "/deliver/slides",
  "/tasks": "/work/tasks",
};

/**
 * THE QUERY SURVIVES A REDIRECT, AND IT IS ONE RULE IN ONE FUNCTION.
 *
 * ---------------------------------------------------------------------------
 * WHAT IT COST NOT TO HAVE THIS, measured in p3 against real ids on :3071.
 * The ids themselves are a customer's and stay out of this repo — they are in
 * the p3 artifacts; `<deck>` below is one of them.
 * ---------------------------------------------------------------------------
 *   /prism?id=<deck>                  -> /deliver/prism, search "" -> the
 *                                        DOCUMENT LIST, not the document
 *   /prism/deck?id=<deck>             -> the DEMO FIXTURE loaded, and the
 *                                        discriminator is exact: 102,408
 *                                        shadow characters for the fixture
 *                                        against 102,321 for the real deck.
 *                                        Worse than an error: it looks right.
 *   /flow?id=…, /workflows?id=…       -> the FLOW and WORKFLOWS galleries
 *                                        could not open a document at all,
 *                                        with 106 saved flows behind them
 *   /work?focus=role-refresh          -> AGENTS' cross-navigation under-delivers
 *
 * 20 call sites in 9 files across 6 leaves emit those addresses. Rewriting
 * every call site to spell the canonical address leaves the mechanism live for
 * the twenty-first — the instance-scope failure DP11 names. This is the class.
 *
 * ---------------------------------------------------------------------------
 * THE RULE, and the one parameter that is deliberately NOT carried:
 * ---------------------------------------------------------------------------
 * Every incoming query parameter is carried verbatim EXCEPT `view`.
 *
 * `?view=` is the shell's own section slot (memory 2b036d78, fact 3) and the
 * LEGACY table is the authority on which section a legacy address means. So on
 * a collision the TABLE WINS: `/work/gantt?view=runs` lands on
 * `/work/tasks?view=gantt`, not on RUNS, because `/work/gantt` IS the gantt
 * address and the bookmark's `view` is about the old address space.
 *
 * AND WHERE THE TABLE NAMES NO SECTION, THE INCOMING `view` IS DROPPED RATHER
 * THAN PASSED THROUGH. The one live path where `view` is a routing INPUT is
 * `/assets?view=media`, which `legacyRedirect` reads to pick MEDIA over
 * ASSETS — by the time the redirect is built that parameter has been CONSUMED.
 * Carrying it would put `/deliver/media?view=media` in someone's history, where
 * MEDIA's own slugs are all/image/audio/video, so `media` matches nothing and
 * resolves to section 0 anyway. Dropping it loses no information and cannot
 * select the wrong section; carrying it could.
 *
 * THE HASH IS NOT TOUCHED and is unchanged from before: `resolveEntry` never
 * receives one, so `<Navigate to>` drops the current fragment exactly as it did.
 */
export function mergeSearch(target: string, incoming = ""): string {
  if (!incoming || incoming === "?") return target;

  const qm = target.indexOf("?");
  const path = qm < 0 ? target : target.slice(0, qm);
  const own = new URLSearchParams(qm < 0 ? "" : target.slice(qm));

  // THE TARGET'S OWN PARAMETERS COME FIRST IN THE STRING, and that is about the
  // URL people read and paste rather than about semantics. `pathWithView` emits
  // `?view=<slug>` first, so a redirected address spelled the same way is
  // byte-identical to the one the shell's own navigation produces — one
  // spelling in the history, not two that mean the same thing.
  const merged = new URLSearchParams(own);
  // Which keys the table claimed. Tracked as a set rather than re-reading
  // `merged`, so a repeated incoming key (`?a=1&a=2`) still appends both.
  const claimed = new Set(own.keys());

  const inc = new URLSearchParams(incoming);
  // The section is never inherited from the request — see the rule above.
  inc.delete("view");
  for (const [k, v] of inc) if (!claimed.has(k)) merged.append(k, v);

  const q = merged.toString();
  return q ? `${path}?${q}` : path;
}

/**
 * Live `/assets?view=media` is the MEDIA leaf while bare `/assets` is ASSETS —
 * one path, two nav entries, distinguished only by the query. The resolver
 * therefore has to see the search string; resolving on pathname alone would
 * silently collapse the two.
 *
 * IT STILL RETURNS A BARE TARGET — the merge happens in `resolveEntry`, not
 * here, and that separation is deliberate. This function's single honest rule
 * is "a live path maps to a canonical address", which a unit test asserts by
 * resolving every value in the table. Merging the request's query in here would
 * make the return value depend on the caller's search string, and the table's
 * own invariant would no longer be testable in isolation.
 */
export function legacyRedirect(pathname: string, search = ""): string | null {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/assets" && new URLSearchParams(search).get("view") === "media") {
    return "/deliver/media";
  }
  if (LEGACY[path]) return LEGACY[path];
  if (LEGACY_VIEWS[path]) return LEGACY_VIEWS[path];

  // A live detail path whose destination is a SECTION of a leaf.
  for (const [from, { to, view }] of Object.entries(LEGACY_ID_VIEWS)) {
    if (path.startsWith(`${from}/`)) {
      const id = path.slice(from.length + 1);
      return id ? `${to}/${id}?view=${view}` : `${to}?view=${view}`;
    }
  }

  for (const [from, to] of Object.entries(LEGACY_PREFIXES)) {
    if (path.startsWith(`${from}/`)) return to + path.slice(from.length);
  }

  // ---------------------------------------------------------------------------
  // LIVE `/work/{id}` — THE ONE LEGACY SHAPE THAT CANNOT BE A PREFIX ENTRY.
  // ---------------------------------------------------------------------------
  // The live app routes `/work/:id` to the task detail (`app.tsx:387` before
  // p2), so `/work/abc123` is a real bookmark. Putting `"/work": "/work/tasks"`
  // in LEGACY_PREFIXES would catch it — and would also catch every canonical
  // address under WORK, turning `/work/tasks` into `/work/tasks/tasks` and
  // `/work/agents` into `/work/tasks/agents`. The prefix loop tests
  // `startsWith("/work/")` and cannot tell the two apart, so the rule has to
  // know the leaf slugs.
  //
  // IT FIRES ONLY ON A TWO-SEGMENT PATH WHOSE SECOND SEGMENT IS NOT A LEAF, and
  // that is exactly the live grammar: WORK's live paths are `/work`,
  // `/work/gantt` (caught by LEGACY_VIEWS above) and `/work/{id}`. A mistyped
  // `/work/task` therefore resolves to a task detail for the id "task" rather
  // than to the topic's first leaf — which is what the live app does with the
  // same URL today, so the forgiving behaviour is preserved rather than traded.
  const seg = path.split("/").filter(Boolean);
  if (seg.length === 2 && seg[0] === "work" && !leafSlugs("work").includes(seg[1]!)) {
    const tasks = leafSlugs("work").indexOf("tasks");
    if (tasks >= 0) return pathFor("work", tasks, seg[1]!);
  }

  return null;
}

/**
 * Live paths with NO home in the redesign IA. EMPTY as of 2026-09-14 — the
 * last four (PROJECTS, LESSONS, CORPORA, DESIGN) were placed by the owner and now
 * redirect through LEGACY like every other live path.
 *
 * THE EXPORT STAYS, and deliberately. It is the place a future unplaced leaf
 * gets named instead of being swallowed by the forgiving unknown-path rule —
 * which is exactly what it caught the first time. An empty list is the correct
 * reading of "nothing is unplaced"; deleting it would mean the next gap has
 * nowhere to be recorded and would surface as a bookmark quietly landing on
 * `/start/now`.
 */
export const UNHOMED: readonly string[] = [
  // p2, 2026-09-14. Each of these renders a real page today and has no leaf, so
  // each is named here rather than swallowed by the forgiving unknown-path rule.
  // They stay reachable at the legacy paths below because `app.tsx` keeps them
  // as routes outside the shell; none of them is a navigable leaf.
  "/q/:token", // public, unauthenticated, outside OnboardingGate too
  "/onboarding", // the gate's own destination — it precedes the shell
  "/inline/:sessionId", // per-session bearer via ?t=, deliberately chrome-free
  "/dev/crash", // DEV-only ErrorBoundary fixture
  "/prism-gallery", // no ruling: "I don't know. Prism is work in progress"
  "/sparring", // no ruling, no proposal — p3 analysis proposes one
];

/**
 * THE REVERSE OF `LEGACY` — which live paths mean this canonical address.
 *
 * WHY IT EXISTS, and it is a defect this file was one line away from shipping.
 * Everything in okuro that gates or classifies a ROUTE was written against the
 * live address space. The clearest case is the feature switch:
 * `src/okuro/features.py:245` declares `routes=("/lessons",)` — the only
 * `routes=` declaration in the whole registry — and its feature ships
 * `default=False`, so `/lessons` is withheld on a stock install. Under the shell
 * the pathname is `/know/lessons`, and `isRouteWithheld("/know/lessons",
 * ["/lessons"])` is FALSE. The gate would have passed everything, silently, with
 * no error anywhere and nothing in a screenshot to see.
 *
 * SO THE GATE IS GIVEN BOTH SPELLINGS RATHER THAN THE DECLARATION BEING MOVED.
 * Editing `features.py` to say `/know/lessons` would break the CLI and the MCP
 * seams, which share that declaration and have no shell; and it would have to be
 * redone at every future IA change. Inverting the table the redirects already
 * live in costs nothing and keeps one source of truth.
 *
 * Built from `LEGACY` only. `LEGACY_VIEWS` values carry a query and a feature
 * prefix is a path; `LEGACY_PREFIXES` is covered because its two `from` values
 * are already `LEGACY` keys.
 */
const LEGACY_BY_TARGET: Map<string, string[]> = (() => {
  const m = new Map<string, string[]>();
  for (const [from, to] of Object.entries(LEGACY)) {
    const list = m.get(to);
    if (list) list.push(from);
    else m.set(to, [from]);
  }
  return m;
})();

/**
 * Every path spelling that addresses this leaf — the canonical one first, then
 * the live ones. What a route-keyed gate has to test against.
 */
export function routeAliases(topic: TopicId | string, leaf: string): string[] {
  const canonical = `/${topic}/${leaf}`;
  return [canonical, ...(LEGACY_BY_TARGET.get(canonical) ?? [])];
}

/**
 * SECTIONS LIVE IN THE QUERY, NOT THE PATH. The owner ruled `?view=`:
 * `/know/cortex?view=atom`.
 *
 * The precedent was already in the codebase — live `/assets?view=media` — and
 * `resolveEntry` already took `search` to tell that pair apart, so the mechanism
 * existed rather than being invented for this.
 *
 * WHY IT MATTERS STRUCTURALLY: sections never enter the path, so the grammar
 * stays two segments plus an optional id tail and there is no collision between
 * a section slug and a detail id one level down. That is what unblocked the
 * section-shaped orphans — `/work/gantt`, `/prism/deck`, `/settings/embed|stt|tts`
 * are all views of a leaf, not ids under one.
 *
 * A BARE LEAF IS VALID AND CANONICAL. `/know/cortex` with no `?view=` lands on
 * the leaf's first section and does NOT redirect to add one. The query is
 * optional; a link without it must never 404 or land nowhere. Two spellings for
 * one state is fine — both derive from the URL, so there is still one store.
 *
 * SECTIONS ARE PER LEAF, and that is Phase 4's one change to this file. They
 * were a single global list while every pane carried the same placeholder body;
 * a real view declares its own (`views/sections.ts`), so `?view=` has to be read
 * against the leaf being addressed or `runs` under WORK and `runs` under DELIVER
 * would be forced to mean the same index. The placeholder set is still what a
 * leaf with no declaration gets, so an un-ported leaf behaves exactly as before.
 */
export { PLACEHOLDER_SECTIONS as SECTIONS } from "./views/sections";
export {
  SECTION_ALIASES,
  canonicalSectionSlug,
  sectionSlugs,
  sectionsFor,
} from "./views/sections";

/** The placeholder set's slugs. Kept for a caller with no leaf in hand. */
export const SECTION_SLUGS: string[] = PLACEHOLDER_SECTIONS.map((s) => s.toLowerCase());

/**
 * Which section a URL selects. Unknown or absent -> the first.
 *
 * `topic`/`leaf` are OPTIONAL, and their absence means "resolve against the
 * placeholder set" rather than "fail". A caller that has only a query string —
 * a test, a link-checker — still gets an answer, and a caller that knows the
 * leaf gets the right one.
 */
export function viewIndexFrom(search: string, topic?: TopicId | string, leaf?: string): number {
  const params = new URLSearchParams(search);
  // `?tab=` IS READ AS AN ALIAS OF `?view=` — F6, and READ ONLY.
  //
  // okuro has two section grammars live at once. `?tab=` is the app's: 31
  // sub-views are URL-bound on it through `hooks/use-url-tab.ts`. `?view=` is
  // the shell's, declared per leaf in `views/sections.ts`. Before this line the
  // shell simply did not understand the app's spelling, so on KNOW/BRAIN
  // `?tab=memory` worked — the page reads it — while the shell resolved section
  // 0 and the two disagreed about where you were on the same URL.
  //
  // `view` WINS WHEN BOTH ARE PRESENT, because after F1 a legacy redirect can
  // carry a `tab` alongside the table's own `view`: `/work/gantt?tab=runs`
  // becomes `/work/tasks?view=gantt&tab=runs`, and the address the table chose
  // has to outrank a parameter the request happened to bring.
  //
  // THE WRITERS ARE NOT TOUCHED. `use-url-tab.ts` still writes `?tab=` and the
  // leaf passes convert it; this is the compatibility shim WORK's P5 recommended
  // ("C then A") so each leaf pass stays independent and reversible.
  //
  // CHECKED FOR COLLISIONS BEFORE SHIPPING, because a wrong alias would
  // MIS-DISPATCH a mount rather than merely mis-label a section. Only three
  // leaves have a mount that dispatches on this index, and their slugs are
  // disjoint from the pages' tab vocabularies: `work/tasks` (runs, gantt) and
  // `deliver/prism` (documents, deck, redline) have no `?tab=` at all, and
  // `system/settings` (general, embed, stt, tts) has 15 tab values of which
  // none is one of those four — so `?tab=feedback` still lands on the main
  // settings page, which is what the global feedback button depends on.
  const raw = params.get("view") ?? params.get("tab");
  if (!raw) return 0;
  const slugs =
    topic && leaf ? sectionSlugs(topic, leaf) : SECTION_SLUGS;
  // A SLUG THE LEAF USED TO ANSWER TO RESOLVES TO THE SECTION IT MEANS NOW.
  // `canonicalSectionSlug` is the identity for every leaf that has no alias,
  // so this line costs nothing where nothing was renamed. It exists because an
  // unknown slug resolves to 0, which means a stale bookmark would land on the
  // default and look like a correct page rather than a lost one — see
  // `SECTION_ALIASES` for the case that earned it.
  const wanted = topic && leaf
    ? canonicalSectionSlug(topic, leaf, raw.toLowerCase())
    : raw.toLowerCase();
  const i = slugs.indexOf(wanted);
  return i < 0 ? 0 : i;
}

/**
 * The canonical address for a leaf plus one of its sections.
 *
 * `search` IS THE SAME DEFECT AS THE REDIRECTS' AND THE SAME RULE FIXES IT.
 * This function built the address from scratch, so from
 * `/deliver/prism?id=<doc>` clicking the DECK section produced
 * `/deliver/prism?view=deck` and LOST the open document — measured in p3 and
 * recorded as the second symptom of memory 7424570b's root cause. The section
 * is the one parameter this function owns, so `mergeSearch` carrying everything
 * but `view` is exactly the right rule: the caller's `id`, `focus`, `tab` and
 * `q` survive a section click, and the section itself comes from here.
 *
 * Omitting `search` keeps the old behaviour, which is what every test that
 * asserts a bare address relies on.
 */
export function pathWithView(
  topic: TopicId,
  leafIndex: number,
  viewIndex: number,
  search = "",
): string {
  const base = pathFor(topic, leafIndex);
  // Section 0 is the bare leaf's meaning, so it needs no query. Emitting
  // `?view=atom` for it would make two URLs where the shorter one already says
  // the same thing, and the shorter one is what people paste.
  if (viewIndex === 0) return mergeSearch(base, search);
  const slugs = leafSlugs(topic);
  // Every topic in the IA has at least one leaf, so `slugs[0]` is present; the
  // `?? ""` only satisfies `noUncheckedIndexedAccess` and resolves to the
  // placeholder section set if the IA is ever emptied, which `sectionsFor`
  // already handles.
  const slug = sectionSlugs(topic, slugs[leafIndex] ?? slugs[0] ?? "")[viewIndex];
  // A section index with no slug cannot be addressed, so the bare leaf is the
  // honest answer — never `?view=undefined`, which resolves to 0 anyway but
  // leaves an unreadable URL in someone's history.
  return slug ? mergeSearch(`${base}?view=${slug}`, search) : mergeSearch(base, search);
}

/** Step the topic rail by one, as a path. Clamped, never wrapped. */
export function stepPath(
  current: TopicId,
  delta: number,
  remembered: Partial<Record<TopicId, number>> = {},
): string | null {
  const next = IA[topicIndex(current) + delta];
  if (!next) return null;
  return pathFor(next.id, remembered[next.id] ?? 0);
}

/**
 * WHETHER A NAVIGATION ANIMATES, and this is the taxonomy Law 3 dictates.
 *
 * A cold arrival does NOT animate. Direction follows the physical position OF A
 * MOVEMENT, and on a cold arrival nothing moved — no outgoing bar, no previous
 * position, no journey. Animating one invents a journey the user did not take,
 * which is the exact failure Law 3 was written against. It is also what Smart
 * Animate does with a layer that has no match in the outgoing frame, and a cold
 * arrival has no outgoing frame at all.
 *
 *   first paint / reload / paste / bookmark   no `from`   -> no animation
 *   in-app click                              has `from`  -> physical position
 *   back / forward                            has `from`  -> physical position
 *   redirect (/work -> /work/agents)          a resolution -> no animation
 *
 * BACK AND FORWARD ARE THE INTERESTING ROW. "Back" does not mean "reverse the
 * previous animation" — it means "go to that position", and the direction is
 * computed fresh from the two positions like any other move. Back from
 * `/system/health` to `/work/agents` travels LEFT, because SYSTEM sits right of
 * WORK, regardless of how you arrived. The rail is a physical row; direction is
 * a fact about the row, not about history. The alternative would make one URL
 * animate differently depending on the route taken to reach it, which is
 * precisely the history-dependence Law 3 forbids.
 *
 * REPLACE is how a redirect reaches us, so it is the one navigation type that
 * suppresses motion — it is a resolution of an incomplete address, not a move.
 */
export function shouldAnimate(
  previous: TopicId | null,
  navigationType: "POP" | "PUSH" | "REPLACE",
): boolean {
  if (previous === null) return false;
  return navigationType !== "REPLACE";
}
