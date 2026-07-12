# Framework Development Docs

This directory documents the framework under `src/quant_core/`.

Do not store quantitative trading business knowledge, prompt references, strategy research notes, run logs, or generated reports here. Skill-specific input knowledge belongs under `.agents/skills/<skill-name>/references/`, independently runnable workflow scripts under `.agents/skills/<skill-name>/scripts/`, output templates under `.agents/skills/<skill-name>/assets/`, and generated artifacts under `.agents/skills/<skill-name>/outputs/`.

## Contents

- `architecture.md`: framework module boundaries and package layout.
- `skill_contract.md`: skill directory responsibilities, stage boundaries, and dependency rules.
- `loop-harness.md`: Harness goals, workflow, components, and implementation stages.
- `loop-harness-contracts.md`: task configuration and single-experiment result contracts.
