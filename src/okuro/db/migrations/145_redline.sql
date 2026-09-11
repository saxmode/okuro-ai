-- <!-- AGENT_HEADER
-- role: code
-- purpose: 145_redline — a comment on an HTML document belongs to ONE VERSION,
--   and a machine-guessed re-anchor is unrepresentable.
-- index: content
-- AGENT_HEADER_END -->
--
-- WHY NOT `reviews` (111). Four measured blockers, all still true:
--   1. reviews has no status / version / parent_id / done_at / done_by /
--      anchor column (111_reviews.sql:51-96).
--   2. reviews is append-only BY CONTRACT — "Nothing is ever updated in place
--      except todo_id" (111_reviews.sql:34-37) — and open->done is an
--      in-place update.
--   3. EVERY reviews row enters an outbound Supabase queue and the comment
--      text is sent verbatim (112_review_sync.sql:11-31, api/reviews.py:167-170).
--      Sync is opt-in AND was measured LIVE on the authoring install
--      (feedback.sync_enabled: true, with an org_label set). A client's
--      mockup comment must not join a product-telemetry pipe.
--   4. severity gates todo creation at WRITE time (sense/reviews.py:42-49),
--      so forty inline comments would create forty todos.
-- Same doctrine, different identity availability -> a second table set, exactly
-- as 111 itself chose over extending 108 and wrote down why (111_reviews.sql:9-18).
--
-- WHY version_id AND NOT document_id IS THE COMMENT'S HOME. Figma keeps
-- comments on the FILE because a Figma node has a stable id. Agent-generated
-- HTML does not: measured on the real mockups-v2 -> v3 regeneration, 0 of 5
-- anchors survived on ANY tier; v2 carries ZERO elements with an id and v3
-- carries 7 of 491. A comment pinned to the document would therefore point at
-- nothing after one regeneration while still claiming to point somewhere.
-- Per version is the honest shape, and the agent re-resolves by hand.
--
-- WHY THERE IS NO RESOLUTION CACHE. A cached (comment, version) resolution
-- goes stale whenever a sibling asset changes, and what it caches is a
-- querySelector that costs microseconds in a browser that is already open.
-- Under DP09 the machine rows do not earn a table. redline_reanchors exists
-- ONLY for rows the owner pointed by hand, and `manual` is CHECK-constrained
-- to 1 so a machine row cannot be written even by the sqlite3 binary.
--
-- Rollback (SQLite forward-only):
--   DROP TRIGGER IF EXISTS redline_reply_is_one_level;
--   DROP TABLE IF EXISTS redline_reanchors;
--   DROP TABLE IF EXISTS redline_comments;
--   DROP TABLE IF EXISTS redline_versions;
--   DROP TABLE IF EXISTS redline_documents;

CREATE TABLE IF NOT EXISTS redline_documents (
    id             TEXT PRIMARY KEY,              -- uuid4
    kind           TEXT NOT NULL CHECK (kind IN ('file', 'artifact', 'prism')),
    ref            TEXT NOT NULL,                 -- absolute realpath | artifact id | deck id
    title          TEXT NOT NULL DEFAULT '',
    project        TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    tombstoned_at  TEXT,                          -- tombstone, never delete
    UNIQUE (kind, ref)
);

CREATE TABLE IF NOT EXISTS redline_versions (
    id            TEXT PRIMARY KEY,               -- uuid4
    document_id   TEXT NOT NULL REFERENCES redline_documents(id),
    seq           INTEGER NOT NULL,               -- 1-based, monotonic per document
    -- file: sha256 of the served TREE (relpath + NUL + bytes, sorted).
    -- artifact: the artifact id of this link in the supersede chain.
    -- prism: sha256 of the DeckDoc JSON, sort_keys, compact separators.
    content_hash  TEXT NOT NULL,
    root_path     TEXT,                           -- directory served; NULL for prism
    label         TEXT,
    captured_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (document_id, seq),
    UNIQUE (document_id, content_hash)            -- reopening identical bytes is NOT a new version
);

CREATE TABLE IF NOT EXISTS redline_comments (
    id             TEXT PRIMARY KEY,              -- uuid4
    -- THE COMMENT'S HOME. Not the document.
    version_id     TEXT NOT NULL REFERENCES redline_versions(id),
    document_id    TEXT NOT NULL REFERENCES redline_documents(id),  -- denormalized, for the index
    seq            INTEGER NOT NULL,              -- the pin number, creation order within the version
    anchor         TEXT,                          -- the JSON of design v1.1 section 4; NULL for a reply
    body           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    author         TEXT NOT NULL DEFAULT 'owner',
    parent_id      TEXT REFERENCES redline_comments(id),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    done_at        TEXT,
    done_by        TEXT,
    done_note      TEXT,                          -- what the agent did; the actual evidence
    after_excerpt  TEXT,                          -- the element's outerHTML after the fix
    promoted_review_id TEXT,                      -- manual bridge to reviews(id); never auto-filled
    tombstoned_at  TEXT,

    -- Spelled POSITIVELY, per 143's lesson: a row is exempt only when it is
    -- NOT done. Anything claiming status='done' must positively carry all
    -- three, so an empty resolve is refused by the SCHEMA and not only by
    -- the Python that happens to be calling today.
    CHECK (status != 'done' OR (
        done_at IS NOT NULL
        AND done_by IS NOT NULL AND TRIM(done_by) != ''
        AND done_note IS NOT NULL AND TRIM(done_note) != ''
    )),
    -- A reply is text about a comment; it has no anchor of its own.
    CHECK (parent_id IS NULL OR anchor IS NULL),
    UNIQUE (version_id, seq)
);

-- One reply level, and a reply lives in its parent's version. A CHECK cannot
-- hold a subquery in SQLite, so the invariant lives in a trigger.
CREATE TRIGGER IF NOT EXISTS redline_reply_is_one_level
BEFORE INSERT ON redline_comments
FOR EACH ROW WHEN NEW.parent_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'redline: a reply must target a top-level comment in the same version')
    WHERE (SELECT parent_id  FROM redline_comments WHERE id = NEW.parent_id) IS NOT NULL
       OR (SELECT version_id FROM redline_comments WHERE id = NEW.parent_id) != NEW.version_id;
END;

-- MANUAL re-anchors only. There is no machine resolution cache anywhere in
-- this module, and `manual = 1` makes writing one impossible rather than
-- merely discouraged.
CREATE TABLE IF NOT EXISTS redline_reanchors (
    comment_id   TEXT NOT NULL REFERENCES redline_comments(id),
    version_id   TEXT NOT NULL REFERENCES redline_versions(id),
    anchor       TEXT NOT NULL,                   -- a fresh anchor JSON for the NEW version
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    created_by   TEXT NOT NULL DEFAULT 'owner',
    manual       INTEGER NOT NULL DEFAULT 1 CHECK (manual = 1),
    PRIMARY KEY (comment_id, version_id)
);

CREATE INDEX IF NOT EXISTS idx_redline_comments_version ON redline_comments(version_id, status, seq);
CREATE INDEX IF NOT EXISTS idx_redline_comments_doc     ON redline_comments(document_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_redline_comments_parent  ON redline_comments(parent_id);
CREATE INDEX IF NOT EXISTS idx_redline_versions_doc     ON redline_versions(document_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_redline_documents_ref    ON redline_documents(kind, ref);
