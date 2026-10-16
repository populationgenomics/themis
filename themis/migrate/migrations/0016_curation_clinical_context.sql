-- 0016_curation_clinical_context.sql -- the clinical context a registration states
-- (curation-surface.md). Nullable because the registrations made before it have none: the stored
-- case notes are one curator's reading rather than the question, so each is written by hand. Its
-- checks mirror the surface's, since a value written by hand never passes through the surface. The
-- bound is a safeguard against storing a blob, and it is the 10 000 of
-- themis.workbench.models.workbench.VariantClassificationInputs.clinical_context; whether a run's
-- composed context, the entity statement and this text, fits is checked when a round builds it.
ALTER TABLE curation.variants ADD COLUMN clinical_context TEXT
    CHECK (clinical_context ~ '\S' AND char_length(clinical_context) <= 10000);
