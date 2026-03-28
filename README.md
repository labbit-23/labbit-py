# labbit-py

Python API/service for report fetch and delivery flows.

## Run locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## Secrets

- Copy `.env.example` to `.env` and export vars before running services.
- Keep `config.ini` and `services.ini` local/private (they are ignored by git).
- `config.ini.example` and `services.ini.example` are safe templates.

Important env overrides:
- `NEOSOFT_LOGIN_USERNAME`
- `NEOSOFT_LOGIN_PASSWORD`
- `WHATSAPP_API_KEY`
- `WHATSAPP_ENDPOINT`
- `CTO_INGEST_TOKEN` (if using `ENV:CTO_INGEST_TOKEN` in services config)

## PM2 on VPS

API service:
```bash
pm2 start /opt/labbit-py/start.sh --name labbit-api --interpreter /usr/bin/bash
```

Monitoring agent:
```bash
pm2 start /opt/labbit-py/scripts/start-monitoring.sh --name labbit-monitoring --interpreter /usr/bin/bash
```

Deploy both:
```bash
bash /opt/labbit-py/scripts/deploy-vps-api.sh
```

Or use ecosystem file for both services + env in one place:
```bash
cd /opt/labbit-py
pm2 start ecosystem.config.cjs
pm2 save
```

After editing env values in `ecosystem.config.cjs`:
```bash
pm2 restart ecosystem.config.cjs --update-env
pm2 save
```

## Structure

- `app/` core application modules
- `scripts/` utility/CLI scripts
- `tests/` ad-hoc tests
- `assets/` static files used by PDF tools
- `config.ini` runtime config
- `services.ini` monitoring service config

## Monitoring Profiles (Local vs VPS)

Use one of these templates as `services.ini`:

- Local machine: `services.local.ini.example`
- VPS machine: `services.vps.ini.example`

Only required edit before start:
- Set `ingest_token` in `[monitoring]`.

`lab_id` is prefilled and does not need changes.
