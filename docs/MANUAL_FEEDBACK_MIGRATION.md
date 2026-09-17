# Stage 06 manual-feedback migration note

The stage-06 migration is additive. It introduces `manual_feedback_journal` and
`manual_feedback_effects` plus indexes. Existing tables, saved vectors, models, labels,
settings, generation lineage and rollback data are not rewritten.

Legacy Teaching and Teach-RL rows remain valid. New journal facts can link to newly-created
legacy-store labels so undo can retire their effects without reinterpreting older rows.
