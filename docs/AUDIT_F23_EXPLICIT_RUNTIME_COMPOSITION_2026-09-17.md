# F23 — explicit runtime composition, stage 1

Date: 2026-09-17

## Scope

This is deliberately a small refactor with no intended behaviour change. It does **not** rewrite the full runtime. It first characterizes the shipped composition, then replaces the two highest-risk cross-cutting mechanisms requested by F23:

1. opaque Candidate promotion booleans become an ordered list of named validation results;
2. final-stack Candidate promotion and core manual-feedback POST endpoints are owned by one explicit route registry instead of traversing several feature-specific `Handler.do_POST` wrappers.

Other overlays are retained as compatibility fallbacks and are listed below for later stages.

## Shipped startup path

The image starts exactly this chain:

`Dockerfile CMD -> /app/run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> main.py`

`run.sh` still executes `trial_queue_main.py`. Stage 16 does not change the external add-on entrypoint.

The important change is inside `trial_queue_main.py`: it no longer contains its own hand-written `prepare_engine_extensions` wrapper. It binds `RuntimeCompositionRoot` once. The root invokes the already-characterized preference/fast base stack and then explicitly attaches Stage 11, 13, 14, 15 and the new Stage-16 contracts.

## Characterization before refactor

The final stack had several kinds of composition:

| Area | Existing mechanism | Risk / status after this PR |
|---|---|---|
| `queue_main` | replaces `initialize_runtime`, `shutdown_runtime`, `Handler.status_payload`, `do_GET/POST/PATCH/DELETE` | retained compatibility layer; later stage |
| `fast_queue_main` | explicit named engine-extension hooks plus several install functions | named hooks retained; preferred pattern |
| `preference_queue_main` | replaces `core.prepare_engine_extensions` and temporarily registers fast-stack hooks | retained as characterized base composition |
| `trial_queue_main` | replaced `core.prepare_engine_extensions` again | **removed from final entrypoint; replaced by `RuntimeCompositionRoot`** |
| Candidate manager | wraps `engine.process_agent`; legacy feedback method hooks; installs candidate HTTP wrappers | process observation retained; final promotion HTTP bypassed by explicit router; feedback listener in final stack already explicit |
| Candidate promotion extensions | `atomic_promote` and `user_promotion` wrap Candidate methods and `Handler.do_POST` | business methods retained; final HTTP ownership moved to named routes |
| Candidate preference/confidence extensions | wrap `_comparison_summary`, `status` and UI/static methods | metric decoration retained; final promotion decision is projected through named validation contract |
| manual feedback | legacy `Handler.do_POST` wrapper; physical-equivalence process wrapper | physical-equivalence retained; final core feedback POST ownership moved to named routes |
| Candidate store/history isolation | class-level Store / HistoryManager overlays | retained and explicitly called out as global mutable compatibility debt |
| UI feature modules | several `do_GET` / `static` wrappers | retained; later stage |

`tests/test_runtime_composition_f23.py` additionally scans the actual source AST for `install*` / `bind*` functions which assign object methods or attributes and emits `F23_OVERLAY_MAP` in CI. That is the mechanical characterization used to prevent this document from becoming the only source of truth.

## Single final composition root

`runtime_composition.py` owns the final binding and declares contracts for:

- **context** — `engine.context`;
- **policy** — `engine.policy` + `engine.decision_composer` before `ActionIntent`;
- **feedback** — `engine.manual_feedback_journal` plus explicit Candidate listener;
- **episode evaluation** — `engine.episode_evaluator` observer contract;
- **candidates** — isolated `engine.agent_candidates` Shadow generations;
- **promotion gates** — `manager.promotion_validation_service`;
- **execution** — existing `engine.executor`, still the only physical HA dispatch boundary.

The root injects a clock, repository (`STORE`) and HTTP transport registry into the new Stage-16 services. Those services have no module-global mutable registry.

Repeated `bind_final_composition()` returns the same root. Repeated preparation for the same Engine instance is ignored, so the final root cannot install the migrated handling twice.

## Named promotion validation

### Before

Different layers could calculate or wrap `promotable` while also decorating various gate dictionaries. Correctness depended partly on wrapper order and on each later layer remembering not to weaken an earlier veto.

### After

The final source of truth is:

`comparison.promotion_validations[]`

Each item contains at least:

- `name`;
- raw `passed`;
- `effective_passed`;
- `reason`;
- `custom_override` policy;
- `observed` evidence;
- `source`.

`promotable` remains in the public API for compatibility but is derived from this list in the final composition.

Existing Stage-07 named gates are projected into the list. Stage-13 `independent_final_evaluation` therefore remains a named `custom_override=never` veto. Legacy/non-fast Candidate summaries which do not yet expose named gates are decomposed into equivalent named checks for freshness, action coverage, quality regression, false-early safety and execution prerequisites.

Duplicate validation names compose with logical AND. A module installed later cannot erase a failed earlier result merely by returning a pass with the same name.

Custom promotion keeps its existing behaviour: a user may waive only a gate whose pre-existing contract is not `never`. The raw failed result remains visible and is marked as explicitly waived; hard safety vetoes remain effective.

## Explicit HTTP transport

`ExplicitRouteRegistry` is instance-owned and keyed by `(HTTP method, route name)`.

Registration is idempotent: registering the same named route replaces it in place rather than stacking another `do_POST` wrapper. Dispatch order is stable by priority and registration sequence.

The final root installs one dispatcher after the characterized compatibility stack, then registers these routes explicitly:

### Feedback

- `feedback.manual-correction`
- `feedback.teach-desired`
- `feedback.teaching`
- `feedback.undo-teaching`
- `feedback.manual-feedback`
- `feedback.undo-feedback`

### Promotion

- `promotion.target_mode`
- `promotion.standard`
- `promotion.custom`

The route callbacks call the same existing domain functions / Candidate methods, use the same trusted-client and runtime requirements, and preserve response status semantics. For these paths the final dispatcher handles the request before the historical wrapper chain, so the old wrappers are compatibility fallback rather than the active owner.

Unmigrated routes still fall through to the exact handler chain that existed before Stage 16.

## Replaced overlays in this stage

| Previous final-stack mechanism | Stage-16 replacement |
|---|---|
| `trial_queue_main` manually wrapping `core.prepare_engine_extensions` | `RuntimeCompositionRoot.prepare_engine_extensions` |
| promotion outcome treated as an authoritative opaque `promotable` bool | ordered `promotion_validations[]`; bool is compatibility projection |
| final request path traversing Candidate/base + atomic + custom `do_POST` promotion wrappers | `promotion.*` named routes in `ExplicitRouteRegistry` |
| final request path traversing legacy manual-feedback `do_POST` wrapper | `feedback.*` named routes in `ExplicitRouteRegistry` |

The legacy module wrappers are intentionally not deleted yet because individual module tests and alternative entrypoints may still depend on them. They are no longer the owner for migrated routes in the shipped final composition. Removing those fallbacks can be done in a later PR after module-level consumers are migrated.

## Remaining composition debt

The next safe stages are:

1. migrate queue/Teach-RL and Candidate lifecycle HTTP endpoints into the same route registry, then remove their handler wrappers;
2. replace Candidate `engine.process_agent` wrapping with explicit before/after observer contracts;
3. move Store/History candidate isolation away from class-level mutation into injected repositories/views;
4. migrate GET/static UI injection to an asset registry;
5. only then simplify `preference_queue_main -> fast_queue_main -> queue_main` into direct root-owned installers.

The physical `ActionIntent -> Executor` boundary is not part of this refactor.

## Safety / behaviour invariants

- Candidate remains physical Shadow until atomic promotion commits.
- Stage-13 independent future evaluation remains a non-overridable promotion veto.
- User custom promotion cannot waive `custom_override=never` results.
- Executor remains the only AI physical dispatch boundary.
- Public endpoints and response formats are preserved.
- Existing SQLite migrations/data, Candidate lineage, TrialRecords, labels and rollback snapshots are untouched.
- No threshold was relaxed.

## Tests

Stage-16 tests cover:

- shipped image/entrypoint reaches exactly the characterized composition root;
- final root declares context, policy, feedback, episode, Candidate, promotion and execution contracts;
- repeated route installation does not stack handling;
- same named route replaces in place;
- two independent route registries do not affect each other;
- unmatched routes still fall through to the previous handler chain;
- duplicate named promotion results compose with AND in either module order;
- custom evidence may be explicitly waived while a hard veto cannot;
- legacy Candidate bool is decomposed into named validation results;
- Candidate Shadow invariant remains present;
- AST characterization exposes remaining install-time method/function overlays for subsequent PRs.

## v2 hardening on the current runtime stack

Stage 16 was already merged in PR #75 and still owns the final composition root, named promotion validation and explicit feedback/promotion routes. Re-review on the Stage-15-v2 stack found one isolation bug in the transport binding rather than a policy/composition-order bug.

### Shared Handler class leak

`ExplicitRouteRegistry` itself was instance-owned, but the original `install_dispatch()` stored the active registry on the shared `Handler` class. If two runtime/test instances reused that base class, the second installation could replace `_explicit_http_route_registry` for the first instance. The old unit test compared two registries directly and therefore did not exercise the actual installed dispatcher.

Version 2 makes the shipped transport owner the concrete `ThreadingHTTPServer` instance:

- `main.main()` exposes the active server as `HTTP_SERVER` before the background runtime composition starts;
- `install_dispatch()` creates a small per-server Handler subclass and assigns it to `server.RequestHandlerClass`;
- the route registry is stored on the concrete server instance;
- the shared `main.Handler` class is not modified in the shipped runtime;
- two servers may therefore share one base Handler class while keeping completely separate route registries;
- repeated installation for one server reuses the same subclass and registry, so wrappers do not stack;
- requests that were already accepted before runtime composition continue through the original compatibility Handler, which is safe because explicit feature routes require the runtime to be ready;
- entrypoints/tests with no exposed server retain the previous class-binding path as a compatibility fallback only.

This is intentionally not a rewrite of `main.py` or the HTTP stack. The existing server still starts before heavy runtime initialization, preserving the UI/startup lifeline.

### Current tested startup chain

`Dockerfile -> run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> main.py`

The Stage-16 characterization test now also scans `prepare_*` hooks, so historical `preference/fast/queue` composition mutations cannot disappear from the overlay map merely because they are not named `install_*`.

### Replaced contract

| Previous mechanism | v2 replacement |
|---|---|
| active explicit registry stored on shared `Handler` class | registry owned by concrete HTTP server + per-server Handler subclass |
| route-isolation test exercised registries only | test installs two runtimes sharing one base Handler and proves independent dispatch |
| overlay AST map scanned only `install*` / `bind*` | map also scans `prepare_*` composition hooks |

Promotion validation, feedback domain functions, queue semantics, Candidate Shadow isolation, ActionIntent, Executor and all persistence formats are unchanged.