# FulfillFlow benchmark harness — Phase A

This directory defines reproducible data and workload artifacts. It does not contain an official
`v1-baseline` campaign or official results. Creating host-specific values and executing the five
official repetitions are a separate approved phase.

## Frozen logical datasets

Both datasets use seed `20260828`, base instant `2026-08-28T00:00:00Z`, deterministic UUIDs with
RFC 4122 variant and explicit version-4 bits, and exclusively synthetic recipients under
`example.test`.

The generator is `benchmarks/dataset.py`. The demo matrix contains 25 Orders, 50 Shipments, all
eight Shipment states, 89 TrackingEvents, 93 inboxes, and 86 Notifications. The benchmark matrix
contains 1,000 Orders, 1,500 Shipments, 15,000 initial TrackingEvents, 15,000 `PROCESSED` inboxes,
and 2,998 Notifications. Alpha and Beta each own 750 benchmark Shipments. The mutable component
contains 188 `IN_TRANSIT` plus 188 `OUT_FOR_DELIVERY` Shipments.

The frozen artifact is `benchmarks/datasets/benchmark-v1.0.json`. It is the complete logical
document used by the seed: metadata, two disjoint balanced mutable cohorts, the timeline read
cohort, and canonical Orders, Shipments, inboxes, TrackingEvents, and Notifications. It is not a
second compact manifest.

Every table is sorted by canonical `id`; each cohort has an explicit canonical order. UUIDs use
lowercase canonical text; UTC datetimes use six fractional digits and `Z`; dates use ISO 8601;
bytes use base64; object keys are sorted. JSON is encoded as UTF-8 with compact separators,
`ensure_ascii=false`, no NaN values, and no trailing newline. The adjacent
`benchmark-v1.0.logical.sha256` is SHA-256 over exactly the persisted JSON bytes. The digest is not
inside the document, so the algorithm has no self-reference. Reordering, truncation, noncanonical
whitespace, a divergent sidecar, or a campaign digest mismatch is rejected before cohorts load.
After byte authentication, the effective campaign loader independently replays every event through
the frozen, dependency-free v1.0 transition contract. Recomputing the JSON, sidecar, and campaign
digest therefore cannot legitimize semantically altered result labels or timelines. Generator tests
also compare that frozen replay with the application's canonical state machine; Locust itself still
imports no application or database package.

Regenerate and verify the frozen artifacts:

```powershell
uv run python -m benchmarks.dataset
uv run python -c "from pathlib import Path; from benchmarks.dataset import verify_benchmark_artifacts; print(verify_benchmark_artifacts(Path('benchmarks/datasets')))"
```

## Seed safety contract

`benchmarks/seed_loader.py` reflects the migrated schema and uses SQLAlchemy Core. It does not
import ORM models or reproduce table definitions. Before writing, it requires:

- the `postgresql+psycopg` dialect and PostgreSQL major version 18;
- an explicit `APP_ENV` appropriate to the selected dataset;
- `--confirm-database-name` exactly equal to both the URL database and `current_database()`;
- the database Alembic head exactly equal to the repository head;
- exactly the unchanged official Alpha and Beta carrier rows;
- either an empty set of business tables or the complete exact logical dataset.

All validation and inserts run in one database transaction. Empty databases receive bulk inserts in
FK order. Repeating the exact dataset performs no writes. Partial, foreign, or divergent data raises
`SeedSafetyError` before commit. The loader contains no `DELETE`, `TRUNCATE`, drop, downgrade, or
reset operation. Errors never print a password, full DSN, carrier secret, signature, or raw body.

Example for an explicitly disposable local database:

```powershell
$env:APP_ENV = "local"
$env:DATABASE_URL = "postgresql+psycopg://<user>:<password>@127.0.0.1:5432/fulfillflow_demo"
uv run python scripts/seed_demo.py --confirm-database-name fulfillflow_demo
```

Benchmark data requires `APP_ENV=benchmark` outside tests:

```powershell
$env:APP_ENV = "benchmark"
$env:DATABASE_URL = "postgresql+psycopg://<user>:<password>@127.0.0.1:5432/fulfillflow_benchmark"
uv run python scripts/seed_benchmark.py --confirm-database-name fulfillflow_benchmark
```

## Campaign manifest

`benchmarks/campaign.py` requires a complete manifest with release/SHA, authenticated dataset
digest, one app worker, 60 seconds of warm-up, 300 seconds of measurement, resource limits for
app/PostgreSQL/loadgen, pool parameters, immutable image digests, logging/tracing, collection
interval, bounded process/request/drain timeouts, load levels, cohort references, Alembic heads, and
a release-specific PostgreSQL structural-schema digest. An official manifest must contain exactly
five repetitions. Every load is capped at 188 users and must use a multiple of four so its active
slots remain balanced across both carriers and both initial states.

The manifest also requires finite, nonnegative `stabilization_seconds` and a `host` contract.
Official stabilization must be positive; the synthetic fixture deliberately uses zero and empty
host expectations, not approved host parameters. After database preparation/verification and runtime
manifest installation, every repetition waits the declared interval before warm-up. Metadata records
`started_at`, `finished_at`, `expected_seconds` and monotonic `observed_seconds`. Dynamic host checks
then gate warm-up admission. A fixed wait is not evidence of thermal or memory quiescence.

Process timeouts are finite and must satisfy exactly these strict inequalities:

```text
warmup_process_seconds > 60 + drain_seconds
measurement_process_seconds > 300 + drain_seconds
```

These are minimum consistency rules, not an added numeric safety margin or a guarantee that process
startup/export overhead fits. The phase-start timeout and per-request timeout remain separate.

Host-dependent users, spawn rate, load levels, CPU, memory, and even warm-up quota `q` are not
selected in Phase A. There is intentionally no `benchmarks/campaigns/v1-baseline.json`. The file
`benchmarks/fixtures/smoke-campaign.json` is explicitly synthetic, non-official, and exists only for
contract tests and validation smoke.

Validate it without starting Locust:

```powershell
uv run python -m benchmarks.run_campaign `
  --manifest benchmarks/fixtures/smoke-campaign.json `
  --validate-only
```

Execution is deliberately fail-closed: `run_campaign.py --execute` additionally requires a literal
campaign-name confirmation, a nonexistent results destination, a base URL, and a JSON argv
preparation command that restores and seeds the database before every repetition. Secrets and DSNs
are forbidden in argv and must come from the environment. A zero exit status is insufficient. After
confirming PostgreSQL 18, the runner reads the installed Alembic heads and stable `pg_catalog`
structure, compares its structural digest with the release-specific manifest value, and only then
verifies the authenticated dataset, initial cardinalities, tracking results, and every mutable
cohort state through `psql` in the PostgreSQL container. It canonicalizes and compares every
relevant column of every Order, Shipment, inbox, TrackingEvent, and Notification against the
authenticated artifact, plus the complete official Alpha/Beta rows. UUID, UTC timestamp, date,
JSON, `bytea`, enum/string, null, and relationship values participate in per-table and global data
digests, so equal counts cannot conceal changed content. It never repairs database state and reports
only sanitized divergence classes and digests, never row contents.

Structural contract version 1 includes the exact public application tables; column ordinal, full
physical type, nullability, normalized default, identity and generated mode; primary, unique,
foreign-key and check constraints with columns, actions and deferrability; and complete index keys,
order, expressions, included columns, predicates, access method, uniqueness and validity. It omits
OIDs, relfilenodes, sizes, planner statistics, page counts, environment-dependent ownership,
filesystem timestamps, containers and row data. The expected digest in
`fixtures/smoke-campaign.json` was captured once from PostgreSQL 18 after applying the v1.0 migration
head to a clean database; the observed digest is independently recalculated from the prepared
database before every warm-up.

The structural digest authenticates one release, not the comparison dataset. v1.0 and v1.1 may
legitimately have different app image, Git SHA, Alembic head and schema digest, and those differences
are recorded as part of the architectural change. Every repetition of one release must match that
release's expected structure, while the logical dataset, public routes, workload, weights and
measurement methodology remain frozen across the comparative campaign.

Before either phase, Docker inspection compares expected and observed project/service labels,
container IDs, image digests, health, effective CPU/memory limits, one app worker, pool/statement
timeouts, PostgreSQL 18, and logging/tracing. Metadata stores expected and observed values plus the
comparison result; declarations are never presented as observations. Official runs refuse tracked
or staged changes. Synthetic smoke records `worktree_clean=false` when appropriate.

### Sanitized host contract

`host_probe.py` supports the official Windows/WSL2 host with bounded, read-only commands and no new
dependency. Import, construction and `--validate-only` do not run a probe. At execution, stable
identity is observed once (also saved in campaign `metadata.json`); each repetition records that
identity and a fresh dynamic observation in its own `metadata.json`, including a refused host gate.

All `host.identity` fields are essential in an official manifest: `os` (`Windows`), `os_version`,
`os_build` (build plus UBR), `cpu_model`, `physical_cores`, `logical_processors`,
`physical_memory_bytes`, `docker_engine`, `docker_compose`, `wsl_version`, `wsl_kernel`,
`docker_cpus` and `docker_memory_bytes`. Docker's reported kernel must match `docker-desktop`'s
observed WSL2 kernel. Effective Docker resources come from the daemon, not a personal configuration
file. A changed host/VM requires a new invocation and review of the declared contract.

Essential `host.conditions` fields are `ac_power`, `power_plan_guid` (lowercase GUID, never a custom
plan name) and `concurrent_containers` (running count excluding the campaign's verified app,
PostgreSQL and loadgen IDs). No names or IDs of foreign containers are persisted. A missing campaign
container makes that check unconfirmed, not zero. Essential declarations/observations missing in an
official campaign, or any declared mismatch even in a non-official run, fail closed.

Dynamic `available_memory_bytes`, `committed_bytes`, `commit_limit_bytes`,
`pagefile_allocated_bytes`, `pagefile_used_bytes`, `wsl_swap_total_bytes` and `wsl_swap_free_bytes`
are recorded without uncalibrated acceptance thresholds. Each field has `expected`, `observed`,
`matches` and `status`; unavailable optional observations use null plus `not_confirmed`. An
undeclared optional comparison has `matches=null`, never a fabricated success. Thresholds, stable
power conditions and the final stabilization interval still need an approved execution protocol.

Only allowlisted values are retained. No hostname, user, serial, IP/MAC, personal path, general
process inventory, environment dump, secret, proprietary sensor or raw error output is recorded.
The probe does not change power, Docker or WSL settings and does not read personal configuration
files. It is a point-in-time check, not continuous detection of host interference during measurement.

### Comparable resource budget

The primary comparison fixes aggregate CPU/memory separately for the application tier and the data
tier. A monolith and extracted services share the same total application budget; any additional
database shares the same total data budget. Distribution overhead is included, not granted free
resources. Loadgen remains separate and identical; connection pools must respect the same aggregate
connection budget instead of multiplying it by component. v1.0 retains one Uvicorn worker.
Per-component resource experiments may supplement, but never replace or mix with, this primary
comparison. This defines no future implementation or final resource values.

## HTTP workload contract

`benchmarks/locustfile.py` imports only the benchmark contract, Python/Locust libraries, and uses
public HTTP routes. It does not import `fulfillflow`, SQLAlchemy, psycopg, ORM models, repositories,
services, or database settings.

The mixed request plan has exactly 100 executable slots:

- 25 Shipment detail requests;
- 25 timeline requests;
- 30 signed webhook requests;
- 20 filtered Shipment listing requests.

Timeline and detail select only the frozen `timeline_read_cohort`, which is disjoint from both
mutable cohorts. Each user receives one exclusive warm-up Shipment and one exclusive measurement
Shipment. Both phases are balanced across Alpha/Beta and `IN_TRANSIT`/`OUT_FOR_DELIVERY`.

Webhook users alternate only:

```text
IN_TRANSIT <-> OUT_FOR_DELIVERY
```

The external event ID includes profile, load, phase, slot, and sequence, but never release or
repetition. Warm-up uses `warmup`; measurement uses `measure`. `occurred_at` advances by the frozen
positive step for each slot. HMAC uses the current Unix timestamp. The payload is serialized once;
the exact bytes are signed and sent. Only HTTP 200 with `result=APPLIED` succeeds. Timeout, HTTP
error, `DUPLICATE`, `NO_STATE_CHANGE`, or `IGNORED_*` records a Locust failure, invalidates the
repetition with a non-zero process exit code, halts that mutable sequence, and coordinates campaign
termination. It never repairs or queries the database.

Every confirmed cycle edge is expected to append one TrackingEvent and one Notification, update the
Shipment, and leave its Order uncompleted. Those persistence effects are application invariants and
are verified by PostgreSQL/API integration tests, not by adding database access to Locust.

Warm-up and measurement run in separate Locust processes and write to separate directories. Only
the measurement process can produce the canonical Locust CSV files at the repetition root. During
measurement, the runner also records sanitized HTTP/result tallies, periodic container CPU/memory
samples, PostgreSQL connection counts, and before/after database cardinalities. `metadata.json`
separates manifest expectations from values observed through Docker/Compose and PostgreSQL; any
required mismatch invalidates the repetition. `checksums.sha256` covers every final repetition
artifact except itself. Completed result directories are intentionally visible to Git for later
audit and preservation.

The same `ResourceSampler` also covers warm-up, writing `warmup/resources.csv`. Both phase resource
files are required and checksummed. After each process exits, validation rejects absent/empty files,
incomplete or duplicate service cycles, wrong container IDs, invalid timestamps, nonfinite/negative
CPU, invalid memory and missing PostgreSQL connection counts. This verifies data completeness, not
an uncalibrated cadence tolerance: actual sample timestamps must still be reviewed for collection
overhead and gaps. Measurement sampling and canonical measurement CSV locations remain unchanged.

For an approved official campaign, five valid repetitions per profile/load are retained and
`summary.csv` reports their medians for throughput, error rate, p50, and p95. Invalid or interrupted
repetitions remain marked incomplete and never enter the median. Phase A implements this capability
without generating official results or claiming a baseline.

## Warm-up and logical pre-measurement identity

All users reach a barrier before the 60-second clock starts. The manifest fixes an even quota `q`
per warm-up Shipment. A deterministic global index composed from sequence index and slot rank gives
every event a distinct offset distributed throughout the window; the final offset is strictly before
60 seconds. The remaining interval is admission headroom, not a final-response deadline (for
188 users and `q=2`, `60 / (188*2+1)` is approximately 159 ms). An admitted request may complete
during `drain_seconds`, still subject to its request timeout. No extra mutations are emitted after
quota completion. Quota is checked after drain: all users must have exactly `q` confirmed `APPLIED`
events. An incomplete quota or drain timeout invalidates the repetition without extending admission.
`q=2` is only a synthetic fixture/future probe, not an approved calibration or official quota.

At the boundary, the warm-up process stops admitting requests, drains all requests already in flight,
exits, and is checked against the exact quota, expected database deltas and final cohort states. The
runner checks each warm-up external event ID, carrier and Shipment, exact payload hash and
`occurred_at`, normalized status, `APPLIED` transition edge, inbox-to-event-to-Notification links,
and the Shipment ordering pointer. It also rechecks every original dataset row. The measurement
cohort must remain untouched, including all ordering fields and per-Shipment event counts. A new
Locust process then starts the independent
300-second measurement window; its statistics and CSV writers are new rather than reset. The
application/PostgreSQL caches are warmed, but HTTP connections from the warm-up process are not
reused. At the measurement deadline, all request types stop being admitted and bounded draining
finishes before CSVs, counters, and checksums are finalized.

Pre-measurement identity is logical and cardinal: the same external event IDs, raw payload hashes,
`occurred_at` values, results, cohort states, row counts, and preserved 15,000 initial events. UUIDs,
inbox/TrackingEvent/Notification physical IDs, `received_at`, `processed_at`, and equivalent physical
timestamps generated by public warm-up request processing may differ. External event IDs,
`occurred_at`, payload hashes, result, Carrier, Shipment, normalized status, transition edges, and
logical relationships may not differ. No
post-warm-up database rewrite is permitted to conceal that expected runtime variation.

The runner is fail-closed: it authenticates the integral dataset artifact and sidecar, verifies
preparation cardinalities and cohort states, compares expected and observed environment values,
uses bounded subprocess/drain timeouts, and leaves interrupted output marked incomplete. It never
records a DSN, password, HMAC secret, signature, or raw webhook body.

## CI boundary

CI may regenerate/verify datasets, exercise seed safety against PostgreSQL 18, validate manifests,
test the Locust contract, validate Compose, build the loadgen target, and run a minimal version or
import smoke. HostProbe tests use simulated executors only, never the real CI host; stabilization
tests use injected clocks/waits. CI must never run the 60-second warm-up, the 300-second measurement,
or a complete campaign. Official outputs belong only to a separately approved execution phase.
