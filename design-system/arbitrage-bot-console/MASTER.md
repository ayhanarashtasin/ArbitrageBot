# Arbitrage Bot Console: design system (master)

Source of truth for the dashboard at `app/api/static/dashboard.html`. Read this before adding a view, card or table.
It was produced with the `ui-ux-pro-max` skill (`.agents/skills/ui-ux-pro-max`). The query was
"financial operations monitoring dashboard" with density 8, motion 2 and variance 3. Only the verified parts were
kept: the skill's landing-page "patterns" do not apply to a monitoring console, and its web fonts and GSAP motion
are not allowed on this page.

## Hard constraints

- **Self-contained page.** No web fonts, CDNs, external scripts or stylesheets (`tests/unit/test_dashboard.py`
  enforces this). Icons are inline SVG.
- **Read-only.** The page only reads `/api/*`. It never calls an admin endpoint.
- **Semantic tokens only.** Components use the CSS variables below and never raw hex.

## Style

Minimalism, Swiss style. Dark theme by default, light theme via the toggle. The sidebar stays dark in both themes.
Keep flat surfaces, 1 px separators and no decorative gradients. The one exception is the hero KPI's faint accent wash.

## Colour tokens

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | `#020617` | `#F1F5F9` | page |
| `--surface` | `#0E1223` | `#FFFFFF` | cards |
| `--surface-2` / `--muted-bg` | `#131a2e` / `#1A1E2F` | `#F8FAFC` / `#EEF2F7` | table heads, tracks, chips |
| `--border-soft` | `#1e293b` | `#E2E8F0` | separators (decorative) |
| `--control` | `#64748B` | `#64748B` | outlines that identify a control (buttons, search, selected filter): at least 3:1 |
| `--text` / `--text-2` / `--text-muted` | `#F8FAFC` / `#CBD5E1` / `#94A3B8` | `#0F172A` / `#334155` / `#475569` | text |
| `--pos` (`-fill`, `-soft`) | `#4ADE80` | `#166534` | gain, success, passed |
| `--neg` | `#F87171` | `#B91C1C` | loss, failure, disconnected |
| `--warn` | `#FBBF24` | `#92400E` | waiting, stale, shadow research |
| `--info` | `#7DD3FC` | `#0369A1` | information, links, entering |
| `--accent` | `#22C55E` | `#15803D` | active nav, focus of attention, primary series |

Measured contrast: all text is at least 4.5:1 on every surface and chip in both themes. The lowest pair is
dark red on its chip at 5.78:1. Control outlines are at least 3.9:1.

**Never colour alone.** Money always carries a sign (`+$0.01`, `−$0.34`). States and outcomes are chips with an
icon and a word. Chart thresholds are labelled lines, and a second series is dashed.

## Typography

- Text uses the humanist sans stack: `"Fira Sans", "Segoe UI Variable Text", "Segoe UI", system-ui, …`.
- Numbers use the tabular mono stack: `"Fira Code", "Cascadia Mono", Consolas, ui-monospace, …`, always with
  `.num` (tabular figures).
- Table headers stay in the sans face, even over numeric columns.
- Sizes: body 15 px, tables 13.8 px, captions at least 12 px, h1 24 px, h2 16.3 px, KPI 26 px, hero P&L 37 px.

## Spacing, shape, motion

- Spacing scale `--s1…--s6` = 4, 8, 12, 16, 24 and 32 px (dense dashboard rhythm).
- Radius 6 px for controls, 10 px for inner boxes and 14 px for cards.
- Motion is limited to 160 ms colour and background transitions and the skeleton shimmer. All of it is switched
  off under `prefers-reduced-motion`.

## Layout and navigation

- A 232 px sidebar holds the 7 views: Overview, Trades, The plan, Scanner, Market, Decisions, Settings.
- Every view has a hash route (`#/plan`, `#/trades/<id>/<tab>`), so deep links and the Back button work. The
  active item gets `aria-current="page"`.
- Below 960 px the sidebar becomes a horizontally scrolling top bar. Below 640 px grids stack, button labels hide
  (the `aria-label` remains) and wide tables keep a 660 px minimum inside their own horizontal scroller.
- **The page itself must never scroll sideways.** Verify at 390 and 820 px with a frame of that exact width.
  Headless Chrome on Windows will not render narrower than about 500 px.

## Components

- **card** has a `card-head`: an h2, then either a muted subtitle or a link to the full view.
- **kpi / stats**: four big KPI tiles at most. Secondary numbers go in the `stats` strip, and word values there use
  `.v.txt`.
- **chip** is for state and outcome (icon + word). **tag** is a small label (shadow, gated, ×count).
- **table** is always wrapped in `.table-wrap`. Headers are sticky, numeric cells use `td.num`, and dates use
  `td.nowrap`. A clickable row also contains a real link, so it works from the keyboard.
- **tabs** follow the ARIA tabs pattern (roving tabindex, arrow keys, Home and End). The selected tab is part of
  the URL.
- **seg** is a filter button group with `aria-pressed`.
- **empty** is an empty state: an icon plus a sentence saying what will appear and when. Never leave blank space.
- **skeleton** is the stable placeholder shown while data loads.

## Content rules

- Plain language, with no machine codes on screen. Map check names through `CHECK`, alert kinds through `ALERT`,
  exchange adapter names through `VENUE`, and free text through `human()`.
- Group repeats: consecutive identical risk events and alerts become one row with `×n since …`.
- Charts follow the space they occupy (`slotWidth()`), so axis labels stay about 11 px. Each chart shows its
  current value in words above it.

## Adding something new

1. Put it in the view it belongs to. Do not add another section to Overview unless it is a headline number or a
   short link-out card.
2. Reuse the existing component classes and tokens above.
3. Add an empty state and a loading skeleton.
4. Check contrast if you introduce a new colour pair, and check for horizontal overflow at 390 and 820 px.
