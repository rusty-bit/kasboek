# Kasboek

Local-first family finance app: bank sync (Rabobank, Knab, comdirect), auto-categories, recurring bills and contracts, free-to-spend, forecasts, savings pots and habit goals. Runs in Docker on the NAS.

- `CLAUDE.md`: rules for Claude Code
- `docs/SPEC.md`: features, bank connectivity, architecture, algorithms, milestones
- `app/db.py`: schema + default NL/DE categories
- `app/importers.py`: CSV (Rabobank, Knab, ING, comdirect…) and CAMT.053 import, tested
- `samples/`: synthetic bank files (no real data)

## Get started (Docker)

Download [`docker-compose.yml`](docker-compose.yml) into an empty folder and run:

```
docker compose up -d
```

This pulls the ready-made image `ghcr.io/rusty-bit/kasboek` (amd64 and arm64). With Portainer, paste the same
file as a stack.

1. Open `http://<this machine>:8420` and sign in with username `kasboek` and password `kasboek`.
2. Kasboek asks you to choose your own username and password right away. You can change them later through the
   person icon at the top right.
3. **Banks**: create a free application in the [Enable Banking](https://enablebanking.com) control panel and
   upload its `.pem` key on the Banks page. Or skip this and import CSV / CAMT.053 files on the Import page.
4. **Accounts**: set each account's kind (checking, savings…) and owner.

Your data (database, bank key, daily backups) lives in the Docker volume `kasboek-data`. To use a folder instead,
put `KASBOEK_DATA=/absolute/path` in a `.env` file next to `docker-compose.yml`. Other optional settings are in
`stack.env.example`. Updating: change the version in `docker-compose.yml`, then
`docker compose pull && docker compose up -d`.

Forgot the login? `docker exec kasboek python -m app.auth reset` sets it back to `kasboek` / `kasboek`.
For use outside your home network, put Kasboek behind a reverse proxy with HTTPS.

## Run locally (Windows, PowerShell)

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pytest
$env:DATA_DIR = "./data"; uvicorn app.main:app --reload
```

Sign in with `kasboek` / `kasboek` on a fresh data folder, or set `$env:KASBOEK_AUTH = "off"` to work without a login.

Open http://127.0.0.1:8000.

## Run in Docker (local dev)

```
docker compose -f docker-compose.dev.yml up --build
```

Uses `./data` next to the repo. **Local only. Never use this file on the NAS.**

## Deploy on the NAS

Paths are absolute on purpose: a relative bind mount would land on the 1.8 GB rootfs.

1. The data (`kasboek.db`) lives in `/volume1/Docker/kasboek/data` on the `Docker` share (the same volume as
   Docker itself), owned by uid 1000: `chown -R 1000:1000 /volume1/Docker/kasboek/data`. Only one Kasboek may sync
   with the banks, so keep a local copy stopped.
2. Portainer: Stacks → Add stack → Web editor → paste `portainer-stack.yml`; deploy. Portainer pulls the image.
3. Open `http://<NAS address>:8420`, sign in with `kasboek` / `kasboek` and choose your own login.
4. For HTTPS, add a proxy host in Nginx Proxy Manager to `<NAS address>:8420`. Then register
   `https://<that host>/banks/callback` as redirect URL in the Enable Banking control panel and on the Banks page,
   so bank logins come straight back.

Updating: change the version in the stack (Portainer → the stack → Editor) and click Update the stack.
Backups: one copy of the database per day in `/volume1/Docker/kasboek/data/backups`, the newest 14 are kept.

The app listens on port 8420 and the container is limited to 256 MB.

## Releasing

Bump the version in `docker-compose.yml` and `portainer-stack.yml`, commit, then tag:
`git tag v0.4.1 && git push origin v0.4.1`. GitHub Actions runs the tests and publishes
`ghcr.io/rusty-bit/kasboek:0.4.1` for amd64 and arm64.
