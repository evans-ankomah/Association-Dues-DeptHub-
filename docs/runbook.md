# Operations runbook

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | yes | Long random string. Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`. |
| `ALLOWED_HOSTS` | yes (prod) | Comma-separated, e.g. `dues.example.edu` |
| `CSRF_TRUSTED_ORIGINS` | yes (prod) | e.g. `https://dues.example.edu` |
| `DATABASE_URL` | yes | `postgres://user:pass@host:5432/db` |
| `REDIS_URL` | yes (prod) | Celery broker, results and cache |
| `SITE_URL` | yes (prod) | Absolute base URL used in emails |
| `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` | for email | SMTP settings |
| `PAYSTACK_SECRET_KEY`, `PAYSTACK_PUBLIC_KEY` | for online payment | Use the `sk_test_` key until you go live |
| `SMS_BACKEND` | no | `disabled` (default) or `console`. No real SMS provider is wired in yet. |
| `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS` | no | Defaults are strict. Lower HSTS while first testing HTTPS. |
| `PRIVATE_MEDIA_ROOT` | yes (prod) | Folder for uploaded documents. Must be a persistent volume, **never** served by the web server or proxy. Back it up with the database. |
| `DOCUMENT_MAX_UPLOAD_BYTES` | no | Upload limit, default 10 MB. Keep the proxy's request-body limit a little above this. |

Never commit `.env` files. Use `.env.example` as the template.

## Deploying with Docker

```bash
cp .env.example .env.prod            # fill in production values, plus POSTGRES_DB/USER/PASSWORD for the db container
docker compose -f docker-compose.prod.yml --env-file .env.prod build
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser   # first platform admin
```

What runs where:
- `migrate` runs once before `web` starts.
- `web` runs gunicorn on port 8000. Put a TLS proxy (Caddy, nginx or the host's load balancer) in front of it and forward `X-Forwarded-Proto`.
- `worker` and `beat` run the Celery jobs.

Before every release:
1. `bash scripts/check.sh` passes (lint, migrations, schema, deploy check, tests).
2. Take a **database backup** (below).
3. Deploy, then check `https://<host>/healthz/` returns `{"status": "ok", "database": true}`.

## Paystack set-up

1. In the Paystack dashboard → Settings → API Keys & Webhooks, copy the secret and public keys into the environment.
2. Set the webhook URL to `https://<host>/finance/webhooks/paystack/`.
3. Make a test payment from **My dues → Pay online** with a test card or test MoMo number. The receipt should appear within seconds, and the event should show as *processed* under **Reconciliation → Webhook problems** (none expected).
4. Refunds for online payments are made in the Paystack dashboard. Record the refund reference in DeptHub when processing the refund.

## Backups

Back up the database at least daily, and keep copies off the server.

```bash
# backup (custom format, compressed)
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > depthub-$(date +%F).dump

# restore into an empty database
docker compose -f docker-compose.prod.yml exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists < depthub-YYYY-MM-DD.dump
```

Uploaded documents live in the `private_media` volume, not the database. Back it up at the same time:

```bash
docker run --rm -v depthub_private_media:/data -v "$PWD":/backup alpine tar czf /backup/documents-$(date +%F).tgz -C /data .
```

**Test a restore every term** into a separate database, and check that the dashboard totals match production. A backup that has never been restored isn't a backup.

## Rolling back

1. Stop `web`, `worker` and `beat`.
2. Redeploy the previous image tag.
3. If the release ran migrations that the old code can't use, restore the pre-release backup. Never run migrations backwards against live financial data without a backup.

## Routine tasks

| When | Task |
|---|---|
| Start of session | Create the new academic session and mark it current (Django admin). Import or roll over memberships. Create dues types and assess members. |
| Executive handover | End outgoing role assignments; create new ones. Check the new Treasurer and Financial Secretary can sign in. |
| Weekly (Treasurer) | Reconciliation page: clear pending verifications, check duplicates and mismatches, match the MoMo/bank statement. |
| End of session | Close dues types and budgets. Export balances and payments to Excel for the archive. |

## Sample data

`python manage.py load_umat_seed` is for development and demos only. It refuses to run with `DEBUG` off. Its phone numbers use real Ghanaian prefixes, so keep `SMS_BACKEND=disabled` on any database that has ever held seed data.
