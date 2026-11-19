# Product

<!-- impeccable:product-schema 1 -->

## Platform

web (mobile web first; also used on desktop)

## Users
One household: two adults with shared and personal accounts (Rabobank, comdirect, Knab, PayPal) in the NL/DE border region. Both look at the same shared household view, mostly on a phone; filtering "mine / yours / ours" is available but not the default. They check where money went this month, what is still coming, and how their savings develop.

## Product Purpose
Kasboek is a self-hosted family finance app on the home NAS. It pulls bank transactions (Enable Banking, CSV/CAMT files), sorts them into categories, finds recurring bills and contracts, says what is free to spend this month, forecasts the balance, and (later) tracks savings pots and habits. Success: the household knows its real monthly costs and is never surprised by a bill or a low balance.

## Positioning
Local-first and private: bank data never leaves the house, no cloud APIs except fetching from the bank aggregator, no telemetry, no CDN assets. Numbers come from deterministic code; a local AI may only suggest categories that a person confirms. Inspired by Finanzguru (analyses, contract folder, free-to-spend) and Home Assistant (dark, glanceable card dashboards).

## Constraints
- FastAPI + Jinja2 + HTMX, no JS build step; charts are server-drawn SVG; all assets vendored.
- Runs on a 3.8 GB NAS: one container, under ~200 MB RAM.
- UI language English (plain words), amounts as `€ 1.234,56` (nl-NL). Sign convention: negative = money out.
- Never show a guessed number: when data is missing (e.g. less than one full month), say so.

## Dashboard requirements (confirmed 2026-11-19)
- Phone first, cards in one column; desktop shows the same cards in a grid.
- Dark theme for the whole app (Home Assistant style), with a light option.
- Month review, spending by category (switch month / year), monthly spending trend, balance across accounts, net worth over time, custom saved analyses (e.g. "Italy", "Bike": category and/or text and/or date range).
- Category cards from the start: Car, Housing & household, Groceries, Kids & daycare, Animals.
