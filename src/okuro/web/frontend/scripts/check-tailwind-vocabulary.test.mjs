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

test("ignores comments and non-colour Tailwind utilities", (t) => {
  const paths = fixture(
    '// text-ghost\n/* bg-phantom */\n<div className="text-left border-0" />',
    ".text-left{text-align:left}.border-0{border-width:0}",
  );
  t.after(() => fs.rmSync(paths.root, { recursive: true, force: true }));

  assert.equal(findMissingUtilities(paths.sourceRoot, paths.cssRoot).size, 0);
});
