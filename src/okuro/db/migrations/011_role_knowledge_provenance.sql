-- <!-- AGENT_HEADER
-- role: code
-- purpose: 011_role_knowledge_provenance — provenance + decay columns for role_knowledge
-- index: content
-- AGENT_HEADER_END -->
-- Enables the role self-maintenance loop (weekly domain research).
-- Entries written by roles_learn can now carry origin (source_url), session link,
-- confidence for decay, last_accessed for usage tracking, and supersedes for
-- replacement chains — mirroring the agent_memory pattern.

ALTER TABLE role_knowledge ADD COLUMN source_url    TEXT;
ALTER TABLE role_knowledge ADD COLUMN session_id    TEXT;
ALTER TABLE role_knowledge ADD COLUMN confidence    REAL DEFAULT 0.7;
ALTER TABLE role_knowledge ADD COLUMN last_accessed TEXT;
ALTER TABLE role_knowledge ADD COLUMN supersedes    TEXT REFERENCES role_knowledge(id);

CREATE INDEX IF NOT EXISTS idx_role_knowledge_role
    ON role_knowledge(role_id);
CREATE INDEX IF NOT EXISTS idx_role_knowledge_last_accessed
    ON role_knowledge(last_accessed);
CREATE INDEX IF NOT EXISTS idx_role_knowledge_confidence
    ON role_knowledge(confidence);
