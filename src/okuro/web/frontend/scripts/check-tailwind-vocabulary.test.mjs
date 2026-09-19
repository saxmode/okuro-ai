import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { findMissingUtilities, formatMissing } from "./check-tailwind-vocabulary.mjs";

function fixture(source, css) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "okuro-tailwind-vocabulary-"));
  const sourceRoot = path.join(root, "src");
  const cssRoot = path.join(root, "dist");
  fs.mkdirSync(sourceRoot);
  fs.mkdirSync(cssRoot);
  fs.writeFileSync(path.join(sourceRoot, "component.tsx"), source);
  fs.writeFileSync(path.join(cssRoot, "index.css"), css);
  return { root, sourceRoot, cssRoot };
}

test("reports semantic utilities that Tailwind dropped", (t) => {
  const paths = fixture(
    '<div className="bg-surface text-error hover:border-warning/40" />',
    ".bg-surface{background:#000}.text-error{color:red}",
  );
  t.after(() => fs.rmSync(paths.root, { recursive: true, force: true }));

  const missing = findMissingUtilities(paths.sourceRoot, paths.cssRoot);
  assert.equal(formatMissing(missing), "  border-warning/40: component.tsx");
});

test("a `/*` inside a string does not blind the scan that follows it", (t) => {
  // The real shape: `accept="image/*"` on a file input, or a comment that
  // mentions `/api/stack/*`. Naively that opens a block comment which runs to
  // the next real `*/`, and every className in between disappears — measured
  // at 46,053 characters across eight files, 58.8 % of `pages/stack.tsx`.
  const paths = fixture(
    '<input accept="image/*" />\n' +
      '/* a real comment with bg-phantom in it */\n' +
      '<div className="bg-surface text-invented-tone" />',
    ".bg-surface{background:#000}",
  );
  t.after(() => fs.rmSync(paths.root, { recursive: true, force: true }));

  // `text-invented-tone` sits AFTER the string that used to open the fake
  // comment, so it is only reported once the lookbehind is in place.
  assert.equal(
    formatMissing(findMissingUtilities(paths.sourceRoot, paths.cssRoot)),
    "  text-invented-tone: component.tsx",
  );
});

test("ignores comments and non-colour Tailwind utilities", (t) => {
  const paths = fixture(
    '// text-ghost\n/* bg-phantom */\n<div className="text-left border-0" />',
    ".text-left{text-align:left}.border-0{border-width:0}",
  );
  t.after(() => fs.rmSync(paths.root, { recursive: true, force: true }));

  assert.equal(findMissingUtilities(paths.sourceRoot, paths.cssRoot).size, 0);
});
