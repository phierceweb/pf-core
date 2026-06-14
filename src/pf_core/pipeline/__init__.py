"""Pipeline ergonomics — generic patterns for multi-stage pipelines.

Currently:
  - run_record: stamp <output_dir>/<filename> with resolved config + input hash + timestamps + counts.
  - baseline: snapshot output dirs for later comparison.
  - baseline_diff: structured diff between a baseline and current live output.
  - cache: stage-cascade cache invalidation with structural/content-keyed split.
  - resume: snapshot validity check + read for downstream-phase resume.
  - sequencer: run a contiguous slice of an ordered, named pipeline.

Promoted as a coherent group from a consumer document-extraction
pipeline's re-test ergonomics work. Pf-core's API parameterizes the
project-specific filenames / dir names / stage names so any consumer
with a multi-stage pipeline can use the same machinery.
"""
