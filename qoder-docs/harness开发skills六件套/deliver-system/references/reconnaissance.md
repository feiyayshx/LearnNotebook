# M2 Reconnaissance Contract

Use this reference whenever the current build inspects or routes a project. M2 is read-only reconnaissance; it does not authorize implementation, test execution, OpenSpec mutations, or state advancement beyond `BASELINING`.

## Invocation

Resolve the script relative to `SKILL.md`, then run:

```text
python3 scripts/deliveryctl.py inspect --project-root <absolute-project-root> [--source <project-relative-source>]...
```

On Windows, invoke the same script with `python`; the `python3` launcher name is POSIX-only. Use `--route` only for an explicit user override. An override cannot bypass an existing, partial, invalid, or unfinished delivery control plane. Inspection persists only to stdout. Do not redirect output into the target project.

## Output contract

Require:

- `read_only: true` and `persistence: stdout-only`;
- `implemented_milestone: 2`;
- a bounded `scan` with limits, counts, warnings, and truncation reasons;
- a `route` containing recommended/selected route, override record, confidence, reason codes, and blocked status;
- capability metadata for Git, stacks, package managers, commands, testing, CI, documentation, OpenSpec, and Qoder;
- `adoption.strategy: adopt-not-replace`, evidence-linked stable preservation IDs, `status: draft`, and `verification_status: not_run`;
- source, Git, OpenSpec, evidence, and overall drift using only `unchanged`, `changed`, `missing`, `unknown`, or `not_applicable`;
- risks, an exact `next_action`, and a stable `report_digest`.

Do not treat a displayed list as a complete safety index: result lists may be capped, while internal counts and routing checks use complete observed sets.

## Safety and boundedness

The scanner uses deterministic ordering, does not follow symbolic links, skips control/VCS/dependency/build/cache directories, caps regular files, directories, total observed entries, depth, marker bytes, Git output, and displayed results. It classifies FIFOs, sockets, devices, and other non-regular, non-link entries as special entries and never opens them. Any symbolic link, observed special entry, scan warning, budget truncation, unsafe Git metadata, Git command failure, or ambiguous repository scope blocks progression. Special-entry counts cover the bounded observed set while displayed paths remain capped by `max_results`.

On very large projects the default budgets may truncate observation before routing evidence is complete. Raise the budgets explicitly with `--max-files`, `--max-directories`, `--max-depth`, `--max-marker-bytes`, or `--max-results`; never widen the scanner code itself, and keep treating any remaining truncation warning as a blocker.

Git queries must use the hardened runner. It resolves Git only from absolute PATH directories outside the target and rejects a final executable target that links back into the project. It disables optional locks, Hooks, fsmonitor, system/global configuration, trace output, external diff/text conversion, prompts, and dangerous inherited `GIT_*` variables. A repository attribute that can activate a content filter causes structured `unknown/error`; do not run `git status` or diff through that filter. Accept only full lowercase 40- or 64-character Git object IDs as baselines.

Never execute a discovered package script, inferred command, Hook, Skill, MCP server, OpenSpec CLI, test, browser, network call, or deployment command. Never expose configuration values. MCP server identifiers are opaque hashes; unknown Hook keys are counted rather than echoed.

## Route rules

- `resume`: any partial/invalid control plane, or a valid unfinished control plane. Integrity blockers cannot be overridden.
- `brownfield`: no unfinished control plane and meaningful product implementation exists, including static Web assets.
- `greenfield`: no product implementation was observed; a committed Spec/prototype does not by itself make a project Brownfield.
- a valid terminal `CLOSED` control plane routes a new request by observed product state, normally Brownfield.

If the scan or Git scope is incomplete, the route may retain a recommendation for explanation but must be `blocked: true`.

## Capability and preservation semantics

Command discovery is metadata only:

- `discovery: declared` means a project manifest explicitly defines the entry point;
- `discovery: inferred_candidate` means markers suggest a likely command, but authority is unresolved;
- `execution_status: not_run` means M2 has not verified the command.

Preservation drafts cover discovered source assets, existing test files, declared/candidate commands, CI, docs, OpenSpec config/specs/active Changes, Qoder Rules/Hooks/Skills/settings, and MCP configuration paths. These are not behavior-complete requirements. M3 must read the authoritative specs, tests, code, and user decisions to extract behavior-level Preservation Requirements and Characterization Tests before implementation.

## Resume drift

`unchanged` requires direct evidence. Missing source paths, invalid Git status, incomplete scan, hidden active Changes, unverified gate bindings, or unavailable environment/config/test-suite identity produce `unknown` or `missing`, never optimistic `unchanged`.

A passed historical gate is:

- `changed` when its Change, source digest, commit, or verified local artifact binding differs or is absent;
- `unknown` even when those bindings match, because M2 cannot reproduce the gate environment, configuration, and test-suite identity;
- never sufficient to unlock an M3+ or quality transition.

Inspection never repairs control data, invalidates evidence, transitions state, runs Smoke, or writes a Resume event. Report the blocker and exact next action instead.
