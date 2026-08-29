# FulfillFlow

Bootstrap executável do FulfillFlow v1.0.0: FastAPI, configuração validada,
PostgreSQL 18 assíncrono, Alembic e infraestrutura local em Docker Compose.
Este incremento não contém módulos de negócio, UI, seeds, benchmark ou a
implementação completa de observabilidade.

## Subida local com Docker Compose

O fluxo padrão funciona a partir do checkout sem publicar o PostgreSQL no host:

```powershell
docker compose up --build --wait
```

O Compose aguarda o banco ficar saudável, executa `alembic upgrade head` no
serviço one-shot `migrate` e só então inicia `app`. A API é publicada apenas em
`127.0.0.1:8000` por padrão.

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health/live
Invoke-WebRequest http://127.0.0.1:8000/health/ready
```

Os contratos atuais são:

- `GET /health/live` retorna `200 {"status":"ok"}` sem acessar o banco;
- `GET /health/ready` retorna `200 {"status":"ok"}` quando o schema está no
  head do Alembic e `SELECT 1` responde;
- readiness retorna `503 {"status":"unavailable"}` se o banco ficar
  indisponível depois do startup;
- startup falha se o banco estiver inacessível ou o schema não estiver no head.

Os defaults de secrets no `compose.yaml` são exclusivos do ambiente local
isolado. Para sobrescrevê-los, copie `.env.example` para `.env` e substitua todos
os placeholders. Mantenha `POSTGRES_PASSWORD` e a senha codificada em
`DATABASE_URL` coerentes.

Para encerrar e remover o volume local deste projeto:

```powershell
docker compose down --volumes
```

## Execução sem container da aplicação

É necessário Python 3.13 gerenciado pelo `uv`, um PostgreSQL 18 acessível e um
`.env` preenchido. Para um banco no host, altere o hostname de `DATABASE_URL` de
`db` para `127.0.0.1`.

```powershell
uv sync --frozen
uv run alembic upgrade head
uv run alembic current --check-heads
uv run alembic check
uv run fastapi dev src/fulfillflow/main.py
```

## Testes e qualidade

Os testes unitários e de API não dependem de PostgreSQL. O teste de integração
usa somente um banco PostgreSQL 18 dedicado informado por `TEST_DATABASE_URL`;
sem essa variável, ele é explicitamente ignorado.

```powershell
uv run pytest tests/unit tests/api -q
uv run pytest
uv run pytest --cov=fulfillflow --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run lint-imports
docker compose config --quiet
```

A revisão `0001_bootstrap` não cria tabelas de domínio. Ela estabelece o head
inicial e a tabela técnica `alembic_version`, usados pelo gate de compatibilidade
do startup. Tabelas de Orders, Shipments, Carriers, Tracking e Notifications
serão introduzidas somente nos incrementos correspondentes.
