# Development log

- 2026-10-04: Completed the pre-release workspace-root metadata rename.
  Session/undo metadata writes workspace_root; undo restore and the Code Squid
  session viewer read that key. Updated current fixtures, schema assertions,
  viewer output, README wording, and architecture docs. The undo index has no
  root field and keeps its existing file/snapshot schema. No aliases, old-key
  fallback, dual writes, migrations, or warnings were added. Earlier development
  metadata may be incompatible; paths, containment, replay, and undo lifecycle
  behavior are unchanged. Historical entries remain as written.
  Verification: Core storage/index focused suite: 62 passed; Code Squid session
  index/viewer/CLI focused suite: 62 passed. Full Core: 781 passed, 1 skipped
  (POSIX process-group regression on Windows); full Code Squid: 779 passed.
  Compilation and serialization/reopening/index/restore smoke checks passed,
  including rejection of legacy-only undo root metadata. git diff --check passed
  in both repositories. Production/API/context/storage and current docs/tests
  contain no old root identifier; remaining occurrences are historical dev-log
  prose only. No live inference was run; no follow-up migration is planned.


- 2026-10-04: Finished the narrow Mod/Skill genericity cleanup. Renamed generic
  runtime/API parameters, Session and ToolExecutor attributes, Hook contexts,
  saved-tool contexts, and run-started root fields from repo_root to workspace_root.
  Updated Code Squid consumers and tests without compatibility aliases. Kept the
  persisted session/undo repo_root metadata key, paths, containment, and storage
  behavior unchanged. Standalone Skills now restrict Agent exposure and default
  executor construction just like Mod Skills: omitted lists retain defaults,
  explicit empty lists disable tools, and named lists only select available tools.
  Unavailable names fail instead of adding tools; already permitted registries
  retain identity. Mod discovery/loading now receive built_in_tools and validate
  against the same registry as resolution, including explicit empty registries.
  Made shell verification wording and stale Core comments task-neutral.
  Verification: focused Core Skill/Mod/API/context/executor/storage checks: 272
  passed; focused Code Squid integration: 105 passed. Full Core: 781 passed,
  1 skipped (POSIX process-group test on Windows); full Code Squid: 779 passed.
  Compilation and import/signature/context construction smoke checks passed.
  git diff --check passed in both repositories. No live inference was run.
  An obsolete shell-description expectation and incomplete application test
  double were updated. Reader registration, tool safety, resource precedence,
  provider/Hook architecture, prompt sections/semantics, accent metadata, and
  packaging were unchanged; broader audit follow-ups remain deferred.



- 2026-10-04: Made Core prompt/runtime defaults generic while retaining Code
  Squid behavior in the sibling application. `tools=None` now selects built-ins
  consistently in Agent, ToolExecutor, and both spawner helpers; empty mappings
  select no tools and explicit mappings remain the same object without merging.
  Renamed the pre-release identity section to AGENT_IDENTITY and the defaults
  field to agent_identity; updated current code, tests, and contract docs without
  an alias. The three fixed sections remain AGENT_IDENTITY, TOOL_USAGE_GUIDANCE,
  and FINAL_SUMMARY_REQUIREMENTS with literal append/replace semantics.
  Moved narration, coding examples, and source/shell-discovery preferences into
  Code Squid's non-replaceable baseline policy. Its existing reading/editing
  guidance and concise final-report policy remain application-owned and retain
  their Skill override behavior. Core protects only response protocol,
  batching/result-dependent sequencing, completion, verification honesty,
  task-neutral examples, and malformed/empty-response correction. Environment
  guidance receives the resolved registry and mentions built-in capabilities
  only when active. Read/trim markers and their recognition use SQUID CORE.
  Compatibility impact: identity consumers must adopt the new names; callers
  relying on the spawner's old no-tools default must pass tools={}; historical
  branded omission markers are not recognized as the current internal format.
  Persisted history is not rewritten. No released compatibility need was found.
  Verification: Core focused prompt/spawner/Skill/Mod/environment/trimming/tool
  tests: 196 passed, 1 skipped; minimal-provider fixture checks: 6 passed.
  Core full suite: 768 passed, 1 skipped. Code Squid focused coding prompt,
  Core integration, Mod and Skill runner checks: 60 passed. Code Squid full
  suite after the final policy move: 779 passed. The Core skip is the POSIX
  process-group regression on Windows. Compilation and
  import/construction smoke checks passed for both packages; git diff --check
  passed in both repositories. No live model inference was run.
  Provider contracts/structure, hooks, optional reader dependencies, Mod accent
  validation, packaging, and please_stop_contemplating_the_void were untouched;
  Mod color ownership remains deferred to the broader Mods/Skills design pass.


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

- 2026-10-04: Polished the existing single provider contract for standalone
  integration. Traced BaseProvider, ModelAdapter, construction/injection, every
  Core provider call and the associated tests before editing. Normal inference
  already required only create_response(model, messages, tools); neither
  subclassing nor readiness, discovery, streaming, vision or cancellation
  methods were required. Kept that minimum and existing keyword request shape
  instead of adding a request wrapper, alternate callable API or registry.
  Clarified BaseProvider's optional defaults and host-only operations, corrected
  the adapter's ownership comment and added a minimal integration README example.
  All completed transports now explicitly require ModelResponse at the shared
  adapter boundary, with a clear malformed_response ModelRequestError for invalid
  outputs. Response lookalikes previously slipped through attribute-based
  handling; third-party providers returning them must translate to ModelResponse.
  Existing squid-providers implementations need no changes.
  Added regressions for plain/minimal BaseProvider objects, normal and text
  inference, streaming fallback, the default agent loop with streamed display
  enabled/disabled, and invalid response contracts across all transport paths.
  Updated architecture docs; retained Core ownership of response contracts and
  tool validation. Readiness/discovery remain explicitly unimplemented in the
  base rather than fabricating backend capabilities. Optional transport controls
  and streaming signatures are deliberately unchanged; hooks, retries, replay,
  sessions and tool validation semantics were not redesigned.
  Verification: focused provider/adapter/generation/spawner/boundary tests
  103 passed; full Core suite 865 passed, 1 skipped; sibling Providers suite
  139 passed. Python compileall and git diff --check passed. Built and installed
  Core with its declared dependencies into a fresh isolated environment; outside
  the checkout, confirmed application, squid-providers, OpenAI, Anthropic,
  Ollama and httpx modules were absent and explicitly blocked their imports.
  Installed spawner/generation imports, minimal BaseProvider inference, invalid
  response rejection and pip check passed. No live inference was performed.

- 2026-10-04: Extracted behavioral hooks into Code Squid. Moved
  hooks/no_progress.py (RepeatedFailureNudge), too_many_reads.py and
  post_verification.py plus focused and behavioral loop integration tests to
  code_squid/hooks/ and Code Squid tests. Deleted redundant_read.py and its
  unit/integration tests, removing RepeatedReadNudge rather than retaining a
  compatibility wrapper. Core's hook exports now contain generic mechanisms
  only. HookRunner, loader, contexts, transformations and all dispatch paths
  are unchanged. Retained compaction_applied and read_result_trimmed as public
  host notification contracts; generic compaction dispatch/persistence tests
  remain, and host-only read-visibility tests cover notification order and
  request trimming without modifying canonical source.
  Code Squid still enables TooManyReadsNudge and PostVerificationNudge and
  disables RepeatedFailureNudge. Removed the repeated_read configuration key.
  Compatibility: concrete hook/constant imports move to code_squid.hooks;
  deleted-hook imports and old repeated_read settings are unsupported.
  Verification: focused Core hook/loader/loop/trimming/boundary tests 185
  passed; full Core 734 passed, 1 platform skip. Focused Code Squid hook/
  composition/settings/boundary tests 207 passed; full Code Squid 777 passed;
  Providers image integration 4 passed and full Providers 139 passed.
  compileall passed across source/tests in all three repositories. Fresh-process
  blocked-import tests confirm Core imports and host hook execution without
  Code Squid or transports. AST comparison confirms moved behavior is unchanged
  apart from imports; production source checks confirm no application imports
  or behavioral hook classes in Core and no deleted-hook references anywhere
  in the three production packages. Application file-tree generation and
  git diff --check passed. No live inference. No follow-up implementation
  deferred; package release and commits remain outside this patch.

- 2026-10-04: Made the generic command-environment instruction conditional on
  `run_command` in the active registry. OS, preferred shell, path separator,
  and workspace facts remain unconditional when environment context is supplied;
  existing Windows and non-Windows command guidance is unchanged. Extended
  prompt regressions for default built-ins, empty registries, explicit tools
  without command execution, and explicit command tools on both platforms.
  Verification: focused prompt/platform tests 38 passed; full Core suite 769
  passed, 1 skipped. Source/test compileall, prompt imports, and git diff --check
  passed. No live inference or deferred follow-ups.

- 2026-10-04: Completed the final pre-v0.1 compatibility cleanup. Removed
  streaming-provider signature inspection and silent omission of
  on_reasoning_delta, compaction adapter signature inspection and silent
  omission of configured should_cancel, and persisted reasoning_content/
  thinking fallback reads. Streaming callbacks now use the current provider
  contract; compaction uses an ordinary call without cancellation and passes
  the configured callback otherwise. Updated compaction/generation test doubles,
  fixed the transcript's diagnostic raw_tool_calls fixture to flat records, and
  retained explicit nested model-message coverage. Request observation keeps
  its capability checks; only its historical comment changed.
  Code Squid removed its unused CodingAgent alias/module and uses Core Agent
  directly in the affected tests. No current production caller used the alias.
  No aliases, migrations, or fallback paths were added. Obsolete pre-release
  provider/adapter signatures intentionally fail when supplied current callbacks.
  Verification: focused model adapter, compaction, transcript, spawner, and
  generation tests 159 passed; full Core 791 passed, 1 skipped. Focused Code
  Squid Agent/runner/import-boundary tests 77 passed; full application 778 passed.
  Source/test compileall and fresh-process Core imports/Agent construction passed;
  application imports/construction and direct Core Agent wiring passed.
  git diff --check passed in both repositories. No live inference was run.
  No implementation follow-ups deferred; provider/settings migrations, package
  metadata, and unrelated architecture remain outside this patch.


- 2026-10-04: Made the v0.1 base package require zero third-party Python runtime
  dependencies. Set project.dependencies to [] and moved epub-utils==0.1.0a1,
  odfpy, openpyxl, python-docx, Pillow, and PyMuPDF into the single documents
  extra. Package name/version, Python minimum, build requirements, and reader
  ownership/static suffix dispatch remain unchanged. Moved the sole eager
  third-party runtime import (PIL Image/UnidentifiedImageError in image_content)
  into inspect_image_bytes. EPUB, ODS, XLSX, DOCX, and PDF readers now separate
  optional import failures from malformed-file errors, returning the existing
  tool failure shape with pip install "squid-core[documents]" guidance. Pillow
  absence raises ImageValidationError with that same guidance, including through
  session generated-image validation; installed-library integrity, decompression,
  container, and range checks remain intact. Optional parser fixture imports now
  skip at use sites; parser-independent tests still run, using stdlib ZIP fixtures
  for container/selector checks. Added fresh-process blocked-import, packaging,
  plain-text/session storage, every known optional suffix, and image/generated
  image failure coverage. Updated README installation notes and architecture docs.
  Verification: focused optional-dependency/reader/document/image suite 80 passed;
  full current-environment Core suite 809 passed, 1 platform skip. Full suite with
  all six optional dependency roots (and the legacy fitz alias) blocked: 761
  passed, 49 skipped. Built a wheel and verified all six Requires-Dist entries
  are conditional on the documents extra, with no unconditional requirements.
  Installed that wheel into a fresh temporary environment containing only pip
  and squid-core; outside the checkout, verified absent optional modules, Core/
  Agent/spawner/built-in imports, Agent construction, schemas, plain-text reading,
  and pip check. Production AST import inventory found only the six optional
  roots beyond stdlib/Core, all lazy. Source/test compileall and git diff --check
  passed. No live model inference. No implementation follow-ups deferred;
  publication and the broader README/license cleanup remain outside this patch.

- 2026-10-05: Verified Providers finding #3 without changing production code.
  The shared classifier already traverses causes and unsuppressed contexts and
  recognizes wrapped urllib HTTPError status through its generic status lookup.
  Added 12 direct public-contract regressions covering explicit cause and implicit
  context over six HTTP statuses. Providers' real-path regressions separately
  verify urllib body retention in safe messages and httpx response metadata,
  without adding a transport dependency or Ollama policy to Core. Documented the
  existing chain limit and metadata contract. Verification: focused classifier/
  adapter 75 passed; full Core 821 passed, 1 skipped; focused Ollama 88 passed;
  full Providers 197 passed. Compilation and git diff --check passed in both
  repositories. No live inference; no production correction or follow-up needed.
