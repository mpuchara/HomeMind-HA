# Audit of the 0.8.0 baseline

Base: origin/main 8b3fdbc, release 0.8.0. Audited before implementation on 2026-09-12.

The runtime has two Python files, four static UI assets and a shell launcher. Docker uses Python 3.13 Alpine and websockets; SQLite is the only persistent database. No baseline automated tests were shipped. The temporary extraction workflow still references a removed encoded runtime and an old branch; replace it with validation, without write permissions.

| Responsibility | 0.8 implementation | Decision for 0.9 |
|---|---|---|
| Settings, migration | main.py defaults/options, Store additive columns | Preserve settings; add explicit model compatibility migration |
| Archive | Store.entity_history, archive_iter fetchmany | Keep raw data, use streaming in every replay path |
| Policies | DiagonalLinUCB, MultiHorizonPolicy, explicit schema v10 | Keep mathematical learner; add backend contract and drift |
| Context | broad candidate selection, history edge scores | Preserve ESPHome sensor exception and unit exclusion |
| Rooms | entity area only | Add device/area registries and explicit fallback mapping |
| Training | manual Train/Resume/Rebuild; one agent worker by default | Keep manual lifecycle; share one job slot with home bootstrap |
| Replay | streaming screening, then full selected-row list and as-of timeline | Replace large replay/held-out arrays with disk-backed as-of queries/spool |
| Discovery | automatic target discovery, paused agents | Keep WAITING until Train; prohibit restart replay |
| Modes | qualification >78%, Shadow, Control, Paused | Keep qualification and prohibit services in Shadow including Verify |
| Execution | Engine.process_agent combines prediction, gates and HTTP | Introduce immutable ActionIntent and separate Executor |
| Timing | control.py domain acknowledgement/settling/manual profiles | Preserve domain-specific timing and hardware quantization |
| Manual changes | explicit user context, expected echo matching, persisted hold | Preserve, tighten command correlation and startup observation |
| Takeover | disables known target automations and confirms OFF | Move all services into Executor; retain stop_actions |
| Rewards | delayed weak acceptance, manual correction, historical dwell reward | Add bounded, separately observable anticipation reward components |
| UI | agent diagnostics, benchmark, manual controls | Add compact home intelligence and intent diagnostics |

Important limits found: no shared trajectory model, no model/intent/context version boundary, no drift, no architectural service-boundary test. Selected replay rows, fast edge rows and deferred validation vectors can grow with archive size. REST polling is already independent of per-target queues, but skipped events during an in-flight request need a fresh wakeup. Startup temporal priming scans archive and is unnecessary for the new no-replay startup contract.

Implementation order: split existing responsibilities without replacing device adapters; implement pure home model, context, intents, rewards, telemetry/job coordination; connect policy and executor; convert replay to bounded memory; migrate and expose UI/API; test real policy anticipation, migration, race/gate handling and streaming; package source add-on ZIP. Hardware thresholds remain targets until measured on Raspberry Pi 4.
