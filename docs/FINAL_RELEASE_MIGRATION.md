# Migration rehearsal — generated previous-version fixture

Selected migration-guard result: **PASS**. Release migration gate: **BLOCKER**;
the copy of the actual existing installation and full production startup are
**NOT RUN**.

`tools/verify_release_upgrade.py` uses the exact previous stable source
`91c7343324d38a1922bfed6486b4ef6dd7a3ba06` (0.14.184) in fresh subprocesses
to generate disposable persistent data. Current source is 0.14.232, runtime
`f858414c` (same runtime in `5ce4a78e`). All input/output copies are under
`~/homemind-final-release-20261106/upgrade184-3` on WSL ext4. No installed HA,
Recorder database, device, original user data or production token was accessed.

The fixture includes three roots in Control/Shadow/Paused, three Candidate
models and generation relations, raw target history, legacy feedback, Correct
and Undo journal rows, and existing Supervisor options. Models are actual
previous-code observation-contract policy 11/schema 12 with nonempty learned
heads, serialized checksums and read-only reconstruction/prediction checks.

After old-code restart the harness copies all data to a separate upgrade
directory, runs current Store/schema/model guards twice in separate processes,
and compares **every existing column and every row** in 16 protected tables.
Only operational `app_meta` and `events` are excluded. New columns are allowed;
protected identifiers, values, row counts, model bytes, modes and generation
relations must match exactly. `options.json` retains its exact SHA-256.
SQLite integrity checks pass. Both current restarts retained all six models
without marking agents for retraining or requiring Rebuild in this fixture.

The machine-readable receipt is
[FINAL_RELEASE_UPGRADE_FIXTURE.json](benchmarks/FINAL_RELEASE_UPGRADE_FIXTURE.json).
The raw archive includes all subprocess logs and failed initial attempts.

## Baseline preparation and preserved failures

Attempt 1 used a future fixture feedback timestamp and was correctly rejected
by the previous version's journal clock validation. The fixture now uses a
past timestamp relative to its process clock.

Attempt 2 compared immediately seeded qualified agents with missing training
cursors to a restarted database. It reported changed `agents` rows. Inspection
found only cursor/window backfill and progress 0→1. That same backfill already
exists in **0.14.184**. Attempt 3 explicitly restarts the old application first,
retains the old-startup changed-table list in the report, and uses its completed
restart state as the upgrade baseline. No protected column was removed and no
row-comparison requirement was relaxed to obtain PASS.

## Rollback scope

Reopening a separate upgraded copy with old code passed for this generated
fixture. This is diagnostic evidence only, not general downgrade compatibility.
Restoring the complete pre-upgrade data copy and old code also passed the exact
protected-row/options checks. Keep the complete-data restore procedure as the
approved recovery approach until actual installation-specific compatibility is
verified; see [upgrade and rollback procedure](FINAL_RELEASE_UPGRADE_AND_ROLLBACK.md).

Still **NOT RUN**: a copy of actual installation data, complete runtime installer
startup, active/pending training recovery, incompatible/legacy model matrix,
retention boundaries, disk pressure, WAL limits and storage faults. This rehearsal
does not close those requirements or physical Pi/HA acceptance.
