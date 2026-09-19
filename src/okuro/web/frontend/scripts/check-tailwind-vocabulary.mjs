// Build gate: every literal semantic colour utility in TSX must compile to CSS.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const UTILITY_PATTERN = /(?<![\w-])(?:[a-z-]+:)*!?((?:bg|text|border|ring|outline|fill|stroke)-(?!\[)[a-z][a-z0-9-]*(?:\/\d+)?)(?![\w/-])/g;

const NON_CLASS_LITERALS = new Map([
  ["pages/prism-gallery.tsx", new Set(["text-only"])],
  ["pages/studio.tsx", new Set(["text-engines", "text-models"])],
]);

function walk(root) {
  return fs.readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const target = path.join(root, entry.name);
    return entry.isDirectory() ? walk(target) : [target];
  });
}

/**
 * A `/*` INSIDE A STRING IS NOT A COMMENT, AND READING IT AS ONE BLINDED THIS
 * GATE TO 46,053 CHARACTERS OF SOURCE.
 *
 * Measured across `src/` on 2026-09-15, naive strip vs this one:
 *
 *   src/pages/stack.tsx                          34,206 of 58,203   58.8 %
 *   src/components/task/continue-bar.tsx          5,175 of 17,460   29.6 %
 *   src/lib/api.ts                                2,159 of 67,749    3.2 %
 *   src/components/task/artifacts-viewer.tsx      2,148 of 38,900    5.5 %
 *   src/components/prism/deck2/archetypes.tsx     1,707 of 18,726    9.1 %
 *   + 3 more files
 *
 * The openers are ordinary strings: `accept="image/*"`, a comment mentioning
 * `/api/stack/*`, a glob. Each one starts a comment that runs to the next
 * real `*​/`, so everything between — every className in it — was invisible to
 * the gate. On the largest file that was three fifths of the page.
 *
 * THE LOOKBEHIND IS THE WHOLE FIX: a real block comment's `/*` is never
 * preceded by a word character or a quote, while every false positive above
 * is (`image/*` -> `e`, `stack/*` -> `k`). Line comments are stripped FIRST so
 * a `//` that mentions `/*` cannot open one either.
 */
function withoutComments(source) {
  return source
    .replace(/^\s*\/\/.*$/gm, "")
    .replace(/(?<![\w"'])\/\*[\s\S]*?\*\//g, "");
}

export function findMissingUtilities(sourceRoot, cssRoot) {
  const compiled = walk(cssRoot)
    .filter((file) => file.endsWith(".css"))
    .map((file) => fs.readFileSync(file, "utf8"))
    .join("\n");
  const missing = new Map();

  for (const file of walk(sourceRoot).filter((candidate) => candidate.endsWith(".tsx"))) {
    const relative = path.relative(sourceRoot, file).split(path.sep).join("/");
    const exceptions = NON_CLASS_LITERALS.get(relative) ?? new Set();
    const source = withoutComments(fs.readFileSync(file, "utf8"));
    for (const match of source.matchAll(UTILITY_PATTERN)) {
      const utility = match[1];
      if (exceptions.has(utility) || compiled.includes(utility.replaceAll("/", "\\/"))) continue;
      if (!missing.has(utility)) missing.set(utility, new Set());
      missing.get(utility).add(relative);
    }
  }
  return missing;
}

export function formatMissing(missing) {
  return [...missing]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([utility, files]) => `  ${utility}: ${[...files].sort().join(", ")}`)
    .join("\n");
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const frontendRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  const sourceRoot = path.join(frontendRoot, "src");
  const cssRoot = path.join(frontendRoot, "..", "dist", "assets");
  const missing = findMissingUtilities(sourceRoot, cssRoot);
  if (missing.size) {
    console.error(`Tailwind silently dropped ${missing.size} semantic utilities:\n${formatMissing(missing)}`);
    process.exitCode = 1;
  }
}
