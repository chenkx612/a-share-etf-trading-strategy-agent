# Skill Contract

Skills live under `.claude/skills/<skill-name>/` and are responsible for task-specific workflow, references, scripts, output templates, and generated outputs.

Recommended skill layout:

```text
.claude/skills/<skill-name>/
  SKILL.md
  references/
  assets/
  scripts/
  outputs/
```

## Responsibilities

- `SKILL.md`: operational contract for the skill. It should describe the SOP, stage boundaries, command examples, required inputs, durable outputs, and final response format.
- `references/`: inputs to the skill, including prompt references, domain notes, business knowledge, migrated task manuals, default pools, static universes, and other reference data.
- `assets/`: output templates or reusable files copied into generated outputs. Do not store input knowledge, default pools, or base universes here.
- `scripts/`: independently runnable scripts with their own CLI entry points. If a Python file is only a helper imported by another script and is not useful as a standalone command, it should live in framework code or be folded into the caller instead of being exposed as a skill script.
- `outputs/`: generated data, reports, logs, and intermediate artifacts. Stage outputs that are meant to be consumed by later stages belong here.

## Stage Design

Skills may be split into multiple stages when the workflow contains logically different decisions or execution phases.

Each stage should have one clear responsibility and a concrete file boundary:

- Stage inputs come from user arguments, `references/`, or previous stage artifacts in `outputs/`.
- Stage outputs are written to `outputs/` with stable names and documented schemas when later stages need to read them.
- Later stages should read prior outputs instead of recomputing or implicitly invoking earlier stages.
- Human or AI review steps should be explicit stages when they change downstream behavior.
- A script for one stage should not hide execution of a different stage unless the skill explicitly defines that script as an end-to-end wrapper.

For example, a candidate discovery stage can write a ranked shortlist to `outputs/`; an AI review stage can choose reviewed candidates from that shortlist; a full automation stage can then read the reviewed candidates and continue without re-running discovery.

## Rules

- Skill scripts may import `quant_core` from `src/` or from an editable install.
- Skill scripts should pass `--root .claude/skills/<skill-name>/outputs/<run-root>` to `quant_core` CLI commands.
- Generated `outputs/` content is ignored by default.
- Reusable framework behavior belongs in `src/quant_core/`, not in skill scripts.
- Business knowledge, SOP composition, default pools, candidate-selection rules, prompt references, and output schemas belong in the skill, not in `src/quant_core/`.
- Keep `outputs/` disposable unless the skill documents a durable artifact. Durable files should be listed in `SKILL.md`.
