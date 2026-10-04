# AGENTS.md

## Code style

- Put a clear module docstring at the top of Python files.
- Prefer useful docstrings on public classes and functions.
- Use plentiful comments where they improve readability or preserve important intent.
- When modifying code, actively notice places where a short comment would make ownership, control flow, invariants, lifecycle order, safety behavior, or non-obvious decisions easier to understand, and add one when useful.
- Comments should explain **why**, constraints, or architectural intent, not merely restate obvious code.
- Write comments with future coding agents in mind as well as human maintainers. When code has an important invariant, ownership boundary, lifecycle constraint, or tempting-but-wrong simplification, make that intent explicit near the relevant implementation.
- Preserve useful existing comments unless they are stale or incorrect.
- Prefer simple, explicit code over abstraction for its own sake.
- Keep architecture stupid simple and easy to inspect.

## Architecture

- `squid_core/` is the reusable runtime.
- Code Squid application-specific policy, defaults, presentation, and product behavior must remain outside reusable Squid Core.
- Preserve one-way dependency direction from the Code Squid application into Squid Core.
- Move code by ownership, not by its current folder name.
- Avoid new framework layers, factories, registries, compatibility shims, or subpackages unless they clearly simplify the system or satisfy a concrete requirement.
- Do not preserve legacy architecture solely because existing tests reference it.
- Keep subsystem boundaries easy to explain in plain language.

## Documentation

For every meaningful architecture, ownership, public API, resource-layout, runtime-boundary, or dependency-direction change:

- Update `docs/ARCHITECTURE.md`.
- Update `docs/dev-log.md`.
- Treat both documentation updates as part of patch completion, not optional cleanup.

`docs/ARCHITECTURE.md` should describe the **current implementation**:

- ownership
- dependency direction
- public seams
- runtime flow
- resource layout
- moves/renames
- important invariants

`docs/dev-log.md` should record:

- what changed
- why it changed
- verification performed
- intentionally deferred follow-ups
- notable decisions made during the patch

Additional documentation rules:

- Do not document future architecture as if it already exists.
- Preserve historical devlog entries instead of rewriting history.
- If code behavior or ownership changes and the docs would become stale, update them in the same patch.

## Verification

- Run focused tests for the changed subsystem first.
- Run the full test suite for architecture/refactor patches.
- Run relevant compilation and import checks.
- Run `git diff --check`.
- Run other existing repository checks when relevant.
- For structural/import-only changes, prefer lightweight construction smoke tests.
- Do not run live local or cloud model inference unless the patch changes runtime/model/provider behavior or the task explicitly requires it.
- Report exactly what was verified.
- Do not claim manual verification that was not actually performed.

## Scope discipline

- Do not add unrelated features during cleanup or refactor patches.
- Preserve behavior unless the patch explicitly intends to change it.
- If an adjacent issue is discovered, either fix it only when it is clearly within scope or record it as deferred.
- Do not silently expand a narrow patch into a redesign.
- Prefer deleting obsolete code over wrapping it in compatibility machinery when there are no real users requiring compatibility.

## Maintainability while editing

When working in an area of the repository:

- Leave it at least as understandable as you found it.
- If nearby code has a missing module docstring, misleading comment, stale ownership description, or an obvious non-obvious invariant that deserves explanation, clean it up when doing so is small and safe.
- Prefer small local comments that preserve architectural reasoning at the point where a future maintainer or coding agent would need it, rather than relying on distant documentation alone.
- Do not perform unrelated large-scale comment or documentation rewrites.
- Favor comments that help the next person understand why the code is shaped this way.