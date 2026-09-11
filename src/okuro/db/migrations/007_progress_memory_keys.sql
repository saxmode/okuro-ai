-- Link progress entries to memory records by topic or id.
-- Stored as a JSON array of strings (topic names or memory UUIDs).
ALTER TABLE progress ADD COLUMN memory_keys TEXT DEFAULT '[]';
