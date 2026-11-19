# SkillBridge Pipeline

The public code follows the complete experiment flow.

## Pipeline stages

- `pipeline/skillcomptest/scripts/` handles corpus preparation, cross-OS sampling,
  static analysis, benchmark execution, review-batch preparation, result
  extraction, and validation.
- `pipeline/compa_skill/scripts/` handles repair issue construction, repair-agent
  orchestration, replay planning, delivery checks, and repair tracking.
- `core/` contains reusable cross-OS dataset, static-evaluation, and
  reviewed-label/inventory analysis modules.
- `repair/` contains repair-cohort and before/after replay analysis modules.
- `tests/` contains focused checks for the reusable analysis modules.

The released observations and replay records are under `data/raw/`. The
pipeline entry points accept source, runner, and output paths and retain the
original workspace defaults used during the study. Raw skill-package bundles,
execution archives, private databases, credentials, and agent transcripts are
kept outside the Git working tree. Paper-specific summary tables and figure
renderers are not part of this code package.
