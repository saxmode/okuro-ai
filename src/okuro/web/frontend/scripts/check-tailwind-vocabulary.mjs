// Build gate: every literal semantic colour utility in TSX must compile to CSS.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const UTILITY_PATTERN = /(?<![\w-])(?:[a-z-]+:)*!?((?:bg|text|border|ring|outline|fill|stroke)-(?!\[)[a-z][a-z0-9-]*(?:\/\d+)?)(?![\w/-])/g;

const NON_CLASS_LITERALS = new Map([
  ["components/design-engine/tables.tsx", new Set(["border-branded", "border-full", "border-half"])],
  ["pages/prism-gallery.tsx", new Set(["text-only"])],
  ["pages/studio.tsx", new Set(["text-engines", "text-models"])],
]);

function walk(root) {
  return fs.readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const target = path.join(root, entry.name);
    return entry.isDirectory() ? walk(target) : [target];
  });
}

function withoutComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
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
