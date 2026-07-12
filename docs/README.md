# Framework Development Docs

This directory documents the framework under `src/quant_core/`.

Do not store quantitative trading business knowledge, prompt references, strategy research notes, run logs, or generated reports here. Skill-specific input knowledge belongs under `.agents/skills/<skill-name>/references/`, independently runnable workflow scripts under `.agents/skills/<skill-name>/scripts/`, output templates under `.agents/skills/<skill-name>/assets/`, and generated artifacts under `.agents/skills/<skill-name>/outputs/`.

## Contents

- `architecture.md`: framework module boundaries and package layout.
- `skill_contract.md`: skill directory responsibilities, stage boundaries, and dependency rules.
- `strategy-research-loop.md`: goals, mechanisms, and implementation stages for automated strategy research.
