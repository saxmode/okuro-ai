# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: CONTENT + SECRET guards — scan staged additions for real
#   brand/person tokens and for credentials.
# index:
#   imports
#   EXCLUDED_SUFFIXES / EXCLUDED_PREFIXES
#   STOPWORD_PARTS / NEVER_TOKENS
#   def token_files
#   def load_tokens
#   def load_scoped_exceptions / def permitted_at
#   def compile_tokens
#   def compile_phrase_exceptions
#   def find_tokens
#   def find_secrets
#   def scan_staged_secrets
#   def refresh_tokens
#   def iter_added_lines
#   def scan_staged
#   def main
# AGENT_HEADER_END -->
"""Two content guards: no real brand/person token, and no credential.

CONTENT and SECRET share this module because they share the mechanism —
both read the ADDED lines of the staged diff and refuse the commit. They
differ in scope and in remedy: CONTENT is okuro-repo-only and is fixed by
swapping in a demo entity; SECRET applies wherever the guard is installed
and is fixed by moving the value into the keyring. SECRET needs no user
token list, so it works on a fresh install where CONTENT is inert.

CONTENT: no real brand/person token may enter okuro's tree.

The path guards (SCOPE, PUBLISH) classify by location; this one reads what
the commit actually says. It scans the ADDED lines of the staged diff for
tokens from the user's own list — real brands, customers, people — and
refuses the commit when one appears. okuro-repo-only, like the root
allowlist: in the user's other repos those names are their daily work.

THE TOKEN LIST IS ITSELF USER DATA. A denylist of customer names committed
to this repo would leak the very names it protects (that exact mistake
shipped once, as a constant in design_systems/importer.py). So the list
lives in ``~/.okuro/guard/``, never in the tree:

  tokens.txt        the working set (one token per line, # comments ok)
  tokens-extra.txt  manual additions kept across regenerations

and the anchor is ``(?<![A-Za-z0-9])tok(?![A-Za-z0-9])``, not ``\\b`` —
measured on this repo's history: ``\\b`` counts ``_`` as a word character
so brand_UI_v2 survives, and an unanchored scan corrupts its own verdict
on base64 in lockfiles. Lockfiles, dist and vendor trees are excluded as
paths on top of that.

``tokens.txt`` is GENERATED from the CRM (persons.display_name +
organization, companies.name/id/domain) so a new CRM entry covers itself —
the previous hand-maintained list missed bare surnames and that gap hid
the single most exposed string of the 2026-08 audit. ``scan-staged``
regenerates opportunistically when the file is older than REFRESH_DAYS
and falls back to the existing file when the DB is unreachable; manual:
``python -m okuro.cli.guard_content refresh``. Curation stays in files:
``tokens-extra.txt`` (always added) and ``tokens-ignore.txt`` (never
generated) survive every regeneration.

A machine with NO token files simply passes — a fresh install has no real
data to protect. A token file that exists but cannot be read fails CLOSED.
Bypass for a deliberate exception: ``OKURO_ALLOW_CONTENT=1 git commit``.

``tokens-ignore.txt`` has TWO line forms:

  acmecorp                          plain — the token is retracted everywhere
  jane doe @ LICENSE,NOTICE,pyproject.toml
                                    path-scoped — the token stays LIVE and is
                                    permitted only in the listed repo-relative
                                    paths (exact, or fnmatch globs such as
                                    ``docs/*``); anywhere else it still fires

The scoped form exists because the plain form was used for the owner's
attribution (2026-08-09): the name and email had to ship in three files, so
they were retracted globally — and the guard went blind to them in EVERY
file, at commit time and gate time alike (measured 2026-09-09, memory
85a02729). An exception scoped to three paths must be expressed as an
exception scoped to three paths.
"""

from __future__ import annotations

import re
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

from okuro.db.engine import okuro_home

#: Paths never scanned: machine-written files whose bytes can collide with
#: any short token (base64 integrity hashes, minified bundles, model vocab).
EXCLUDED_SUFFIXES = (
    ".lock", ".min.js", ".map", ".svg", ".ico",
)
EXCLUDED_BASENAMES = (
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "requirements-lock.txt",
)
EXCLUDED_PREFIXES = (
    "src/okuro/web/dist/", "node_modules/",
)
#: Minimum token length — anything shorter false-positives on ordinary text.
MIN_TOKEN_LEN = 3

#: Regenerate tokens.txt from the CRM when it is older than this.
REFRESH_DAYS = 7

#: Name PARTS too generic to scan alone (HTTP POST, mail handling, a test
#: fixture). A multi-word phrase still becomes a token even when every one
#: of its parts is stopped — a fixture company "Test Post" yields the token
#: "test post" and nothing else. Extend per false positive via
#: ~/.okuro/guard/tokens-ignore.txt, which survives regeneration.
STOPWORD_PARTS = frozenset({
    "post", "mail", "team", "test", "testperson", "delivery", "admin",
    "office", "group", "board", "shop", "manager", "service", "swiss",
    # legal forms and generic entity words — a CRM row named "Company" or
    # "X GmbH" must not turn ordinary code vocabulary into a refusal
    "company", "companies", "gmbh", "ltd", "llc", "inc", "corp",
    "corporation", "holding", "agency", "agentur",
})

def _owner_never_tokens() -> frozenset[str]:
    """The owner's own attribution words — DERIVED, never written here.

    This module ships in the public export, so a literal owner name in it
    is a leak the guard itself carries. The words come from the package's
    declared author (name words + email local part, via installed metadata,
    falling back to the repo's pyproject) and the release repo's GitHub
    org. Same principle as okuro.release.gates.owner_terms.
    """
    words: set[str] = set()
    name = email = ""
    try:
        from importlib.metadata import metadata
        raw = " ".join(metadata("okuro").get_all("Author-email") or [])
        name, _, rest = raw.partition("<")
        email = rest.rstrip(">").strip()
    except Exception:
        pass
    if not name:
        pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
        try:
            import tomllib
            with open(pyproject, "rb") as fh:
                for a in tomllib.load(fh).get("project", {}).get("authors", []):
                    name, email = a.get("name", ""), a.get("email", "")
                    break
        except Exception:
            pass
    words.update(w for w in re.split(r"[^a-z]+", name.lower()) if w)
    if "@" in email:
        words.add(email.split("@", 1)[0].lower())
    try:
        from okuro.release.manifest import RELEASE_REPO_URL
        words.add(RELEASE_REPO_URL.split("github.com/", 1)[1].split("/", 1)[0].lower())
    except Exception:
        pass
    return frozenset(w for w in words if len(w) >= MIN_TOKEN_LEN)


#: Never tokens, regardless of what the CRM says: okuro itself, the demo
#: roster, and the owner's own attribution (README/NOTICE name the author).
NEVER_TOKENS = frozenset({"okuro", "northwind", "meridian", "lodestar"}) | _owner_never_tokens()


# ---------------------------------------------------------------------------
# SECRET — a credential may not enter the tree
# ---------------------------------------------------------------------------
#
# THE GAP THIS CLOSES, measured 2026-09-09. "ALWAYS load secrets via keyring —
# NEVER a .env file, NEVER a hardcoded credential" is stated in the CORE packet,
# in every provider instruction file and in TOOL-PROTOCOL.md, and NOTHING
# checked it. Of every rule in okuro's cross-surface contradiction matrix it was
# the only one binding on three surfaces with no gate and no hook anywhere.
#
# SCOPE already denies a `.env` FILE by path. That is a different thing: a path
# denylist cannot see `API_KEY = "sk-…"` inside a .py, which is the shape an
# agent actually produces — it is not trying to smuggle a dotfile, it is
# inlining a value it just read.
#
# PRECISION OVER RECALL, deliberately. A guard that cries wolf is switched off,
# and this one blocks commits. So: provider-prefixed keys (unmistakable), PEM
# private-key headers (unmistakable), and ONE heuristic — a secret-NAMED
# variable assigned a long literal — fenced by placeholder and correct-usage
# exclusions. A random 40-char string in an unnamed variable is NOT flagged;
# that is a deliberate hole, because closing it costs more false positives than
# it prevents leaks.

#: Vendor-prefixed credentials. Each prefix is registered and unambiguous, so a
#: match needs no entropy test and no name context.
_SECRET_PREFIX_RX = re.compile(
    r"""(?x)
    \b(?:
        sk-ant-[A-Za-z0-9_-]{16,}      # Anthropic
      | sk-[A-Za-z0-9]{20,}            # OpenAI and lookalikes
      | ghp_[A-Za-z0-9]{30,}           # GitHub personal token
      | gho_[A-Za-z0-9]{30,}           # GitHub OAuth
      | ghu_[A-Za-z0-9]{30,}
      | ghs_[A-Za-z0-9]{30,}
      | github_pat_[A-Za-z0-9_]{50,}
      | glpat-[A-Za-z0-9_-]{16,}       # GitLab
      | xox[baprs]-[A-Za-z0-9-]{10,}   # Slack
      | AIza[A-Za-z0-9_-]{30,}         # Google API
      | (?:AKIA|ASIA)[A-Z0-9]{16}      # AWS access key id
      | dop_v1_[a-f0-9]{60,}           # DigitalOcean
      | npm_[A-Za-z0-9]{30,}           # npm
      | hf_[A-Za-z0-9]{30,}            # Hugging Face
    )
    """
)

#: An armoured private key. The header alone is proof; no length test needed.
_PRIVATE_KEY_RX = re.compile(
    r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----"
)

#: The heuristic: a secret-NAMED variable assigned a long quoted literal.
_NAMED_SECRET_RX = re.compile(
    r"""(?xi)
    \b(?P<name>[A-Za-z0-9_.-]*
        (?:passwd|password|secret|token|api[_-]?key|access[_-]?key
          |private[_-]?key|credential|auth[_-]?key|client[_-]?secret)
       [A-Za-z0-9_.-]*)
    \s*[:=]\s*
    (?P<q>["'])(?P<val>[^"']{16,})(?P=q)
    """
)

#: A value carrying any of these is a placeholder, an example or a schema
#: description — never a live credential.
_PLACEHOLDER_MARKERS = (
    "<", ">", "{", "}", "$", "...", "***", "xxx", "example", "changeme",
    "your_", "your-", "yourkey", "dummy", "fake", "placeholder", "redacted",
    "sample", "insert", "replace", "todo", "n/a", "none", "null",
    "abc123", "s3cr3t", "hunter2", "correct-horse",
)

#: A line doing the RIGHT thing mentions the route. Reading a secret from the
#: keyring or the environment is the rule, not a violation of it — and the
#: variable it lands in is legitimately named `api_key`.
_CORRECT_USAGE_MARKERS = (
    "keyring_get", "keyring.get", "os.environ", "os.getenv", "getenv(",
    "process.env", "secretsmanager", "vault.read",
)

#: Paths whose business IS credential shapes: this guard, and its tests.
_SECRET_SCAN_EXEMPT_PATHS = (
    "src/okuro/cli/guard_content.py",
    "tests/cli/test_guard_secrets.py",
)

#: A value that is itself an IDENTIFIER, not a credential. `API_KEY_ENV_VAR =
#: "ANTHROPIC_API_KEY"` names the variable to read; it is the shape of correct
#: config code, and flagging it punishes exactly the pattern the rule wants.
#: A real credential is mixed-case or carries punctuation; a SCREAMING_SNAKE
#: token is a name. Vendor-prefixed keys are matched on a separate path and are
#: unaffected by this — `AKIA…` is still caught.
_IDENTIFIER_VALUE_RX = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _looks_like_placeholder(value: str) -> bool:
    low = value.lower()
    if any(m in low for m in _PLACEHOLDER_MARKERS):
        return True
    if _IDENTIFIER_VALUE_RX.match(value):
        return True
    # A single repeated character, or no variety at all, is a filler string.
    return len(set(value)) < 5


def find_secrets(text: str) -> list[tuple[str, str]]:
    """Return (kind, evidence) for every credential shape in one added line.

    ``evidence`` is deliberately NOT the secret: it names the pattern or the
    variable. A guard that prints the credential it caught has copied it into
    a terminal, a scrollback and an agent's context.
    """
    if any(marker in text for marker in _CORRECT_USAGE_MARKERS):
        return []

    out: list[tuple[str, str]] = []
    for m in _SECRET_PREFIX_RX.finditer(text):
        out.append(("vendor key", m.group(0)[:8] + "…"))
    if _PRIVATE_KEY_RX.search(text):
        out.append(("private key", "PEM private key block"))
    for m in _NAMED_SECRET_RX.finditer(text):
        if not _looks_like_placeholder(m.group("val")):
            out.append(("hardcoded credential", f"{m.group('name')}=…"))
    return out


def scan_staged_secrets(root: Path) -> list[tuple[str, int, str, str]]:
    """(path, line, kind, evidence) for every credential a staged addition adds."""
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "-U0",
         "--no-color", "--diff-filter=ACMR"],
        capture_output=True, text=True, errors="replace", check=True,
    ).stdout
    findings: list[tuple[str, int, str, str]] = []
    for path, lineno, text in iter_added_lines(diff):
        if _excluded(path) or path in _SECRET_SCAN_EXEMPT_PATHS:
            continue
        for kind, evidence in find_secrets(text):
            findings.append((path, lineno, kind, evidence))
    return findings


def token_files() -> list[Path]:
    gdir = okuro_home() / "guard"
    return [gdir / "tokens.txt", gdir / "tokens-extra.txt"]


def load_tokens() -> list[str]:
    """Union of both token files minus PLAIN ignore entries, lowercased.

    Missing files are fine (nothing to protect); an unreadable existing
    file raises — fail closed, a guard that shrugs is not a guard.

    tokens-ignore.txt wins over tokens-extra.txt: an ignore entry is the
    false-positive remedy, and it must reach CURATED tokens too — ignore
    used to filter only the generated list, which left a curated token
    with no remedy at all (the hole the phrase-exception mechanism first
    exposed). Both additions and retractions are deliberate user acts;
    the retraction is the later, more specific one.

    A PATH-SCOPED ignore entry does not retract: its token stays in this
    list and the scanners consult :func:`load_scoped_exceptions` per file.
    """
    ignored = _ignored()
    seen: dict[str, None] = {}
    for tf in token_files():
        if not tf.exists():
            continue
        for raw in tf.read_text(encoding="utf-8").splitlines():
            tok = raw.split("#", 1)[0].strip().lower()
            if len(tok) >= MIN_TOKEN_LEN and tok not in ignored:
                seen.setdefault(tok)
    return list(seen)


def compile_tokens(tokens: list[str]) -> re.Pattern[str] | None:
    if not tokens:
        return None
    alternation = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    return re.compile(
        rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])", re.IGNORECASE
    )


#: A scoped line: token, whitespace, ``@``, then the paths. The whitespace
#: before ``@`` is REQUIRED so an email address (``a@b``) is never mistaken
#: for a scoped entry; the paths may be absent (malformed → plain).
_SCOPED_LINE_RX = re.compile(r"^(?P<tok>.+?)\s+@(?:\s+(?P<paths>.*))?$")


def _parse_ignore() -> tuple[set[str], dict[str, tuple[str, ...]]]:
    """``(plain, scoped)`` from tokens-ignore.txt.

    ``plain`` retracts globally (today's behaviour, unchanged). ``scoped``
    maps a token to the repo-relative paths/globs where it is permitted; the
    token itself stays live. A scoped line with an empty path list is a
    plain line — the safer reading of a malformed entry.
    """
    f = okuro_home() / "guard" / "tokens-ignore.txt"
    plain: set[str] = set()
    scoped: dict[str, tuple[str, ...]] = {}
    if not f.exists():
        return plain, scoped
    for raw in f.read_text(encoding="utf-8").splitlines():
        entry = raw.split("#", 1)[0].strip()
        if not entry:
            continue
        m = _SCOPED_LINE_RX.match(entry)
        if m:
            tok = m.group("tok").strip().lower()
            globs = tuple(p.strip() for p in (m.group("paths") or "").split(",") if p.strip())
            if tok and globs:
                scoped[tok] = tuple(sorted(set(scoped.get(tok, ())) | set(globs)))
                continue
            entry = tok
        if entry:
            plain.add(entry.lower())
    return plain, scoped


def _ignored() -> set[str]:
    """PLAIN ignore entries only — the globally retracted set."""
    return _parse_ignore()[0]


def load_scoped_exceptions() -> dict[str, tuple[str, ...]]:
    """token -> repo-relative paths/globs where that token is permitted."""
    return _parse_ignore()[1]


def permitted_at(token: str, path: str | None, scoped: dict[str, tuple[str, ...]] | None) -> bool:
    """Is a match of ``token`` inside file ``path`` covered by a scoped
    exception? Exact path or fnmatch glob (``*`` crosses ``/``, so
    ``docs/*`` covers the whole subtree). No path known → never permitted."""
    if not scoped or path is None:
        return False
    globs = scoped.get(token.lower())
    if not globs:
        return False
    return any(path == g or fnmatchcase(path, g) for g in globs)


def compile_phrase_exceptions() -> re.Pattern[str] | None:
    """Ignore entries that are PHRASES (contain a token boundary) become
    context exceptions: a token match INSIDE such a phrase is a false
    positive, not a finding.

    Why this exists: single-word ignore entries only filter the GENERATED
    list, so a curated token in tokens-extra.txt had no false-positive
    remedy at all — found live when the curated GPU-name token refused
    ``o-kuro.svg``, the product's own logo. Adding ``o-kuro`` to
    tokens-ignore.txt now suppresses exactly that context and nothing
    else; the bare token keeps protecting everywhere else.
    """
    phrases = [p for p in _ignored() if re.search(r"[^a-z0-9äöüéèàß]", p)]
    if not phrases:
        return None
    alternation = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(
        rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])", re.IGNORECASE
    )


def find_tokens(
    text: str,
    rx: re.Pattern[str],
    ignore_rx: re.Pattern[str] | None,
    *,
    path: str | None = None,
    scoped: dict[str, tuple[str, ...]] | None = None,
):
    """Token matches in ``text`` that do not sit inside an ignored phrase
    and are not permitted in ``path`` by a scoped exception."""
    spans = [m.span() for m in ignore_rx.finditer(text)] if ignore_rx is not None else []
    for m in rx.finditer(text):
        if any(a <= m.start() and m.end() <= b for a, b in spans):
            continue
        if permitted_at(m.group(0), path, scoped):
            continue
        yield m


def _candidate_tokens(names: list[str]) -> set[str]:
    """A full name yields the phrase plus each distinctive part.

    A SINGLE-word name is a part and takes the stopword filter too —
    without this, a domain label or one-word company named after a generic
    word ships that word into the token list and the guard fires on every
    HTTP verb in the diff (found live: the guard refused its own commit).
    """
    out: set[str] = set()
    for name in names:
        name = (name or "").strip().lower()
        # digits are word characters here: splitting on them shreds a brand
        # like brand4you into generic English ("brand") and arms the guard
        # against ordinary vocabulary — found live via survey tests
        parts = [
            p for p in re.split(r"[^a-z0-9äöüéèàß]+", name)
            if p and not p.isdigit()
        ]
        if len(name) >= max(MIN_TOKEN_LEN, 4) and not (
            len(parts) == 1 and parts[0] in STOPWORD_PARTS
        ):
            out.add(name)
        for part in parts:
            if len(part) >= 4 and part not in STOPWORD_PARTS:
                out.add(part)
    return out - NEVER_TOKENS


def refresh_tokens(db=None, *, force: bool = False) -> dict:
    """Regenerate ~/.okuro/guard/tokens.txt from the CRM.

    Idempotent and freshness-gated: a file younger than REFRESH_DAYS is kept
    unless ``force``. When the DB is unreachable and a file exists, the file
    stands (stale beats absent); with no file either, the error propagates.
    """
    import time

    target = okuro_home() / "guard" / "tokens.txt"
    if target.exists() and not force:
        age_days = (time.time() - target.stat().st_mtime) / 86400
        if age_days < REFRESH_DAYS:
            return {"refreshed": False, "reason": f"fresh ({age_days:.1f}d)"}

    if db is None:
        from okuro.db import get_db
        db = get_db()

    names: list[str] = []
    for sql in (
        "SELECT display_name FROM persons WHERE active = 1",
        "SELECT organization FROM persons WHERE active = 1",
        "SELECT name FROM companies WHERE active = 1",
        "SELECT id FROM companies WHERE active = 1",
        "SELECT domain FROM companies WHERE active = 1",
    ):
        for row in db.fetchall(sql):
            val = next(iter(dict(row).values()), None)
            if val:
                # a domain's registrable label is the brand: zebrabrand.ch
                names.append(str(val).split(".", 1)[0] if "." in str(val) else str(val))

    tokens = sorted(_candidate_tokens(names) - _ignored())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# okuro CONTENT guard — GENERATED from the CRM (persons + companies).\n"
        "# Regenerated when older than 7 days, or via:\n"
        "#   python -m okuro.cli.guard_content refresh\n"
        "# Do NOT hand-edit: additions go to tokens-extra.txt, false positives\n"
        "# to tokens-ignore.txt — both survive regeneration.\n"
        + "\n".join(tokens) + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    return {"refreshed": True, "tokens": len(tokens), "path": str(target)}


def _excluded(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return (
        base in EXCLUDED_BASENAMES
        or path.endswith(EXCLUDED_SUFFIXES)
        or path.startswith(EXCLUDED_PREFIXES)
        or "/node_modules/" in path
    )


def iter_added_lines(diff_text: str):
    """Yield (path, new_line_number, line_text) for every added line."""
    path: str | None = None
    lineno = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) if m else 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            if path is not None:
                yield path, lineno, raw[1:]
            lineno += 1
        elif not raw.startswith("-"):
            lineno += 1


def scan_staged(root: Path) -> list[tuple[str, int, str]]:
    """(path, line, token) for every real-data token a staged addition carries."""
    try:
        refresh_tokens()  # freshness-gated; a failure must not mask the scan
    except Exception:
        pass  # stale beats absent; load_tokens() below is the authority
    rx = compile_tokens(load_tokens())
    if rx is None:
        return []
    ignore_rx = compile_phrase_exceptions()
    scoped = load_scoped_exceptions()
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "-U0",
         "--no-color", "--diff-filter=ACMR"],
        capture_output=True, text=True, errors="replace", check=True,
    ).stdout
    findings: list[tuple[str, int, str]] = []
    for path, lineno, text in iter_added_lines(diff):
        if _excluded(path):
            continue
        for m in find_tokens(text, rx, ignore_rx, path=path, scoped=scoped):
            findings.append((path, lineno, m.group(0).lower()))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args == ["refresh"]:
        print(refresh_tokens(force=True))
        return 0
    if args == ["scan-secrets"]:
        root = Path.cwd()
        try:
            secrets = scan_staged_secrets(root)
        except Exception as exc:  # fail closed, same as the token scan
            print(f"secret guard could not run: {exc}", file=sys.stderr)
            return 1
        if not secrets:
            return 0
        for path, lineno, kind, evidence in secrets:
            print(f"  - {path}:{lineno} {kind}: {evidence}")
        return 1
    if args != ["scan-staged"]:
        print(
            "usage: python -m okuro.cli.guard_content "
            "{scan-staged|scan-secrets|refresh}",
            file=sys.stderr,
        )
        return 2
    root = Path.cwd()
    try:
        findings = scan_staged(root)
    except Exception as exc:  # fail closed: an unreadable guard is a refusal
        print(f"content guard could not run: {exc}", file=sys.stderr)
        return 1
    if not findings:
        return 0
    for path, lineno, tok in findings:
        print(f"  - {path}:{lineno} carries {tok!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
