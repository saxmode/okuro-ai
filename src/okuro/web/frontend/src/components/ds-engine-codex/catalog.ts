export type ShowcaseGroup = "forms" | "overlays" | "navigation" | "feedback" | "data";

const GROUP_LABELS: { id: ShowcaseGroup; label: string; caption: string }[] = [
  { id: "forms", label: "Forms and input", caption: "Components that take or refine a value." },
  { id: "overlays", label: "Overlays", caption: "Components that leave the normal document flow." },
  { id: "navigation", label: "Navigation and structure", caption: "Components that divide and orient a page." },
  { id: "feedback", label: "Feedback and status", caption: "Components that communicate state and progress." },
  { id: "data", label: "Data display", caption: "Components that structure and present content." },
];

const INVENTORY: { file: string; group: ShowcaseGroup }[] = [
  ...["button", "input", "textarea", "label", "select", "switch", "segmented", "form-primitives", "facet-bar"].map((file) => ({ file, group: "forms" as const })),
  ...["dialog", "sheet", "side-panel", "detail-modal", "dropdown-menu", "context-menu", "tooltip", "command-palette"].map((file) => ({ file, group: "overlays" as const })),
  ...["tabs", "scroll-area", "separator", "section-label", "row"].map((file) => ({ file, group: "navigation" as const })),
  ...["toast", "progress", "badge", "status-badge", "empty-state", "loading-skeleton", "timeline"].map((file) => ({ file, group: "feedback" as const })),
  ...["card", "metric-card", "markdown-content", "markdown-render"].map((file) => ({ file, group: "data" as const })),
];

export interface ComponentDoc {
  id: string;
  name: string;
  group: ShowcaseGroup;
  purpose: string;
  configuration: string[];
  behavior: string;
  accessibility: string;
}

const DETAILS: Record<
  string,
  Omit<ComponentDoc, "id" | "name" | "group">
> = {
  button: { purpose: "Starts an immediate action or confirms a decision.", configuration: ["variant", "size", "disabled", "loading", "prefix / suffix"], behavior: "Use one primary action per decision area; preserve the label while loading.", accessibility: "Native button semantics, visible focus, and an accessible name are required." },
  input: { purpose: "Collects a single line of text or structured data.", configuration: ["type", "value", "placeholder", "invalid", "disabled"], behavior: "Keep validation next to the field and preserve entered values after refusal.", accessibility: "Pair with a visible label and connect errors through aria-describedby." },
  textarea: { purpose: "Collects longer, multi-line writing.", configuration: ["rows", "value", "placeholder", "invalid", "disabled"], behavior: "Allow vertical growth when the expected answer is not fixed.", accessibility: "Provide a visible label and announce limits before they are reached." },
  label: { purpose: "Names a control and expands its usable target.", configuration: ["htmlFor", "required", "hint"], behavior: "Write labels as stable nouns; put instructions in supporting copy.", accessibility: "Programmatically associate the label with exactly one control." },
  select: { purpose: "Chooses one value from a bounded set.", configuration: ["value", "groups", "disabled", "placeholder", "portal container"], behavior: "Prefer visible choices when there are fewer than five predictable options.", accessibility: "Keyboard traversal, selected state, and focus return are built into the primitive." },
  switch: { purpose: "Changes one setting immediately between on and off.", configuration: ["checked", "disabled", "label"], behavior: "Do not use for actions that require a separate Save step.", accessibility: "The label describes the setting, not the action taken on click." },
  segmented: { purpose: "Switches between a small set of peer views or modes.", configuration: ["value", "options", "size", "aria label"], behavior: "Keep labels short and choices mutually exclusive.", accessibility: "Expose the group name and the selected option." },
  "form-primitives": { purpose: "Provides the shared anatomy for coherent forms.", configuration: ["label", "description", "error", "required", "layout"], behavior: "Keep label, input, help, and refusal in one predictable vertical rhythm.", accessibility: "Descriptions and errors must be addressable by their control." },
  "facet-bar": { purpose: "Refines a result set through visible, removable filters.", configuration: ["facets", "counts", "active values", "clear"], behavior: "Show applied filters and their effect without hiding them in a modal.", accessibility: "Each facet announces selection state and result-count changes." },
  dialog: { purpose: "Interrupts the page for a short, consequential decision.", configuration: ["open", "title", "description", "dismissal", "portal container"], behavior: "Keep the task focused and return focus to the opener on close.", accessibility: "Trap focus, provide a title, support Escape, and restore focus." },
  sheet: { purpose: "Keeps associated context open beside the current page.", configuration: ["open", "side", "width", "dismissal", "portal container"], behavior: "The underlying page stays legible and useful while the sheet is open.", accessibility: "Provide an explicit close control, Escape, and focus return." },
  "side-panel": { purpose: "Hosts persistent detail or editing in a bounded side region.", configuration: ["open", "title", "width", "mobile trigger"], behavior: "Use when users compare panel content with the page beneath it.", accessibility: "Preserve reading order and expose panel state to the trigger." },
  "detail-modal": { purpose: "Shows structured record detail without losing list context.", configuration: ["title", "sections", "fields", "actions"], behavior: "Lead with identity and status, then group related facts.", accessibility: "The modal requires a unique heading and deterministic focus order." },
  "dropdown-menu": { purpose: "Collects secondary actions behind one trigger.", configuration: ["items", "groups", "submenus", "shortcuts", "portal container"], behavior: "Keep frequent or primary actions visible outside the menu.", accessibility: "Arrow keys traverse; Escape closes; disabled items remain perceivable." },
  "context-menu": { purpose: "Offers actions for a specific object at pointer position.", configuration: ["items", "groups", "checks", "submenus", "portal container"], behavior: "Never make context-menu actions the only path to a capability.", accessibility: "Provide an equivalent visible or keyboard-accessible action path." },
  tooltip: { purpose: "Clarifies an unfamiliar control without adding permanent copy.", configuration: ["content", "side", "delay", "portal container"], behavior: "Use for clarification, never for required instructions or errors.", accessibility: "Content appears for keyboard focus and pointer hover." },
  "command-palette": { purpose: "Finds and runs actions across the product.", configuration: ["commands", "groups", "query", "shortcut", "portal container"], behavior: "Rank by intent and recency; keep destructive actions explicit.", accessibility: "Announce result count, active option, shortcuts, and empty results." },
  tabs: { purpose: "Switches peer panels inside a stable local context.", configuration: ["value", "orientation", "activation", "disabled"], behavior: "Do not use when users must compare panels or understand a sequence.", accessibility: "Arrow-key navigation and tab-to-panel relationships are required." },
  "scroll-area": { purpose: "Constrains long content while retaining themed scroll affordance.", configuration: ["height", "orientation", "scrollbar"], behavior: "Avoid nested scrolling when the page can own the flow.", accessibility: "Keyboard and platform scrolling behavior must remain intact." },
  separator: { purpose: "Marks a semantic boundary between adjacent regions.", configuration: ["orientation", "decorative", "role"], behavior: "Spacing carries hierarchy first; separators clarify a real boundary.", accessibility: "Decorative separators stay out of the accessibility tree." },
  "section-label": { purpose: "Names a compact region in the application hierarchy.", configuration: ["label", "action", "tone"], behavior: "Use sentence case and keep the region name stable.", accessibility: "Connect it to the section it names when it acts as a heading." },
  row: { purpose: "Aligns identity, metadata, status, and actions for one record.", configuration: ["leading", "title", "meta", "trailing", "selected"], behavior: "Keep scanning anchors fixed across neighboring rows.", accessibility: "Interactive rows need one clear primary target and named secondary actions." },
  toast: { purpose: "Confirms a completed background or user-triggered event.", configuration: ["tone", "title", "description", "action", "duration"], behavior: "Never use a toast as the only record of a failure requiring action.", accessibility: "Use a suitable live-region priority without stealing focus." },
  progress: { purpose: "Shows determinate completion for a continuing task.", configuration: ["value", "max", "label", "tone"], behavior: "Use indeterminate treatment when completion cannot be measured.", accessibility: "Expose the current value and a task-specific accessible label." },
  badge: { purpose: "Adds compact classification or count metadata.", configuration: ["variant", "content", "icon"], behavior: "Badges annotate; they do not replace primary labels or buttons.", accessibility: "Do not encode meaning through color alone." },
  "status-badge": { purpose: "Communicates a system or workflow state consistently.", configuration: ["status", "label", "detail"], behavior: "Use the canonical status vocabulary and avoid synonyms.", accessibility: "Pair signal color with explicit state text." },
  "empty-state": { purpose: "Explains why a useful region has no content and what can happen next.", configuration: ["title", "description", "primary action", "secondary action"], behavior: "Distinguish first-use, filtered, and error emptiness.", accessibility: "Keep the recovery action immediately after the explanation." },
  "loading-skeleton": { purpose: "Preserves layout while known content is arriving.", configuration: ["shape", "width", "height", "count"], behavior: "Mirror the final geometry and escalate unusually long waits with copy.", accessibility: "Mark the region busy and avoid announcing decorative placeholders." },
  timeline: { purpose: "Orders events and their metadata along one chronology.", configuration: ["entries", "status", "timestamp", "density"], behavior: "Use ascending or descending time consistently within a product.", accessibility: "Render an ordered structure and include timestamps in text." },
  card: { purpose: "Groups one coherent subject and its nearby actions.", configuration: ["header", "content", "footer", "action", "elevation"], behavior: "Do not make every region a card; grouping must express one subject.", accessibility: "Use semantic headings and avoid competing nested click targets." },
  "metric-card": { purpose: "Pairs one decision-relevant measure with context and change.", configuration: ["value", "label", "delta", "trend", "status"], behavior: "Lead with the measure; make comparison period and unit explicit.", accessibility: "State trend direction and meaning in text, not only an arrow." },
  "markdown-content": { purpose: "Applies readable document typography to trusted rich content.", configuration: ["content", "measure", "density"], behavior: "Preserve heading hierarchy and a comfortable reading measure.", accessibility: "Generated headings, links, tables, and lists retain native semantics." },
  "markdown-render": { purpose: "Transforms Markdown source into product-safe rendered content.", configuration: ["source", "components", "sanitize", "streaming"], behavior: "Treat untrusted source as hostile and keep rendering deterministic.", accessibility: "The resulting document must preserve semantic structure." },
};

const titleCase = (id: string) =>
  id
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");

export const COMPONENT_DOCS: ComponentDoc[] = INVENTORY.map((item) => ({
  id: item.file,
  name: titleCase(item.file),
  group: item.group,
  ...DETAILS[item.file]!,
}));

export const COMPONENT_GROUPS = GROUP_LABELS.map((group) => ({
  ...group,
  components: COMPONENT_DOCS.filter((component) => component.group === group.id),
}));
