# Kickoff prompt for Claude Code

Paste this into Claude Code from inside the unzipped `kasboek` folder:

---

Read CLAUDE.md and docs/SPEC.md. We're building Kasboek, a local-first family finance app.
`app/db.py` (schema + default categories) and `app/importers.py` (CSV + CAMT.053 import) are already written and tested against synthetic files in `samples/`.

1. Do M0: git init, Dockerfile, docker-compose.yml (absolute `/volume2/Docker/kasboek` paths for the NAS, plus a local dev variant), a minimal FastAPI app with an empty overview page, and pytest wired up.
2. Then M1: import page, accounts page, dedupe. I'll give you real exports from Rabobank, Knab and comdirect (and CAMT.053 if available) from `a folder outside the repo` to check the column mapping. Don't commit them.
3. Before starting M2, ask me the open decisions in SPEC §9.

Work milestone by milestone, run the tests after each one, and stop at the end of each milestone so I can try it.

---

Tips
- Put the Enable Banking `.pem` in `./data/` (git-ignored). Set up the free restricted production app in the Enable Banking control panel and link your Rabobank, Knab and comdirect accounts there **before** M6.
- broke's source is a useful reference: `git clone https://github.com/nextwork-projects/broke ../broke` and point Claude Code at `../broke/src/lib`.
