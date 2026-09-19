// SPDX-License-Identifier: Apache-2.0
/**
 * THE CHANNEL FROM A LEAF INTO THE PLATE.
 *
 * ===========================================================================
 * THE CLASS, AND IT IS NOT "THIS PAGE'S HEADER IS IN THE WRONG PLACE"
 * ===========================================================================
 * the owner, 2026-09-17: *"the page Titles are not in the glass part. They are
 * underneath it. Section titles need to be on top. so as the detail titles"*.
 *
 * `TitleBar.tsx` shipped the two plate components and said the right-hand slot
 * was *"honest rather than unfinished: handing them over is per-leaf work"*.
 * That was true and it was also the defect: there was NO WAY to hand them over.
 * The plate is rendered by `TopicBar` ABOVE `.c-panes`; every leaf renders
 * INSIDE `.c-panes`. Without a channel between the two, a leaf's only option is
 * to draw its own header, and that header lands under the plate — in EVERY
 * leaf, not in one. So the fix is the channel, not a page.
 *
 * ===========================================================================
 * THE SAME PATTERN THIS SHELL ALREADY USES TWICE
 * ===========================================================================
 * `CornerActionsProvider` and `PulseDataProvider` both let a descendant reach a
 * piece of chrome that is not its ancestor, and both treat an absent provider as
 * the idle default rather than an error. This is the third instance of that
 * pattern and it keeps their two contracts:
 *
 *   ABSENT PROVIDER IS NOT AN ERROR — `?embed=1`, `/onboarding`, `/q/:token`
 *   and every unit test render leaves with no provider above them. They publish
 *   into a no-op and render exactly as they did before this file existed.
 *
 *   ONE PROVIDER PER BAR, not one per app. Law 3 keeps five panes mounted, so a
 *   single app-wide slot would let the four INACTIVE leaves fight the visible
 *   one for the plate. `TopicBar` mounts this around its own band and its own
 *   panes, so a bar's leaves can only ever reach a bar's own plate.
 *
 * ===========================================================================
 * WHY THE LEAF OWNS THE MEMO, AND WHY THAT IS THE HONEST SPLIT
 * ===========================================================================
 * `actions` is JSX. JSX is a new object on every render, so a hook that
 * published it unconditionally would set state on every render and never
 * settle. The alternative — this file guessing at a dependency list — would be
 * guessing about state it cannot see.
 *
 * So the leaf passes a `useMemo`'d slot and this hook keys on that object's
 * identity. The leaf already knows which of its values the header depends on;
 * that is the only place the question has an answer.
 */

import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
import { createPortal } from "react-dom";
import { SectionTitle, DetailTitle } from "./TitleBar";

/**
 * THE ID OF ONE TOPIC'S SLOT IN THE PLATE'S TRACK.
 *
 * ONE FUNCTION, TWO CALLERS, SO THE STRING CANNOT DRIFT. `App` renders the five
 * slots and reads them back to move them; this file portals into them. Spelling
 * the template literal twice is exactly the class of defect the shell keeps
 * finding in its own comments, one layer down.
 */
export const slotId = (topic: string) => `top-slot-${topic}`;

/** `[SECTION INTRO]` on the left, `[ SEARCH | SORT | FILTER | ADD/DELETE ]` right. */
export interface SectionSlot {
  /** Overrides the shell's derived leaf title. Omit to keep it. */
  title?: ReactNode;
  actions?: ReactNode;
}

/** `← [DETAIL TITLE] [STATUS]` on the left, `[Details] [Actions]` right. */
export interface DetailSlot {
  title: ReactNode;
  status?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  onBack(): void;
}

/**
 * A PUBLISHED SLOT CARRIES WHO PUBLISHED IT, and that one field closes two
 * defects at once.
 *
 * WITHOUT IT, DEFECT ONE: the provider's slot is single and `useSectionTitle`'s
 * cleanup used to clear it unconditionally. `useWindowedPanes` retires the
 * outgoing pane `motionDurationMs + 60` AFTER the switch (`useSlide.ts:81`), so
 * the LEAVING leaf's cleanup ran last and wiped the slot the ARRIVED leaf had
 * already filled. Measured: the plate lost the arrived leaf's title and
 * controls +1148ms after START -> /work/tasks and +986ms after tasks -> bridge,
 * and never got them back.
 *
 * WITHOUT IT, DEFECT TWO: for the ~300ms between the commit and the incoming
 * leaf's own publish, the band showed the NEW fallback title beside the OLD
 * leaf's actions row — one band describing two different leaves.
 *
 * `owner` is the bar's leaf index (`sub` in `TopicBar`), which is what
 * distinguishes two leaves of the same bar. It is captured WHEN A LEAF
 * PUBLISHES, so it answers "whose row is this", and `PlateTitle` renders a
 * published slot only while its owner is still the bar's current leaf.
 */
type LeafOwner = string | number;

interface Published<T> {
  slot: T;
  owner: LeafOwner;
}

interface PageTitleApi {
  section: Published<SectionSlot> | null;
  detail: Published<DetailSlot> | null;
  /** The bar's CURRENT leaf. A published slot from any other leaf is stale. */
  owner: LeafOwner;
  setSection: Dispatch<SetStateAction<Published<SectionSlot> | null>>;
  setDetail: Dispatch<SetStateAction<Published<DetailSlot> | null>>;
}

const NOOP: PageTitleApi = {
  section: null,
  detail: null,
  owner: "",
  setSection: () => {},
  setDetail: () => {},
};

const PageTitleContext = createContext<PageTitleApi>(NOOP);

export function PageTitleProvider({
  owner,
  children,
}: {
  /** The bar's current leaf index — see `Published` above. */
  owner: LeafOwner;
  children: ReactNode;
}) {
  const [section, setSection] = useState<Published<SectionSlot> | null>(null);
  const [detail, setDetail] = useState<Published<DetailSlot> | null>(null);
  const value = useMemo<PageTitleApi>(
    () => ({ section, detail, owner, setSection, setDetail }),
    [section, detail, owner],
  );
  return <PageTitleContext.Provider value={value}>{children}</PageTitleContext.Provider>;
}

/**
 * Publish a leaf's header into its bar's plate. `slot` MUST be memoised by the
 * caller — see the note at the top of this file for why that decision cannot
 * live here.
 */
export function useSectionTitle(slot: SectionSlot): void {
  const { setSection, owner } = useContext(PageTitleContext);
  /* THE OWNER IS CAPTURED AT MOUNT, IN A REF, AND A DEPENDENCY LIST CANNOT DO
     THIS JOB.
     =======================================================================
     An earlier version read `owner` from the closure and left it out of the
     dependency list, with a comment claiming that this kept a retiring leaf
     from being re-stamped. THAT CLAIM WAS FALSE, and a reviewer proved it in
     this repo's own harness. Omitting a value stops the effect re-running
     BECAUSE THAT VALUE CHANGED; it does nothing about the effect re-running
     for another reason and then reading the LATEST render's closure.
     So a leaf whose `slot` memo changes while it is retiring — inside the
     `motionDurationMs + 60` window — re-published with the INCOMING leaf's
     owner. Both stamps then agreed and the browser gate could not see it:

       AFTER THE BAR MOVED ON: headingText "Leaf 1", headingOwner "1",
                               rowOwner "1", rowText "leaf-0-controls-refetched"

     Reachable in the product, not a thought experiment: five section headers
     memo on async server state that flips well inside 710ms — models.tsx,
     media.tsx, studio.tsx, repos.tsx and corpora.tsx all key on an
     `isFetching`-shaped flag. `tasks.tsx` keys on `[search, filter]`, local UI
     state, which is why no sweep hit this by accident.

     A REF IS CORRECT AT CAPTURE because a pane's first appearance is always as
     its bar's CURRENT leaf: `useWindowedPanes` only ever adds `outgoing` for an
     index that was previously `index`. So the value read at mount is this
     leaf's own identity, and it stays that whatever the bar does next — which
     is precisely what "who published this row" has to mean. */
  const mine = useRef(owner);
  useEffect(() => {
    setSection({ slot, owner: mine.current });
    /* CLEARED BY IDENTITY, NOT UNCONDITIONALLY, and that is the other half.
       This used to be `setSection(null)`. Two panes are mounted while one
       travels and the OUTGOING one unmounts LAST, so an unconditional clear
       made the leaving leaf erase the arriving leaf's row. Clearing only when
       the slot still in the provider is THIS leaf's makes the order stop
       mattering: a late cleanup from a retired pane is a no-op. */
    return () => setSection((cur) => (cur && cur.slot === slot ? null : cur));
  }, [slot, setSection]);
}

/** Same contract, for one record inside a leaf. */
export function useDetailTitle(slot: DetailSlot | null): void {
  const { setDetail, owner } = useContext(PageTitleContext);
  // Same mount-captured ref and same identity clear as `useSectionTitle`, for
  // the same two reasons — see the long note there.
  const mine = useRef(owner);
  useEffect(() => {
    setDetail(slot ? { slot, owner: mine.current } : null);
    return () => setDetail((cur) => (cur && cur.slot === slot ? null : cur));
  }, [slot, setDetail]);
}

/**
 * ONE BAR'S TITLE, rendered into that bar's own slot in the plate's track.
 *
 * DETAIL WINS OVER SECTION when both are set, because that is what the two
 * sketches describe: a detail view is reached FROM a list, so the list is still
 * mounted underneath it and both slots are legitimately full at once.
 *
 * `fallback` is the shell's own derived leaf title — R1's rule that the title
 * belongs to the shell. A leaf overrides it only to say something the shell
 * cannot know, which is what the owner's *"YOUR RECENT WORK"* is next to the
 * shell's own *"Tasks"*.
 */
export function PlateTitle({ fallback, topic }: { fallback: ReactNode; topic: string }) {
  const { section, detail, owner } = useContext(PageTitleContext);

  /* ===================================================================
     IT RENDERS INTO THE PLATE, AND THAT IS A STACKING FACT RATHER THAN
     A TIDINESS ONE.
     ===================================================================
     Measured 2026-09-17: the band sat in `.c-inner` with `z-index: 2` and
     did not paint at all — zero bright pixels where the title's own
     `getBoundingClientRect` said it was, while `elementFromPoint` still
     named it. The chain explains both: `.content` carries a transform for
     the pane slide, so it is a STACKING CONTEXT at `z-index: auto`. The
     band's 2 is therefore local to `.content`, and `.top-plate` — a
     `z-index: 1` sibling of `.plane` — outranks every `auto` element in
     it, title included. Hit-testing walked the DOM and disagreed with the
     paint, which is why it looked fine to every measurement but a pixel.

     RAISING THE BAND CANNOT WORK. Give `.content` a rank above the plate
     and the PAGE rises with it, because `.c-panes` is inside the same
     context — the plate would sit behind the content it exists to blur.
     The plate has to be BETWEEN them, and they are one subtree.

     SO THE TITLE MOVES TO THE PLATE, which is also what the design says:
     in Figma the title (`812:8587`) is a CHILD of the plate (`812:8585`),
     not a sibling of the page. A portal moves the DOM node without moving
     the React tree, so the leaf's context, state and handlers are
     untouched and `useSectionTitle` still talks to its own bar.

     ===================================================================
     EVERY BAR PORTALS NOW, EACH INTO ITS OWN SLOT — 2026-09-17.
     ===================================================================
     This used to return `null` unless the bar was active, so exactly ONE
     title existed and a topic switch could only swap it: measured per
     frame, the text changed in a single commit while the page slid for
     650ms underneath it. The owner ruled the title surface to be five
     titles in a sliding track, so all five mount, each into the slot
     `App` renders for it, and `useTopicSwitch` moves the active pair the
     way it already moves the content.

     THE STACK CANNOT COLLIDE, which was the whole reason for the old
     guard. The slots are five absolutely positioned boxes in ONE cell and
     four of them are parked a full column out and transparent — see
     `.c-slot` in the stylesheet. Activity is now the SLOT's attribute
     rather than this component's prop.

     `slot` IS STATE, NOT A REF READ AT RENDER, because the plate is
     rendered by `App` as a sibling of `.plane` and may not exist on this
     component's first pass. A LAYOUT effect rather than a passive one: it
     runs before paint, so the title lands in the first painted frame
     instead of the second, and the slots are certainly in the DOM by then
     because `App` commits the plate and the bars together. */
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => {
    setSlot(document.getElementById(slotId(topic)));
  }, [topic]);

  if (!slot) return null;

  /* ONLY THE CURRENT LEAF'S ROW IS RENDERED, and this is the second half of the
     owner fix. Between a leaf commit and the incoming leaf's own publish there
     is a window — measured at about 300ms — where the provider still holds the
     OUTGOING leaf's slot while the fallback title has already become the new
     leaf's. Rendering that produced one band describing two leaves: the new
     name beside the old controls. Comparing the published owner against the
     bar's current one makes the band show the fallback alone for those frames,
     which is honest, and then the arriving leaf's row. */
  const liveSection = section && section.owner === owner ? section.slot : null;
  const liveDetail = detail && detail.owner === owner ? detail.slot : null;

  const body = liveDetail ? (
    <DetailTitle {...liveDetail} />
  ) : (
    <SectionTitle
      title={liveSection?.title ?? fallback}
      actions={liveSection?.actions}
      /* THE GATE'S HANDLE, AND IT IS TWO VALUES ON PURPOSE. The heading carries
         the bar's CURRENT leaf; the row carries the leaf that PUBLISHED it,
         read straight off `section` rather than off `owner`. One value on both
         elements could never disagree, which would make the browser assertion
         that they match prove nothing. Two sources make the defect — a row from
         one leaf under another leaf's heading — visible. */
      owner={owner}
      actionsOwner={section?.owner}
    />
  );
  return createPortal(body, slot);
}
