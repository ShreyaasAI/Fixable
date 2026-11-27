# M11 contract: UI redesign ("workbench")

- **Last updated:** 2026-11-27
- **Status:** Built. Design and component decisions approved by the user (2026-11-26/27).
- **Scope:** `web/` only. The API contract, `api.ts`, `sse.ts`, `auth.ts`, the env vars and every screen's behaviour are unchanged.
- **Figma:** https://www.figma.com/design/hyhgKmjD9RNYZMYFy0DAIE (tokens, components, dark screens at 1440 and 375; Admin 375 and the light copies were not drawn because the Figma Starter-plan MCP limit was reached; light mode is verified in the code screenshots).
- **Overrides:** this contract replaces m10a §3 (design system) and m10a §0 decision 12 (system fonts). m10a §2.1 test ids still hold, with the amendments in §5.

## 1. Direction

A hand-built workbench rather than a template dashboard:

- **Palette.** Dark mode uses warm near-black ink tones. Light mode uses paper tones. One amber accent; sage green means success, so the sign-in line reads "Red test in. *Green PR out.*"
- **Type.** Sansita ExtraBold (display; a free look-alike of Britannic Bold, A5) for headings and ledger numbers. IBM Plex Sans for body text. JetBrains Mono for code, ids, labels and nav.
- **Signature details:**
  - Status shows as a rubber stamp: mono caps in a ruled box, never colour alone.
  - The quota is a three-cell ledger.
  - The timeline is a numbered log on a rail.
  - The sign-in hero sits on a cutting-mat grid with crop marks and a "fig. 1" caption.
- **3D, once.** Only the sign-in hero is 3D: an isometric bench of blocks where an amber block drops into the one empty slot. No other screen has 3D.

## 2. Decisions (user, grilling rounds 1–2)

| # | Decision |
|---|---|
| D1 | Tailwind CSS v4 through `@tailwindcss/vite`. Tokens are CSS variables in `styles.css`, mapped in `@theme inline`. |
| D2 | shadcn primitives (Radix + cmdk), copied in and restyled: 4 px radius, 1 px borders, no shadows. None of shadcn's default zinc theme ships. Used: Button, Popover, Command, AlertDialog, Checkbox, Label, Input/Textarea, RadioGroup, Tooltip. |
| D3 | Components: `Button`, `Stamp`/`StatusStamp`/`ExitStamp`, `Field`, `Ledger`, `Panel`, `PageHead`, `Wordmark`, `ThemeToggle`, `RepoCombobox`, `ConfirmDialog`, `Alert`, `Footer`, `Hero3D`. |
| D4 | `web/src/ui/` holds the components, one file per app component. The shadcn primitives sit together in `primitives.tsx`. `components.tsx` is removed (replaces m10a A8). |
| D5 | The repo picker is a combobox: Popover + Command, searchable. |
| D6 | A three-state theme toggle (System / Dark / Light), saved under localStorage `fixable-theme`. `index.html` sets `<html data-theme>` before first paint. "System" follows `prefers-color-scheme` live. |
| D7 | The hero uses react-three-fiber, kept small: no drei, one `Canvas`, orthographic camera. It is lazy-loaded, so three.js is never in the entry chunk. |
| D8 | The hero degrades in four ways: reduced motion renders one still frame (`data-motion="static"`); no WebGL shows the static SVG (`data-renderer="svg"`); the SVG also stands in while the chunk loads; rendering pauses off-screen. The hero is `aria-hidden`. |
| D9 | Under 768 px the nav wraps to a second row under the top bar. There is no hamburger. |
| D10 | Fonts are self-hosted through `@fontsource`, so the page makes no third-party request. Sansita comes in 800 and 800 italic; Plex Sans comes in 400/500/600 and JetBrains Mono in 400/500. |
| D11 | UI motion uses `motion` (MIT): route fade, task-row and timeline-row entrances, a spring on decision cards, a "stamp" on status change, and preflight result entry. `<MotionConfig reducedMotion="user">` and a CSS reduced-motion rule turn it off. |

## 3. Tokens

| Token | Dark | Light |
|---|---|---|
| `--bg` | `#13110E` | `#F2ECE1` |
| `--surface` | `#1C1915` | `#FAF6EE` |
| `--raised` | `#25211B` | `#FFFFFF` |
| `--line` | `#3B352C` | `#D8CDB9` |
| `--fg` | `#EEE7DA` | `#1F1A13` |
| `--muted` | `#A69D8C` | `#5C5344` |
| `--accent` / `--on-accent` | `#F0A33C` / `#1A1208` | `#9A4E06` / `#FFFFFF` |
| `--ok` | `#93C98E` | `#2E6A33` |
| `--danger` / `--on-danger` | `#F0806A` / `#1A0A06` | `#A8321E` / `#FFFFFF` |
| `--warn` | `#E9C46A` | `#7A5A00` |
| `--ring` | `#EEE7DA` | `#1F1A13` |

Every text pair is at least 4.5:1. The m10a accessibility rules still hold: a visible focus ring, labels on every field, errors next to the field and in a focused summary, and 44 px primary actions.

## 4. Tests

| File | Proves |
|---|---|
| `tests/test_m11_web_bundle.py` (`e2e`) | three.js is not in the entry chunk but ships in a lazy chunk; no Google Fonts URL; Sansita, Plex and JetBrains woff2 files are bundled and Fraunces is not; the CSS has both themes |
| `tests/e2e/test_m11_ui.py` | the theme toggle switches and survives a reload; System follows the OS live; the hero is only on sign-in and is `aria-hidden`; reduced motion gives `data-motion="static"`; the combobox marks read-only repos disabled and filters; every screen at 375 and 1440 px in dark and light has no sideways scroll (with `SWF_SCREENSHOT_DIR` set, it also saves the PNGs) |
| `tests/e2e/test_ui.py`, `conftest.py` | the 12 m10a tests, with the repo-picker steps rewritten for the combobox (A1) |

## 5. Amendments to m10a §2.1

- **A1, repo picker.** `repo-select` is now the combobox trigger (`role="combobox"`). Repos are `role="option"` and carry `aria-disabled` when `can_push` is false. Tests open the trigger and pick by role. On the keyboard, Enter opens it, typing filters, and Enter picks.
- **A2, confirm dialog.** `confirm-dialog` sits on the shadcn AlertDialog content (`role="alertdialog"`) instead of a native `<dialog>`. Focus opens on Keep (`confirm-no`), Escape keeps, and focus returns to the opener.
- **A3, noise toggle.** `noise-toggle` is a Radix checkbox (`role="checkbox"`).
- **A4, theme and hero ids (new).** `theme-toggle` is a radiogroup with radios named System, Dark and Light. `hero-3d` (`data-motion`, `data-renderer`) appears on sign-in only.
- **A5, display font (user, 2026-11-27).** The user asked for Britannic Bold. It is a licensed Monotype font, so the display face is Sansita ExtraBold (SIL OFL, `@fontsource/sansita`), listed among the closest free matches. `--font-display` keeps `"Britannic Bold"` as a local fallback. Not an m10a id change.

## 6. Results

See `docs/HANDOVER.md` (M11 row) for the commands run and their output. Screenshots are in `docs/software-factory/m11-screenshots/` (`<screen>-<width>-<theme>.png`).
