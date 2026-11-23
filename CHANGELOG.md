# Changelog

## 0.2.0 - 2026-11-22

This release includes the development since `0.1.0`.

### Added

- Built-in `list_files`, `find_files`, and `search_for_text` tools for bounded,
  workspace-contained file discovery and literal text search.
- Literal Skill and Mod system-prompt section overrides.
- Long-running command support with configurable deadlines, cancellation, and
  bounded output capture.

### Changed

- Renamed the built-in editing tool from `apply_patch` to `edit_file`. The old
  name is not an executable alias; consumers must update tool permissions,
  prompts, and integrations to use `edit_file`.
- Improved command output handling for processes that produce large amounts of
  output.

### Compatibility

- Provider integration continues to use the existing `ModelAdapter` and
  `ModelResponse` contracts; no provider API change is included in this release.
- Historical completed `apply_patch` calls remain readable as session history,
  but new calls must use `edit_file`.
