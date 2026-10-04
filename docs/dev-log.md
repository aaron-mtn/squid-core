# Development log


- 2026-10-04: Finished the physical repository split and remaining provider
  catalog/config ownership after the earlier staged extraction. Initialized
  sibling squid-core and squid-providers Git repositories; moved runtime,
  transports, packaging and tests by ownership without keeping application-local
  source copies or compatibility re-exports. Git history and the earlier staged
  patch remain in Code Squid; no commits or remote publication were performed.
  ProviderProfiles, IDs/names/defaults, constructor choice, readiness/discovery,
  replay policy, credential variables, supported scoped config, shared transport
  control validation and legacy config interpretation now live in Providers.
  Code Squid consumes the catalog, persists arbitrary supported scoped overrides,
  applies its environment overrides and preserves model-selection UX. Removed
  application URL/key defaults and the named native timeout field; timeout is
  providers.<id>.request_timeout in provider-owned config. No concrete provider
  IDs/classes remain in production Code Squid or Core. Generic Core contracts,
  injected adapter/provider precedence, normalized responses, tracing ownership
  and cancellation semantics remain unchanged.
  Each repository now builds its own distribution. Core has generic reader
  dependencies only; Providers adds OpenAI/httpx; Code Squid depends on both
  local packages plus terminal dependencies. Requirements supply sibling editable
  paths together, avoiding lookup of unpublished packages. Updated README and
  architecture docs in all repositories; provider-extraction.md records moves.
  Split mixed agent/spawner/Mod/PDF/image tests; moved backend construction,
  transport/config/vision tests to Providers and generic runtime tests to Core.
  Retired old-path-only checks and added current import/source guards.
  Verification: focused Core (231), Providers (125) and corrected application
  settings/boundary/construction (120) checks passed during implementation.
  Final full suites in a fresh environment with sibling editable installs:
  Code Squid 706 passed; Core 847 passed, 1 platform skip; Providers 139 passed
  (1,692 passed total). Compilation and file-tree generation passed. All three
  wheels built and their contents/dependencies were inspected; they contain only
  their intended packages, without tests/bytecode. Installed all wheels together
  and imported them outside the checkout, constructed all catalog providers
  without requests, ran pip check and installed CLI --help. A separate Core-only
  wheel environment has no application, Providers, OpenAI or httpx and imported
  spawner/generation with those dependencies explicitly blocked. Fresh-process
  guards verify Providers imports only the two allowed Core modules with Code
  Squid blocked; source guards/searches verify no provider IDs/classes in Core
  or Code Squid. git diff --check passed in each repository; the complete
  application working-tree diff against HEAD also passed. The prior staged
  snapshot was left intact. No live inference was performed.
  Wheel inspection detected stale Core files in the old app build cache. Moved
  generated build/old distribution metadata to a temp archive and rebuilt cleanly;
  deleting those generated artifacts had been rejected by automatic review with
  the reason "blocked by policy". Deferred only commits, remote publication and
  package releases; no architecture or runtime work remains deferred.
