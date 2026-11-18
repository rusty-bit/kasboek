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

`docker-compose.yml` mounts `/volume2/Docker/kasboek/data`. That path is absolute on purpose, because a relative path would land on the small rootfs.

1. `mkdir -p /volume2/Docker/kasboek/data && chown 1000:1000 /volume2/Docker/kasboek/data`
2. Provide `stack.env`, using `stack.env.example` as the template. In Portainer, the stack's environment variables become `stack.env`.
3. The compose file builds the image (`build: .`). Portainer's web editor can't build, so deploy it as a **Repository** stack, or build it once over SSH with `docker build -t kasboek:0.1.0 .` and then remove the `build:` line.

The app listens on port 8420 and the container is limited to 256 MB.
