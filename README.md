# DeptHub — Association Dues Collection

A dues and membership platform for university departments: member rosters, academic sessions, dues assessments, online and offline payments, receipts, and finance reporting, plus department operations such as events and attendance.

Built as a Django modular monolith with server-rendered pages (HTMX, Alpine.js, Tailwind CSS), PostgreSQL, and Celery/Redis for background jobs.

## Requirements

- Python 3.12
- Node.js 20+ (builds the CSS and copies the browser libraries)
- Docker Desktop (runs PostgreSQL and Redis)

## First-time setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows; use `source .venv/bin/activate` elsewhere
pip install -r requirements-dev.txt
npm install
npm run build                     # writes static/css/app.css and static/vendor/
cp .env.example .env              # then edit values as needed
docker compose up -d              # PostgreSQL on localhost:5433, Redis on localhost:6379
python manage.py migrate
python manage.py runserver
```

Open http://localhost:8000. The health check is at `/healthz/`.

PostgreSQL is exposed on port **5433** so it doesn't clash with a locally installed PostgreSQL on 5432.

## Demo data

The `seed/` folder generates an anonymised dataset for UMaT's Computer Science & Engineering department: two sessions, about 525 students, elected executives, staff, dues, payments and receipts.

```bash
python seed/generate_umat.py      # writes seed/output/
python manage.py load_umat_seed   # add --flush to replace existing data
```

Every seeded account uses the password `depthub-demo-2026` (change it with `--password`). Some accounts to try:

| Role | Email |
|---|---|
| Student | `eunice.ankrah75@st.umat.test` |
| Financial Secretary | `issah.ziem62@st.umat.test` |
| Treasurer | `abdulrashid.bawah77@st.umat.test` |
| Department President | `kwaku.duah97@st.umat.test` |
| HoD / Faculty Adviser | `equansah@umat.test` |
| Auditor | `bkoomson@umat.test` |
| Platform Administrator | `ioforiatta@umat.test` |

These emails come from the default generator seed (`--seed 2026`). The sample phone numbers use real Ghanaian network prefixes, so keep `SMS_BACKEND=disabled` whenever this data is loaded. The loader refuses to run when `DEBUG` is off.

## Background jobs

In development, Celery tasks run inline (`CELERY_TASK_ALWAYS_EAGER=true`), so no worker is needed. To run the real worker and scheduler against Redis, set `CELERY_TASK_ALWAYS_EAGER=false` and start:

```bash
celery -A config worker -l info --pool=solo   # --pool=solo is needed on Windows
celery -A config beat -l info                 # daily dues reminders (09:00), hourly checkout clean-up
```

Reminders can also be run by hand: `python manage.py send_dues_reminders --dry-run` shows who would be reminded without sending anything.

## Everyday commands

| Task | Command |
|---|---|
| Run tests | `pytest` |
| Lint and format | `ruff check . && ruff format .` |
| Rebuild CSS while editing templates | `npm run dev` |
| Stop the database | `docker compose down` (data is kept in the `pgdata` volume) |

## Project layout

```
config/       settings (base, dev, test, prod), URLs, Celery
apps/         one Django app per module (common, accounts, finance, ...)
templates/    shared templates and partials
assets/src/   Tailwind source (built into static/css/)
seed/         anonymised UMaT sample-data generator (see seed/README.md)
```

## Documentation

- [Architecture](docs/architecture.md): modules, money rules, background jobs
- [Roles and permissions](docs/permissions.md): who can do what, and the separation of duties
- [Operations runbook](docs/runbook.md): environment, Docker deploy, Paystack set-up, backups, rollback
- [Troubleshooting](docs/troubleshooting.md)
- API: interactive docs at `/api/docs/`, OpenAPI schema at `/api/schema/`

## Branches

- `main`: released code.
- `develop`: integration branch. Feature branches (`feature/*`, `chore/*`) merge here through pull requests once CI passes.
