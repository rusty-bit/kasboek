# Kasboek — family finance app, local-first

Working name **Kasboek**. A self-hosted money app for one household (two adults, shared and own accounts) that runs in Docker on the NAS. It pulls transactions from Rabobank, Knab and comdirect, sorts them into categories, finds recurring bills and contracts, says what is safe to spend this month, forecasts what's coming, and tracks savings pots and habit goals.

Feature ideas are taken from Finanzguru, Rocket Money and the open-source **broke** repo (`nextwork-projects/broke`, MIT, Next.js + Supabase + Plaid). Bank data stays in a local SQLite file. Nothing goes to a cloud service except the PSD2 aggregator that fetches the bank data.

---

## 1. Principles

1. **Local only.** One SQLite file under `/data`. No telemetry. If an AI model is used, it is a local one (Ollama / OpenAI-compatible endpoint on the home AI box), or no model at all.
2. **Deterministic first, AI second.** Numbers come from code. An AI only words facts or proposes rules, which you confirm (broke does it this way).
3. **Read-only banking.** The app only reads data (AIS). It never makes payments (PIS).
4. **Light.** One container, under ~200 MB RAM, because it runs next to Immich/Paperless on a 3.8 GB NAS.
5. **Every algorithm is a pure function with tests.** Data in, result out, no DB access inside (broke's `src/lib/*.ts` pattern).
6. **Sign convention:** `amount < 0` = money out, `> 0` = money in. EUR only for v1.

---

## 2. What to take from each reference app

| Feature | Finanzguru | Rocket Money | broke | Kasboek |
|---|---|---|---|---|
| All accounts in one view | ✓ | ✓ | ✓ | **MVP** |
| Auto categorisation | ✓ | ✓ | ✓ rules + AI | **MVP**: rules → learned merchants → keyword list → optional local LLM |
| Recurring / subscription detection | ✓ Vertragserkennung | ✓ | ✓ `recurring-find.ts` | **MVP** (port broke's algorithm) |
| Contract folder with notice periods + reminder | ✓ Vertragsordner + Vertragswecker | – | – | **MVP**: contract fields on each recurring stream |
| Price-hike / new-subscription alerts | partial | ✓ | ✓ `subscriptions.ts` | **MVP** |
| "Free to spend" this month | ✓ frei verfügbares Einkommen | ~ | ✓ safe-to-spend | **MVP** |
| Balance forecast | ✓ Prognose (Plus) | – | ✓ projection | **MVP**: 90-day daily + 12-month outlook |
| Budgets per category | ✓ | ✓ | ✓ suggested from median | **MVP** |
| Savings goals / pots | ✓ Sparziele | ✓ Goals + autopilot | ✓ goal plans | **MVP**: pots with target, date, monthly plan |
| Habit reduction ("spend less on X, save the difference") | – | ~ | ~ | **MVP, new**: habit linked to a pot |
| Seasonal "this usually comes up" hints | – | – | – | **v2, new**: same month last year |
| Sinking funds for yearly bills | – | – | – | **MVP, new**: monthly reserve per annual/quarterly bill |
| Weekly check-in | – | – | ✓ `checkins.ts` | v2 |
| AI chat about your money | – | – | ✓ with privacy gate | v3 (local model only) |
| Partner sharing | – | ✓ shared accounts | ✓ `shares` | MVP-lite: account `owner` = shared / person; one household login |
| Transfers between own accounts hidden | ✓ | ✓ | ✓ `transfers.ts` | **MVP** |
| "Is this right?" queue for unsure categories | – | – | ✓ `unsure.ts` | **MVP** |
| Cancel subscriptions for you / bill negotiation | ✓ (DE) | ✓ (US, fee 35–60 %) | prompts only | **Skip.** Use a cancel-link + letter template |
| Credit score, investments, 401k/tax tools | – | ✓ | ✓ US-centric | **Skip** |

**Worth copying from broke's code** (read these files in `nextwork-projects/broke/src/lib`):
- `recurring-find.ts`: groups by direction + cleaned merchant name, median gap → frequency (weekly 7±2, biweekly 14±3, monthly 30.4±4, quarterly 91±10, yearly 365±15). Needs ≥3 dates (≥2 for yearly), ≥70 % of gaps on rhythm, and ≥80 % of amounts within 40 % of the median. Everyday categories (groceries etc.) need amounts within 3 %. Status MATURE / EARLY_DETECTION / TOMBSTONED (more than 2× the period + slack overdue).
- `budget.ts`: suggested budget = max(3-month median, spent so far this month), rounded up to €10 / €25 / €50 steps. "Well off" = at least €25 and 20 % away from the suggestion. Bar turns amber at 85 % and red when over. Daily allowance = remaining ÷ days left.
- `subscriptions.ts`: price hike = at least 2 earlier charges steady within 2 %, then a last charge more than 2 % and €0.50 higher.
- `transfers.ts`, `unsure.ts`, `rules.ts` (rule = all conditions must match; newest rule wins per field), `statement.ts` (CSV/OFX/QIF import with dedupe keys), `privacy.ts` (rounded amounts + placeholder names when anything reaches a cloud model).
- Keep the MIT notice in any file ported from broke.

---

## 3. Bank connectivity (checked Nov 2026)

| Route | Status | Use for |
|---|---|---|
| **Enable Banking, restricted production mode** | Free for your own accounts, non-commercial. You link your accounts in their control panel, and the app can then read only those. Covers Rabobank, Knab (and ING, ABN…) and German banks incl. comdirect via PSD2. Used by Actual Budget and the Firefly III importer. | **Primary connector** for all three banks |
| GoCardless Bank Account Data (ex-Nordigen) | Closed to new sign-ups, being wound down | Don't use |
| FinTS/HBCI (`python-fints`) | Works for comdirect with pushTAN (decoupled). Needs a FinTS product ID registered with Die Deutsche Kreditwirtschaft. | Fallback for comdirect only |
| comdirect REST API | Personal API access, photoTAN 2FA | Optional alternative |
| File import: CAMT.053 XML + CSV | Always works, all three banks export it | **MVP day one**: no keys needed, good for back-filling history |

### Enable Banking flow
- App ID + RSA private key (`.pem`). Each API call is signed with a JWT (RS256, `kid` = app id, `iss: enablebanking.com`, `aud: api.enablebanking.com`, short expiry).
- `POST /auth` {aspsp {name,country}, access.valid_until, psu_type "personal", redirect_url, state} → URL to the bank → the bank redirects with `?code=` → `POST /sessions` {code} → account UIDs → `GET /accounts/{uid}/transactions?date_from=` (page with `continuation_key`) and `GET /accounts/{uid}/balances`.
- Consent lasts at most ~180 days. Store `valid_until` and alert 14 days before it runs out.
- **Redirect without HTTPS on the LAN:** the production redirect must be HTTPS. Two options: (a) put it behind Nginx Proxy Manager with a certificate, or (b) register any HTTPS URL and offer a "paste the URL you landed on" box. The app takes `code` out of the pasted URL. Build (b) first.
- Map: `transaction_amount.amount` + `credit_debit_indicator` (DBIT → negative), `booking_date`, `creditor/debtor.name`, `creditor_account/debtor_account.iban`, `remittance_information[]`, `entry_reference` / `transaction_id` for dedupe. Skip `status: PDNG`.
- Sync: every 6 h, from the last booked date minus 5 days (late bookings), upsert on dedupe key.

### File formats (verify against real exports, see §8)
- **Rabobank CSV**: comma-separated, quoted, amount `+1,23` / `-12,30`. Columns include `IBAN/BBAN, Volgnr, Datum, Bedrag, Tegenrekening IBAN/BBAN, Naam tegenpartij, Omschrijving-1..3`. `Volgnr` = dedupe ref.
- **Knab CSV**: semicolon-separated, positive amount plus a `CreditDebet` D/C column, `Tegenrekeninghouder`, `Omschrijving`, `Referentie`.
- **comdirect CSV**: cp1252 encoding, preamble lines, `Buchungstag;Wertstellung;Vorgang;Buchungstext;Umsatz in EUR`. `Buchungstext` packs `Auftraggeber:/Empfänger: … Kto/IBAN: … Buchungstext: … Ref. …` together (`Ref.` has no colon and is a unique booking reference, used for dedupe). Pending rows say `offen`. *Verified against a real export (Nov 2026):*
  - One file (`umsaetze_<nr>_<timestamp>.csv`) holds several sections: `"Umsätze Girokonto"`, `"Umsätze Tagesgeld PLUS-Konto"` and `"Umsätze Depot"`. Each has its own header, `Neuer Kontostand` (closing balance) and `Alter Kontostand` (opening balance). The Depot section has different columns and is skipped.
  - The file contains no IBAN at all. Accounts are matched on `comdirect:<nr from filename>:<section>`.
  - Some transfers have `Empfänger: NameKto/IBAN: …` with no `Buchungstext:` label. Card payments (`Kartenverfügung`) have only `Buchungstext:`, which holds the merchant, and no counterparty.
  - The export seems capped at 500 bookings per section. The importer checks opening + bookings = closing and warns when the file was cut off.
- **CAMT.053**: `Stmt/Acct/Id/IBAN`, `Ntry/Amt`, `CdtDbtInd`, `BookgDt`, `TxDtls/RltdPties`, `RmtInf/Ustrd`, `AcctSvcrRef`. Closing balance from `Bal` type `CLBD`.

---

## 4. Architecture

```
┌──────────── kasboek container (python:3.12-slim) ─────────────┐
│ FastAPI + Jinja2 + HTMX (no JS build step)                    │
│  ├─ importers.py     CSV/CAMT → rows                          │
│  ├─ enablebanking.py JWT client, auth/session/sync            │
│  ├─ categorize.py    transfer → rule → memory → keyword → LLM │
│  ├─ recurring.py     streams + contracts + alerts             │
│  ├─ forecast.py      free-to-spend, 90-day, 12-month, seasonal│
│  ├─ goals.py         pots, habits, budgets                    │
│  ├─ scheduler        asyncio loop: sync, recompute, notify    │
│  └─ /api/summary     JSON for Home Assistant REST sensor      │
│ SQLite /data/kasboek.db   ·  /data/enablebanking.pem          │
└───────────────────────────────────────────────────────────────┘
      │ optional                      │ optional
      ▼                               ▼
 Ollama on the AI box            ntfy / Home Assistant
 (categorise unknowns,           (contract deadlines, low balance,
  word the weekly check-in)       price hikes, consent expiry)
```

- **Why Python:** `python-fints` exists, signing Enable Banking JWTs is trivial, and it's easy to keep pure functions under test. One process, no Postgres.
- **Auth:** the household is the unit. One login, set in the web interface: a fresh install starts with `kasboek` / `kasboek` (or `KASBOEK_USER` / `KASBOEK_PASSWORD`) and must choose its own after the first sign-in. Salted scrypt hash in `settings`, signed session cookie (30 days, invalid after a password change), 10 wrong tries per address → 15 minutes wait, `python -m app.auth reset` to recover. `KASBOEK_AUTH=off` for local development. Use behind Nginx Proxy Manager for HTTPS. The Enable Banking key is uploaded on the Banks page. Accounts carry `owner` (shared / person) for filtering "mine / yours / ours".
- **Backups:** nightly `sqlite3 .backup` into `/data/backups`, keep 14. Paperless/Immich backup routine can pick it up.

---

## 5. Data model

See `app/db.py` (already written). Tables: `accounts`, `categories` (kind income/expense/transfer, `essential`, `everyday`), `transactions` (`category_id` + `cat_source` = manual|transfer|rule|memory|keyword|llm), `rules`, `merchant_memory`, `recurring` (detected stream + contract fields `is_contract, contract_end, notice_days, notes, dismissed`), `pots`, `pot_moves`, `habits`, `budgets`, `settings`, `eb_sessions`.

Default categories are written for an NL/DE household: salary, Kindergeld/child benefit, allowances & tax refunds, side-business income/costs, mortgage, energy, insurance, telecom, taxes & municipality, groceries, eating out, fuel, car, public transport, kids, health, personal care, shopping, home/DIY, subscriptions, holidays, gifts, hobbies, cash, fees, other, own-account transfers.

---

## 6. Algorithms

### 6.1 Categorisation pipeline (first hit wins; `manual` is never overwritten)
1. **Transfer:** the counterparty IBAN is one of your own accounts → `transfer` (excluded from all totals).
2. **User rules:** text contains (any / counterparty / description / IBAN), optional direction, amount range, all must hold, newest rule first.
3. **Merchant memory:** when you re-categorise a transaction, store `normalised name + direction → category`, then apply it to all past and future uncategorised rows from that merchant.
4. **Keyword list** (direction-aware), seeded for NL/DE: Albert Heijn, Jumbo, Lidl, Aldi, Plus, REWE, Edeka, Picnic → groceries; Thuisbezorgd, Lieferando, McDonald's → eating out; Shell, Esso, Tango, Aral, Fastned → fuel; NS, Arriva, Deutsche Bahn → public transport; Eneco, Vattenfall, Essent, Enexis, WML → energy; Zilveren Kruis, CZ, VGZ, Centraal Beheer, Interpolis, HUK → insurance; KPN, Ziggo, Odido → telecom; Netflix, Spotify, Disney+, Videoland, iCloud → subscriptions; Belastingdienst / Finanzamt **out** → taxes, **in** → allowances & refunds; BsGW, gemeente, waterschap → taxes; Familienkasse / SVB kinderbijslag **in** → child benefit; Gamma, Praxis, Hornbach, OBI → home; Kruidvat, Etos, dm, Rossmann → personal care; Booking.com, Airbnb, KLM, Transavia, Eurowings → holidays; Geldmaat → cash; salaris / Gehalt / loon **in** → salary.
5. **Local LLM (optional):** send a batch of uncategorised rows (name, description, sign, rounded amount) with the category list. Accept only answers that name a valid key. Mark them `llm` so they show up in the "Is this right?" queue.
6. Otherwise `other` / `other_income`, which also goes to the queue.

Merchant normalisation: lowercase, drop card-terminal prefixes (`bea`, `betaalautomaat`, `ccv*`, `sumup *`, `zettle_*`, `pay.nl*`, `paypal *`), digits of 3+ characters, store numbers, city suffixes after `>` or `,`, punctuation.

### 6.2 Recurring streams and contracts
- Port broke's `findRecurring` (§2), with the sign flipped to our convention. Re-run after every import or sync and upsert on `key`. Keep the contract fields you entered by hand.
- **Contract folder:** mark a stream as a contract, then set end date and notice period. `cancel_by = contract_end − notice_days`. Alert at 30 and 7 days before it.
- **Alerts:** price hike (broke rule), new subscription (first seen in the last 45 days), stopped (TOMBSTONED), contract deadline, consent expiry, low projected balance, budget over.

### 6.3 Free to spend this month (Finanzguru-style)
```
free = Σ checking balances
     + expected recurring income still due this month
     − expected recurring expenses still due this month
     − pot plans not yet moved this month
per_day = free ÷ days left (today counts)
```
"Expected and still due" means the stream's next date falls in this month and no matching transaction has been booked yet this month (match on stream key, amount within 40 %). Also show variable spending so far against a typical month (median of the last 3 full months).

### 6.4 Balance projection
- **90 days, daily:** start from today's checking balance. Add each recurring occurrence on its date, stepping `next_date` by the frequency, keeping the day of month (31st → last day). Subtract the daily variable spend (median monthly variable ÷ 30.4) and the pot transfers on payday. Output the series, the lowest point and its date. Draw it as an SVG line with a threshold line.
- **12 months, monthly:** expected income, fixed costs (including yearly and quarterly bills in the months they land), typical variable spending, pot plans, and the net result.
- **Sinking funds:** for each quarterly or yearly bill, monthly reserve = average ÷ (3 or 12). Show the total as "set aside €X/month for irregular bills" (car insurance, road tax, BsGW, ANWB…).
- **Seasonal hints (v2):** for each coming month, a category whose spend in the same month last year was more than 1.5× its median month and more than €100 → "Last December you spent €640 on gifts".

### 6.5 Pots (savings goals)
- Fields: name, icon, target, target month, monthly plan, priority. Saved amount = Σ `pot_moves`.
- `needed_per_month = (target − saved) ÷ max(1, months until target)`. On track if the plan ≥ needed. Show the projected finish date at the current plan.
- **Feasibility:** total pot plans vs typical monthly surplus (median income − fixed − variable). If they don't fit, say so and order the pots by priority and date.
- v2: link a pot to a real savings account (Knab spaarrekening etc.) so the pots on that account always add up to its balance.

### 6.6 Habits ("reduce spending on X")
- Habit = category and/or name match + monthly target + optional pot.
- `baseline` = median of the last 3 full months. This month: spent so far, end-of-month estimate (spent ÷ days elapsed × days in month), status.
- At month end, if spend < baseline: "You spent €85 less on takeaway than usual. Move it to *Holiday*?" One click books a `pot_move`. This combines Rocket Money's "autopilot savings" with habit change, and it's the feature none of the reference apps have.

### 6.7 Budgets
- broke's `suggest` / `wellOff` / `tone` / `perDay` with € steps. A "Use suggested budgets" button for categories without a budget.

### 6.8 Tags (requested by the owner, 2026-11-19; planned, not built)
Categories stay: exactly one per booking, so totals, the donut, budgets and the forecast count every euro once.
Tags add the cross-cutting view: any number per booking, answering "for whom / for what" (`kids`, `animals`,
`garden`, `Italy 2025`, `bike`, `renovation`, `side-business`).
- Data: `tags(id, name UNIQUE, color)`, `transaction_tags(tx_id, tag_id, source)` with source manual | rule | memory,
  `rule_tags(rule_id, tag_id)`, `merchant_tags(norm, direction, tag_id)`. Manual tags are never removed by automation.
- Input: tag chips on each transaction row (autocomplete from used tags); bulk "tag all N results" on any search or
  filter (e.g. 1–21 Dec 2025 → `Italy 2025`); rules can set a category and/or add tags; merchant memory learns tags.
- Analysis: filters and saved analyses accept tags; a category also works as a filter term ("Kids" = category Kids OR
  tag `kids`), so nothing has to be converted. Dashboard category cards may include tags. Any chart over tags says
  that one booking can appear under several tags, and never shows tag shares as a pie adding up to 100 %.
- Later: split one booking into parts (an order of € 120 = € 80 garden + € 40 kids clothing), each with its own
  category and tags; splits sum to the booking amount and the original stays for dedupe.

---

### 6.9 Fair share (built 2026-11-19, `app/fairshare.py`)
Does each person carry their part of the household costs? Two people (names in settings), split **half each** or
**in proportion to income** (monthly net income per person, entered on the page).
- Paid by a person: expense rows on accounts they own, their cash wallet, and payments logged by hand
  ("Paid elsewhere": an account or card that isn't in Kasboek). Joint-account spending is paid in the ratio of
  each person's deposits into the joint account in the period (evenly when nobody deposited).
- Deposits: transfers from a person's own account, or money from an IBAN listed for them (their accounts outside
  Kasboek). Money from such an IBAN gets category `contribution` (income kind: household money coming in), source
  `household`. The page suggests unknown IBANs that paid into the joint account more than once.
- Any expense booking can be marked "only for <person>" (`transactions.personal_for`): carried fully by them,
  not a shared cost. Savings accounts, transfers, income, side-business costs and other owners never count.
- Cash: an ATM withdrawal (`cash`, now transfer kind) moves money into the owner's wallet; what is bought with it
  is logged by hand. The wallet shows withdrawn / logged / left, counted from the day fair share was set up.

## 7. Pages

1. **Overview:** free to spend + per day, 90-day balance line, next 10 bills, pots progress, budget bars, alerts.
2. **Transactions:** month/account/owner/category filter, search, inline category dropdown (re-categorising fills merchant memory; ticking "make rule" creates a rule), "Is this right?" queue.
3. **Recurring & contracts:** streams by monthly cost, contract fields, alerts, monthly total of fixed costs.
4. **Goals:** pots (add move, edit), habits, budgets.
5. **Forecast:** 12-month table, sinking funds, seasonal hints.
6. **Banks & import:** upload CSV/CAMT, Enable Banking connect + paste-URL fallback, accounts (name, kind, owner, included), last sync, consent expiry.
6b. **Fair share:** who carried what (period switch), log a cash/elsewhere payment, partner-deposit suggestions, cash wallets, logged payments, people and account owners.
7. **Settings:** rules, categories, payday, low-balance threshold, LLM endpoint, ntfy topic.
8. `GET /api/summary`: `{free_to_spend, per_day, lowest_balance_90d, lowest_date, alerts[], pots[]}` for a Home Assistant REST sensor.

---

## 8. Milestones (each ends with passing tests)

| # | Deliverable | Done when |
|---|---|---|
| M0 | Repo, Dockerfile, compose, CI-less `pytest`, synthetic sample files for the 3 banks | `docker compose up` serves an empty overview |
| M1 | File import + dedupe + accounts page | Importing the same file twice adds 0 rows. **Real exports from Rabobank, Knab and comdirect parse correctly** (the owner drops them in `a folder outside the repo`; never commit them) |
| M2 | Categorisation pipeline + transactions page + queue | ≥ 85 % of a real month categorised without manual work; a re-categorised merchant sticks |
| M3 | Recurring + contracts + alerts | Known subscriptions all found; no grocery store detected as a bill |
| M4 | Free-to-spend, 90-day and 12-month forecast, sinking funds | Hand-checked against one real month |
| M5 | Pots, habits, budgets | Habit month-end suggestion books a pot move |
| M5b | Tags (§6.8): per-booking tags, bulk tagging, rule/memory tags, tag filters and analyses | "What did the kids cost this year" works across categories without double-counting totals |
| M6 | Enable Banking connector + scheduler + consent alert | All three banks sync for a week unattended |
| M7 | HA sensor, ntfy, backups, deploy on the NAS via Portainer | Runs on the NAS from absolute `/volume1/Docker/kasboek` paths and survives a reboot |
| v2 | Seasonal hints, savings-account-linked pots, weekly check-in, FinTS fallback | — |
| v3 | Local-LLM chat over your data | — |

---

## 9. Decisions (answered by the owner, 2026-11-18)

1. **Login:** one shared household login. Accounts still carry `owner` for filtering.
2. **Host:** the NAS. The AI box is only the optional LLM endpoint.
3. **Enable Banking redirect:** HTTPS via Nginx Proxy Manager. The paste-URL box can stay as a fallback.
4. **side-business stays out of the household totals.** Its accounts get owner `side-business` and are excluded from totals, and the `business` / `side_income` categories don't count toward household free-to-spend.
5. **History:** 5 years where possible. Files give that: the real Rabobank export goes back to 2021-07. comdirect caps a file at ~500 bookings per account, so older periods need extra exports. Enable Banking only back-fills what the bank allows (90 days to 2 years).

## 10. Alternative worth knowing about
**Actual Budget** and **Firefly III** are mature, self-hosted, and both already sync EU banks through Enable Banking. Firefly III has piggy banks and rules. The case for building Kasboek anyway is the Finanzguru-style contract folder, free-to-spend, the forecast, and the habit → pot link, none of which either does well. A fallback plan is to use Actual/Firefly as the storage + sync layer and build these features on their API.
