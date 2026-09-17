# AGENTS.md

## Authority and scope

- Work only on the user-authorized v1.3 scope or increment. DESIGN defines asynchronous Notifications while preserving asynchronous Tracking; RELEASE_PLAN records implementation status. Planning does not authorize implementation. Preserve v1.0.0, v1.1.0-rc.1, v1.2.0-rc.1 and all frozen evidence.
- Read the relevant sections of `DESIGN.md` before changing architecture, domain behavior, persistence, HTTP contracts, security, observability, seeds, or benchmarks.
- For a targeted task, locate the applicable headings and avoid loading unrelated DESIGN sections into context.
- Read inherited contracts from the frozen commit linked in DESIGN, using `git show <commit>:DESIGN.md` when needed. Do not load or copy the entire historical document for each task.
- `DESIGN.md` is authoritative for product and architecture. This file defines how to work in the repository.
- Do not infer requirements from hypothetical later architectures. Build the simplest implementation that fully satisfies the current contract.
- If a request conflicts with `DESIGN.md`, surface the conflict before implementing it. Do not silently diverge.

## Working method

1. Inspect the affected code, tests, migrations, and relevant DESIGN sections before editing.
2. State a short implementation plan for work spanning multiple files or concerns.
3. Make the smallest coherent change. Preserve unrelated user changes.
4. Add or update tests with the behavior, not afterward as an optional pass.
5. Run focused checks first, then the broadest relevant validation available.
6. Review the final diff for scope, secrets, accidental generated files, and DESIGN drift.

For v1.3 implementation requests, follow the executable increments in `RELEASE_PLAN.md`, with tests from the first increment. The plan does not replace DESIGN or authorize execution beyond the user's current request. Resolve routine implementation choices autonomously; ask only for concrete contract or scope decisions.

Do not attempt the entire release in one undifferentiated change. Do not create empty placeholder files merely to reproduce the planned tree.

## Approved stack

- Python 3.13, FastAPI, Uvicorn, Pydantic v2 and pydantic-settings.
- SQLAlchemy 2.x async with psycopg 3, PostgreSQL 18 and Alembic.
- Jinja2, HTMX and locally stored Bootstrap assets; no Node.js toolchain or SPA framework.
- pytest, pytest-asyncio, httpx and pytest-cov.
- Ruff, Mypy and Import Linter.
- Prometheus client and configurable OpenTelemetry/OTLP; Jaeger is optional local infrastructure.
- uv with `pyproject.toml` and committed `uv.lock`.
- Multi-stage Dockerfile, Docker Compose and GitHub Actions.
- RabbitMQ 4.x and aio-pika for the existing Tracking command/result flows and the Notifications event in DESIGN. Preserve the pinned versions, image digests and locked dependencies.

Do not replace an approved component or add a production dependency without a concrete requirement. Update `uv.lock` whenever dependencies change; never edit it manually.

If the repository has not been bootstrapped and `uv.lock` does not exist, creating the initial lock is permitted. After that, use frozen installs for normal validation.

## Architecture boundaries

- Core, Tracking and Notifications have separate databases and roles in one PostgreSQL instance in the v1.3 target. Each API and worker accesses only its owning service's database; Notifications remains local until the coordinated extraction increment.
- Business modules are `orders`, `shipments`, `carriers`, `tracking` and `notifications`.
- `shared` contains only stable technical primitives such as clock, IDs and pagination. It contains no business rules and depends on no business module or infrastructure framework.
- Modules within a service communicate through Python public interfaces. Cross-service commands/results/events use the AMQP contracts in DESIGN; forwarding and queries use the documented authenticated HTTP contracts. Do not import another service's business implementation or persistence.
- A module may access only its own ORM models and repositories.
- Within a service, cross-module access goes through `public.py` or an explicitly public schema.
- Routers and templates contain no business rules. ORM models are never API schemas.
- `domain.py` must not depend on FastAPI, SQLAlchemy, AMQP clients, web, database or observability infrastructure.
- Respect this dependency direction:
  - Tracking uses explicit Core contracts, never in-process access to Core business modules. Preserve its asynchronous command/result flow. Notifications consumes its own Core event, never the result destined for Tracking.
  - Shipments may use public Orders and Carriers contracts.
  - Orders, Carriers and Notifications do not depend on another business module unless DESIGN is revised first.
- Keep Import Linter contracts executable. Do not hide forbidden imports behind local imports, `TYPE_CHECKING`, dynamic imports or re-exports.
- `adapter_key` resolves through a static allowlist. Never import code using a database value.
- Do not add abstractions, ports, event buses or deployment seams solely for speculative reuse.

## Persistence and units of work

- PostgreSQL 18 is the only supported database. Never introduce SQLite or an in-memory persistence substitute.
- Use native UUID, `timestamptz`, named constraints and varchar-backed domain enums with named checks as defined in DESIGN.
- Alembic is the only schema creation and evolution mechanism. Do not call `Base.metadata.create_all()` in runtime or integration tests.
- Use independently scoped sessions per request or worker operation within the owning service. Never share an AsyncSession between concurrent tasks. Hold no SQL connection, transaction or lock during HTTP or AMQP I/O; close/release the SQL scope before publishing or acknowledging. Scripts and workers establish explicit transaction boundaries.
- Repositories may query, add and `flush`; they never `commit` or `rollback` a coordinated transaction.
- The coordinating service owns local transaction boundaries. Public module services within that service participate in its current transaction; no session or transaction spans services.
- Do not add a generic Unit of Work abstraction unless current code demonstrates a concrete need; `AsyncSession` may serve as the transaction context.
- Avoid implicit async ORM I/O and lazy-loading surprises. Load required relationships explicitly.
- Foreign keys for business records use restrictive deletion. Do not add cascading deletion of business history.
- Obtain domain time through the injected `Clock`; do not call the real clock directly from domain logic.
- Keep application data synthetic and deterministic in seeds and tests.

## Tracking transaction invariants

- Authenticate webhooks before parsing or persisting them.
- Compute HMAC-SHA256 over the exact raw bytes and compare with `hmac.compare_digest`.
- Preserve authenticated `raw_body` byte-for-byte. `parsed_payload` is only a query projection.
- Keep one secret per carrier, loaded from settings. Never log secrets, signatures or complete webhook bodies.
- Treat the database unique constraint on `(carrier_id, external_event_id)` as the final idempotency authority.
- Same event ID and same payload hash may resume or return the original result. Same event ID with a different hash is a conflict and never overwrites the original.
- Commit admission, normalized command and command outbox together before returning 202. Persist permanent admission rejection without creating application work.
- Commit the Core receipt/effects and result outbox together. Finalize Tracking with the received result in a separate local transaction, preserving identities, timestamps and outcomes.
- At v1.3 activation, replace local Notification creation with the independent event outbox in that same Core transaction, only for APPLIED transitions. Never dual-write or couple Notifications to Tracking results; isolate the two Core publishers.
- Notifications commits its simulation record and technical-inbox completion together, with one record per tracking_event_id. Infrastructure failures remain retryable or BLOCKED, not FAILED simulations. Core queries Notifications through authenticated HTTP, never its database or a local fallback.
- AMQP ACK follows durable technical-inbox persistence, not business completion. Durable local processing, bounded retries and explicit blocked states then own recovery. Never claim global atomicity or exactly-once transport.
- Repositories inside a coordinated local transaction must not commit independently.
- Core locks Shipment before Order; inbox locks stay in their owning database and never span network I/O. Preserve lock ordering in all worker paths.
- Use `READ COMMITTED`, `SELECT ... FOR UPDATE` and database constraints; do not rely on check-then-insert for concurrency safety.
- Permanent validation or domain failures mark the inbox `REJECTED`. Operational failures keep business state pending and technical work retryable or explicitly blocked; do not convert infrastructure failure into business rejection.
- TrackingEvent is append-only. Stale or invalid transitions are recorded without regressing Shipment state.
- Event ordering is the lexicographic tuple `(occurred_at, received_at, external_event_id)`.

## API, UI and security

- Keep the public API under `/api/v1`. Preserve v1.2 durable webhook acceptance with 202 and result polling. The v1.3 Notifications queries/progress and eventual simulation follow DESIGN and must ship with clients/tests; preserve other frozen routes and semantics.
- Use dedicated Pydantic schemas for create, read and list operations.
- Return documented `application/problem+json` errors through global handlers, including framework validation, 404 and 405 responses.
- Echo a valid `X-Request-ID` or generate a UUID; preserve the original correlation in durable messages and controlled logs. Do not claim tracing is implemented when it remains pending.
- Render HTML server-side. HTMX handlers call the same application services as the API; forwarding and pending/result views follow DESIGN, without business rules in templates or routers.
- Keep Bootstrap and HTMX assets local and compatible with the Content Security Policy.
- Mutating HTML forms require CSRF protection. JSON APIs and carrier webhooks remain stateless and cookie-free.
- Keep CORS disabled by default. Do not add application rate limiting to the controlled comparison environment.
- Never expose secrets, full signatures, raw exception traces or unsanitized external content.

## Code quality

- Use explicit type hints for public functions, service contracts and non-obvious values.
- Keep async code async end-to-end; do not execute synchronous database I/O in async request paths.
- Prefer small cohesive services and repositories over generic frameworks or deep inheritance.
- Raise or translate domain-specific errors; do not catch broad exceptions unless re-raising after required cleanup or telemetry.
- Comments explain invariants and non-obvious decisions, not line-by-line mechanics.
- Keep code, identifiers and API fields in English. Preserve external carrier field names only inside adapters and fixtures.
- Keep user-visible strings deterministic where tests or notifications depend on them.
- Do not weaken typing, lint rules, coverage gates, architectural contracts or existing tests to make a change pass.

## Tests and verification

Every behavior change requires the narrowest meaningful test. Select additional suites by impact:

- pure domain rule or adapter: unit tests;
- repository, constraint, transaction or lock: PostgreSQL integration tests;
- route, schema or error contract: API tests;
- module boundary: Import Linter and architecture tests;
- complete business journey: E2E test;
- template, HTMX or CSRF behavior: UI test.

Rules:

- Integration and API persistence tests run against real PostgreSQL, never SQLite.
- Transport, confirms, acknowledgements and restart tests use real RabbitMQ. Mocks complement failure injection; they do not replace broker integration or justify silent CI skips.
- Control time through `Clock`; tests must not depend on wall-clock time or execution order.
- Concurrency tests must use independent sessions/connections and verify database outcomes, not only mocked calls.
- Cover state transitions, both carrier adapters, raw-byte HMAC, idempotency, retry, lock ordering and Order completion as specified in DESIGN.
- Maintain at least 80% global coverage and complete coverage of transition rules, adapters, HMAC and idempotency.
- Do not call real carrier, email, cloud or other external services from tests.

## Commands

Use repository commands directly; the normal workflow must remain usable from PowerShell without Make, Bash or WSL.

```text
uv sync --frozen
uv run pytest tests/unit -q
uv run pytest
uv run pytest --cov=fulfillflow --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run lint-imports
uv run alembic upgrade head
uv run alembic current --check-heads
uv run alembic check
uv run python scripts/seed_demo.py
uv run fastapi dev src/fulfillflow/main.py
docker compose config
docker compose up --build
```

Run only commands that exist at the current implementation stage. When a check cannot run, report the exact missing prerequisite; do not claim success or substitute a weaker backend.

For a completed cross-cutting change, run Ruff, formatting, Mypy, Import Linter, the full relevant pytest suite, Alembic checks when models changed, and Docker build/smoke checks when runtime files changed.

## Seeds and benchmarks

- Seeds are repeatable and use the configured fixed seed. Re-running against a clean database must reproduce the same logical dataset.
- Do not change benchmark routes, payload semantics, scenario weights, dataset, warm-up, load shape or resource configuration casually.
- After the first valid benchmark, material changes require a documented new campaign and rerun of affected baselines.
- Warm-up uses its own deterministic event-ID namespace, is repeated after every database restore and reaches the same pre-measurement state.
- Extensive campaigns remain paused. Do not port the synchronous loadgen during functional increments or interpret 202 as completed work.
- Record new worker, broker, resource and observability identities. A future protocol must explicitly address offered/accepted/completed work and total resources, and declare instrumentation differences; never silently change frozen images for parity.
- Benchmark results must identify commit, environment, dependency lock, hardware and protocol.

## Scope guard

Do not add any of the following:

- user authentication, SSO, OAuth, RBAC or multitenancy;
- catalog, inventory, warehouse, payment, checkout, billing or ERP integration;
- real carrier, email, SMS, WhatsApp or push integrations;
- Kafka, Redis, Celery, Taskiq, additional brokers or workers outside the service-owned flows authorized in DESIGN;
- API Gateway, Kubernetes, AKS, cloud infrastructure, GitOps or autoscaling;
- React, Vue, Angular, Node.js build tooling or runtime CDN dependencies;
- MinIO, Blob Storage or file storage;
- AI/LLM features;
- background reprocessing outside the durable Tracking/Notifications contracts, other service extractions or distributed rate limiting.

If a task appears to require an excluded component, stop and explain the conflict instead of adding it.

## Documentation and change control

- Keep README commands and observable behavior synchronized with the implementation.
- Keep this file limited to recurring agent guidance; detailed product behavior belongs in DESIGN and one-off execution detail belongs in the active task.
- Update DESIGN in the same change only when an authorized decision alters architecture, module ownership, state machines, transaction boundaries, idempotency, public API, persistent infrastructure, security invariants or benchmark protocol.
- Do not rewrite DESIGN for private names, internal file splitting, fixture layout, visual styling or another implementation freedom listed there.
- Never edit DESIGN after the fact merely to legitimize an accidental divergence.
- When two conforming alternatives exist, choose lower operational complexity and lower coupling.

## Code review rules

- Prioritize concrete correctness, security, transaction, idempotency, concurrency, module-boundary and benchmark-comparability defects.
- Verify claims against the diff, relevant tests and DESIGN; include a reproducible failure path when possible.
- Treat missing tests for changed critical behavior as a correctness finding.
- Do not request speculative infrastructure, future-proof abstractions or out-of-scope features.
- Do not elevate formatting or naming preferences already enforced by tooling unless they obscure a defect.

## Completion report

At the end of a task, report concisely:

- behavior implemented or changed;
- files or areas materially affected;
- validation commands run and their outcomes;
- checks not run and the exact reason;
- any DESIGN change, remaining risk or intentional limitation.

Do not say a task is complete when required behavior is stubbed, tests are failing, migrations are missing or documentation knowingly disagrees with the code.
