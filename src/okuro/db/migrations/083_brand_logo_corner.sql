-- 083_brand_logo_corner — the brand's DEFAULT logo corner on a prism/slides
-- deck. A deck inherits this unless it sets its own per-deck override (stored
-- in the prism doc JSON as ``logo_corner``). The logo module is sticky —
-- it stays in this corner as the deck scrolls/navigates.
--
-- logo_corner: tl | tr | bl | br  (top/bottom · left/right). Default 'tl'.
-- Constant default keeps ADD COLUMN safe; existing brands backfill to 'tl'.

ALTER TABLE brands ADD COLUMN logo_corner TEXT NOT NULL DEFAULT 'tl'
    CHECK (logo_corner IN ('tl', 'tr', 'bl', 'br'));
