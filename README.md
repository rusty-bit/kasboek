# Kasboek

Local-first family finance app: bank sync (Rabobank, Knab, comdirect), auto-categories, recurring bills and contracts, free-to-spend, forecasts, savings pots and habit goals. Runs in Docker on the NAS.

- `CLAUDE.md`: rules for Claude Code
- `docs/SPEC.md`: features, bank connectivity, architecture, algorithms, milestones
- `app/db.py`: schema + default NL/DE categories
- `app/importers.py`: CSV (Rabobank, Knab, ING, comdirect…) and CAMT.053 import, tested
- `samples/`: synthetic bank files (no real data)

## Run locally (Windows, PowerShell)

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pytest
$env:DATA_DIR = "./data"; uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## Run in Docker (local dev)

```
docker compose -f docker-compose.dev.yml up --build
```

Uses `./data` next to the repo. **Local only. Never use this file on the NAS.**

## Deploy on the NAS

Paths are absolute on purpose: a relative bind mount would land on the 1.8 GB rootfs.

1. Copy the code to `/volume2/Docker/kasboek/src` (via the `Docker` share) and the data (`kasboek.db` + the
   Enable Banking `.pem`) to `/volume2/Docker/kasboek/data`, with the local app **stopped** (only one Kasboek may
   sync with the banks).
2. Over SSH on the NAS, once:
   `chown -R 1000:1000 /volume2/Docker/kasboek/data && cd /volume2/Docker/kasboek/src && docker build -t kasboek:0.2.0 .`
3. Portainer: Stacks → Add stack → Web editor → paste `portainer-stack.yml`; add the environment variables
   `KASBOEK_USER` and `KASBOEK_PASSWORD` (the household login); deploy.
4. Open `http://<NAS address>:8420`. For HTTPS, add a proxy host in Nginx Proxy Manager to `<NAS address>:8420`.
   Then register `https://<that host>/banks/callback` as redirect URL in the Enable Banking control panel and on
   the Banks page, so bank logins come straight back.

Updating: copy the new code to `src`, `docker build -t kasboek:<new tag> .`, change the tag in the stack, redeploy.
Backups: one copy of the database per day in `/volume2/Docker/kasboek/data/backups`, the newest 14 are kept.

The app listens on port 8420 and the container is limited to 256 MB.
