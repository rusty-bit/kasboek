# CLAUDE.md — Kasboek

Self-hosted family finance app. Full spec: `docs/SPEC.md` (read it first). Plan: milestones M0–M7 in SPEC §8, built in order.

## Stack
- Python 3.12, FastAPI, Jinja2 + HTMX (no JS build step), SQLite (plain `sqlite3`, no ORM), `httpx`, `PyJWT[crypto]`.
- One container. Data in `/data` (`DATA_DIR` env). Run locally with `DATA_DIR=./data uvicorn app.main:app --reload`.
- Tests: `pytest -q`. Every algorithm module (`importers`, `categorize`, `recurring`, `forecast`, `goals`) is pure: data in, result out, no DB or network. Each has a test file.

## Hard rules
- **Bank data never leaves the machine.** No cloud APIs other than Enable Banking (and only for fetching). No telemetry, no CDN assets: vendor HTMX into `app/static/`.
- **Never commit real bank data, `.env`, `*.pem`, `*.db`.** Real exports live outside the repo (on Windows: `a folder outside the repo`). Tests use only the synthetic files in `samples/`.
- Read-only banking. Never add payment initiation.
- Sign convention: `amount < 0` money out, `> 0` money in. EUR only.
- A category set by hand (`cat_source='manual'`) is never overwritten by any automatic step.
- Code ported from `nextwork-projects/broke` (MIT) keeps a header comment crediting it.
- Never silently guess missing numbers. If a forecast lacks data (less than one full month), say so in the UI.

## Deployment target
- Asustor NAS, ~3.8 GB RAM, Docker via Portainer stacks. Keep the container under ~200 MB RAM.
- **Bind mounts must be absolute paths under `/volume1/Docker/kasboek/...` (the NAS `Docker` share is `/volume1/Docker`; checked 2026-11-19).** A relative path in a Portainer stack lands on the 1.8 GB rootfs and has taken the NAS down before. `env_file: stack.env` may stay relative.
- Pin image tags; no `:latest`. Add healthcheck + log rotation.
- Optional: local LLM on the home AI box via an OpenAI-compatible URL (`LLM_BASE_URL`). The app must work fully without it.

## Working style
- Build one milestone at a time. End each with tests passing and a short note on what changed.
- When a bank's file format is uncertain, ask the owner for a real export rather than guessing.
- Write in plain English in the UI. Amounts as `€ 1.234,56` (nl-NL).
