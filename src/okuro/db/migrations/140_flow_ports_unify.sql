-- Migration 140 — rewrite stored okuro·flow graphs onto the single ports[] list.
--
-- WHY. An okuro·flow node used to carry TWO port lists, data.ins[] and
-- data.outs[]. Handles were then drawn per side as ins-then-outs, so the order
-- of handles on a side was a consequence of direction and could not be
-- authored — which is what crossed the edge vectors whenever a node had an in
-- and an out on the same side. The model is now ONE ordered data.ports[], each
-- port carrying its own dir, and placement is by index within the side. The
-- frontend (graph.ts migrateNodePorts) and layout.py have accepted both shapes
-- since that landed, and as of this migration's branch nothing in okuro EMITS
-- the old one. This migration finishes the job: the rows themselves.
--
-- Readers stay dual-shape ON PURPOSE and this migration does not change that.
-- An agent writing through flow_designer_save, a hand-edited JSON, an old
-- export — all still arrive as ins/outs and are converted on the way in. What
-- ends is okuro storing or emitting it.
--
-- SEMANTICS, mirroring graph.ts::migrateNode so a migrated row and a
-- client-migrated one are the same document:
--   * ports[] already present and NON-EMPTY  -> kept verbatim; stray ins/outs
--     dropped (ports is authoritative, the legacy keys are dead weight)
--   * ins/outs present -> ONE list, ins first then outs — the order the old
--     renderer drew them in, so no handle moves on screen — each port stamped
--     with its dir; ins/outs removed. A per-port `side` is preserved exactly,
--     including the [] case: an EMPTY ins[] means the author deleted that
--     handle, and it converts to no in-port rather than being refilled.
--   * neither -> untouched. Annotation nodes (title/note/mdnote) have no ports
--     and gain none. NOTE the deliberate asymmetry with the emitter
--     (flow_designer/ports.py::norm_node), which DOES inject a default in/out
--     pair on a wired node: a node being minted has no author intent to
--     preserve, a stored one does. Inventing handles here would resurrect
--     ports a user had removed.
--
-- Recursion: json_tree walks the whole document, so a group's nested
-- data.members[] — and members of members — are converted at any depth, which
-- is what migrateNodePorts does client-side. Edges are NOT touched: a handle
-- addresses a port BY ID and the ids survived the merge.
--
-- BOTH tables that persist a graph are rewritten. flow_designer_history is not
-- an archive here — restore_history writes a snapshot straight back into
-- flow_designer, so leaving legacy snapshots in place would let a restore
-- reintroduce the shape this migration removes. flow_designer_events is
-- deliberately NOT touched: it is an append-only ephemeral change feed that
-- carries no graph payload.
--
-- Idempotent: after one pass no object carries ins/outs, so `tgt` is empty and
-- the UPDATE matches nothing. Safe to re-run.
--
-- Rollback (SQLite forward-only). There is no automatic reverse — dir would
-- have to be dropped and the list split back at the in/out boundary, and a
-- ports[] list authored AFTER this migration cannot be expressed in the old
-- shape at all (that is the entire point of the model change). The pre-
-- migration snapshot taken by SQLiteDB.migrate() into ~/.okuro/backups/ is the
-- recovery path.

-- ── flow_designer ───────────────────────────────────────────────────────────
-- The fold walks one legacy path per step, carrying the whole document, so a
-- row with N legacy nodes takes N steps and ends at k = max(k). A CASE-guarded
-- json_valid keeps a NULL / non-JSON blob out of json_tree, which would
-- otherwise abort the migration on one bad row.
CREATE TEMP TABLE _ports140_docs AS
WITH RECURSIVE
tgt(rid, p, k) AS (
    SELECT f.rowid, t.fullkey,
           ROW_NUMBER() OVER (PARTITION BY f.rowid ORDER BY t.fullkey)
      FROM flow_designer f,
           json_tree(CASE WHEN json_valid(f.graph) THEN f.graph ELSE '{}' END) t
     WHERE t.type = 'object'
       AND (json_type(t.value, '$.ins') IS NOT NULL
         OR json_type(t.value, '$.outs') IS NOT NULL)
),
fold(rid, k, doc) AS (
    SELECT rowid, 0, graph FROM flow_designer
     WHERE rowid IN (SELECT rid FROM tgt)
  UNION ALL
    SELECT f.rid, t.k,
           json_remove(
               CASE WHEN json_type(f.doc, t.p || '.ports') = 'array'
                     AND json_array_length(f.doc, t.p || '.ports') > 0
                    THEN f.doc
                    ELSE json_set(f.doc, t.p || '.ports', json((
                         SELECT json_group_array(json(v)) FROM (
                             SELECT json_set(je.value, '$.dir', 'in') AS v,
                                    0 AS g, je.key AS i
                               FROM json_each(f.doc, t.p || '.ins') je
                             UNION ALL
                             SELECT json_set(je.value, '$.dir', 'out'), 1, je.key
                               FROM json_each(f.doc, t.p || '.outs') je
                             ORDER BY g, i))))
               END,
               t.p || '.ins', t.p || '.outs')
      FROM fold f JOIN tgt t ON t.rid = f.rid AND t.k = f.k + 1
)
SELECT rid, doc FROM fold f
 WHERE f.k = (SELECT max(k) FROM tgt WHERE tgt.rid = f.rid);

UPDATE flow_designer
   SET graph = (SELECT doc FROM _ports140_docs WHERE rid = flow_designer.rowid)
 WHERE rowid IN (SELECT rid FROM _ports140_docs);

DROP TABLE _ports140_docs;

-- ── flow_designer_history ───────────────────────────────────────────────────
-- Same fold, same semantics. rev / node_count / edge_count / origin / ts are
-- untouched: the port model changed, the snapshot's identity did not.
CREATE TEMP TABLE _ports140_hist AS
WITH RECURSIVE
tgt(rid, p, k) AS (
    SELECT h.rowid, t.fullkey,
           ROW_NUMBER() OVER (PARTITION BY h.rowid ORDER BY t.fullkey)
      FROM flow_designer_history h,
           json_tree(CASE WHEN json_valid(h.graph) THEN h.graph ELSE '{}' END) t
     WHERE t.type = 'object'
       AND (json_type(t.value, '$.ins') IS NOT NULL
         OR json_type(t.value, '$.outs') IS NOT NULL)
),
fold(rid, k, doc) AS (
    SELECT rowid, 0, graph FROM flow_designer_history
     WHERE rowid IN (SELECT rid FROM tgt)
  UNION ALL
    SELECT f.rid, t.k,
           json_remove(
               CASE WHEN json_type(f.doc, t.p || '.ports') = 'array'
                     AND json_array_length(f.doc, t.p || '.ports') > 0
                    THEN f.doc
                    ELSE json_set(f.doc, t.p || '.ports', json((
                         SELECT json_group_array(json(v)) FROM (
                             SELECT json_set(je.value, '$.dir', 'in') AS v,
                                    0 AS g, je.key AS i
                               FROM json_each(f.doc, t.p || '.ins') je
                             UNION ALL
                             SELECT json_set(je.value, '$.dir', 'out'), 1, je.key
                               FROM json_each(f.doc, t.p || '.outs') je
                             ORDER BY g, i))))
               END,
               t.p || '.ins', t.p || '.outs')
      FROM fold f JOIN tgt t ON t.rid = f.rid AND t.k = f.k + 1
)
SELECT rid, doc FROM fold f
 WHERE f.k = (SELECT max(k) FROM tgt WHERE tgt.rid = f.rid);

UPDATE flow_designer_history
   SET graph = (SELECT doc FROM _ports140_hist WHERE rid = flow_designer_history.rowid)
 WHERE rowid IN (SELECT rid FROM _ports140_hist);

DROP TABLE _ports140_hist;
