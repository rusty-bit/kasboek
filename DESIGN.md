# Design

Recorded from the built app (2026-11-19). Source of truth: `app/static/kasboek.css`, `app/templates/base.html`,
`app/templates/_icons.html`, `app/templates/overview.html`. Product context: PRODUCT.md.

## World
A Home Assistant-style card dashboard: near-black ground, graphite cards, big tabular numbers with a quiet label,
one sky-blue accent for actions. Dark is the default (the household checks it on the phone, often in the evening);
light is a full alternative, switched with the sun/moon button and remembered in the `theme` cookie (set
server-side on `<html data-theme>`, so there is no flash).

## Tokens (`:root` = dark, `[data-theme="light"]` = light)
| Role | Dark | Light |
|---|---|---|
| `--bg` ground | `#111317` | `#eef0f3` |
| `--card` surface | `#1c1f25` | `#ffffff` |
| `--raised` inputs, chips, icon wells | `#262a32` | `#f3f5f7` |
| `--line` hairlines, borders | `#2f343d` | `#e0e3e8` |
| `--ink` / `--muted` / `--faint` | `#e7e9ec` / `#9ea4ad` / `#6c727c` | `#1b1e23` / `#5b626c` / `#9aa1aa` |
| `--accent` actions, links, focus | `#5aa9f8` | `#1769c2` |
| `--neg` money out in lists, `--pos` good change, `--warn` | `#f28b82` `#6fd39a` `#f3b664` | `#b3261e` `#1b7a43` `#9a5800` |
| `--s1`…`--s8` chart series | validated dark steps | validated light steps |

`--faint` is for decoration and disabled states only, never body text.

## Data colour
- Series `--s1..--s7` are the reference categorical palette (blue, orange, aqua, yellow, magenta, green, violet), validated
  against `--card` in both modes; `--s8` is a neutral for "Other". Light mode has three hues under 3:1, so every chart
  ships visible labels with amounts (legend list) or a table view.
- Spending uses eight **fixed groups** (`analytics.GROUPS`): Housing & bills s1, Food s2, Transport s3, Family, health & pets
  s4, Home & garden s5, Leisure & travel s6, Shopping s7, Other s8. Colour follows the group in every period; the donut
  always draws groups in this order, so neighbouring slices are the validated neighbour pairs. Accounts get slots by
  account order.
- Money out is red only in transaction lists and tables; dashboard spending totals are plain ink.

## Type
System sans stack (`system-ui, -apple-system, Segoe UI, Roboto`). 15px body. Headings 1.5rem / 1.25rem section heads /
1rem card titles, weight 600–650. Hero number 2.25rem 700, card numbers 1.75rem 700, both tabular numerals with
-0.02em tracking. Small text .8125rem. SVG chart text is in viewBox units and is enlarged under 520px.

## Shape and depth
Cards: 16px radius, 1px `--line` border, soft two-layer shadow (`--shadow`). Controls: 10px radius. Pills (nav tabs,
segmented control, chips, round step buttons): 999px. Messages (`.notice` `.error` `.ok`): tinted panel with a status
dot, never a thick side stripe.

## Components
- **Top bar**: sticky, translucent ground with blur, brand + pill tabs (scroll sideways on a phone) + theme button.
- **Tile title**: 34px icon well (`.ic`, tinted by group `g1..g8`) + title. No eyebrow labels above headings.
- **Period switch**: segmented Month | Year, previous/next round buttons, label; swaps `#spending` with htmx
  (`hx-select`), pushes the URL, keeps scroll. Future periods are disabled.
- **Donut** (`charts.donut`): 168px ring, 22px thick, 1.6° surface gaps, centre total; hover dims the other slices;
  every slice has a `<title>`; legend sorted by amount with share and amount.
- **Bars** (`charts.stacked_bars`, `charts.bars`): rounded data end only (`round_top`), fixed costs s1 under day-to-day s2,
  selected month marked with an accent band; each month is a link to that month.
- **Line** (`_balance_chart.html`): 2px accent line, dashed `--neg` low-balance limit, lowest point labelled with a card-
  coloured halo, hover readout, "Show as a table".
- **Icons**: one hand-drawn SVG set in `_icons.html`, 24px grid, 1.8 stroke, round joins. No emoji or unicode glyphs.

## Layout
`.dash` is a 12-column grid; cards `w4` / `w8` span 12 on phones, 6 from 760px, 4/8 from 1080px. Cards align to the
top (no stretched empty space). Everything shrinks (`min-width: 0`); amounts never wrap. Max content width 1180px.

## Motion
One authored moment: the spending section dims and blurs briefly while a new period loads, then settles in 220ms
(expo ease-out). Hover transitions 150ms. `prefers-reduced-motion` turns the settle off.

## Browser surfaces
Themed selection, caret, `accent-color`, `scrollbar-color`, `:focus-visible` ring in `--accent`, `color-scheme` per
theme, `theme-color` meta follows the theme.
