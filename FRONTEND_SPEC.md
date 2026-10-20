# Build Spec: SIA Landing Page (`web/`)

> Hand this file to Claude Code. Build the whole page in this file without stopping to ask questions.
> Make reasonable choices where the spec leaves room and list them at the bottom of `web/README.md`.

## 0. Ground rules

- **Layout reference:** a docs-style landing page: a sticky left sidebar with nav and a scroll-spy, and a
  right column with a big headline, CTAs, a copyable setup prompt, a "works with" row and a dark hero visual.
- **Brand is SIA's own.** Don't copy any other company's logo, illustrations, copy, icons or exact styling.
  Colors, fonts, logo mark, hero visual and every word of copy below are SIA's.
- **No fake proof.** No invented customer logos, testimonials, user counts or benchmark numbers. The Results
  section uses only real numbers from `README.md`, or it's left out.
- **Zero build step.** Plain `index.html` + `styles.css` + `app.js`. No framework, no npm. FastAPI serves it.
- Works at 360 px wide with no horizontal scroll. Light and dark mode. Lighthouse accessibility ≥ 95.

## 1. Files

```
web/index.html     the page (Human view)
web/styles.css     tokens + layout + components
web/app.js         theme, Human/Agent toggle, scroll-spy, copy prompt, hero canvas, mobile nav
web/agent.md       the same content as plain markdown (Agent view + direct link)
web/llms.txt       short machine-readable summary + links (for coding agents)
web/assets/logo.svg
web/README.md      how it's served + default choices made
```

Serve it from the API. In `sia/api.py`, **after every route is defined**, add:

```python
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="web", html=True), name="web")
```

`/docs`, `/health` and `/v1/*` must keep working (routes registered before the mount take priority). Add a
test in `tests/test_sia.py`: `GET /` returns 200 and contains `self-improving`.

## 2. Design tokens

### Colors

| Token | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#F7F5F0` | `#0F1620` | page background (warm off-white, not pure white) |
| `--surface` | `#FFFFFF` | `#16202E` | buttons, pill right half, cards |
| `--surface-2` | `#EFECE5` | `#1C2838` | pill left half, setup bar base |
| `--ink` | `#16202E` | `#EEF1F5` | headline, body text |
| `--ink-2` | `#3A4658` | `#C3CBD6` | nav items |
| `--muted` | `#6B7686` | `#8C98A9` | labels, inactive scroll-spy |
| `--line` | `#E1DCD2` | `#2A3646` | sidebar border, dividers |
| `--accent` | `#1F8A7A` | `#3DB3A0` | primary CTA, active nav marker, links (teal) |
| `--accent-ink` | `#0F5C51` | `#8FE0D2` | text on `--accent-soft` |
| `--accent-soft` | `#E2F1ED` | `#123A35` | secondary button fill |
| `--signal` | `#E39B2D` | `#F0B457` | amber: status dot, logo dot, "learned" particles |
| `--hero-bg` | `#0E1B2A` | `#0A131E` | hero visual block |

Text contrast: `--ink` and `--ink-2` on `--paper` ≥ 7:1; `--muted` ≥ 4.5:1; white on `--accent` ≥ 4.5:1.
Check all of these and adjust lightness if any fails.

### Fonts (Google Fonts, `display=swap`)

| Role | Font | Weights | Notes |
|---|---|---|---|
| Display | **Fraunces** | 500, 600 | headline + wordmark; `font-variation-settings: "opsz" 96, "SOFT" 30` |
| Body | **IBM Plex Sans** | 400, 500, 600 | nav, paragraphs |
| Mono | **IBM Plex Mono** | 400, 500 | buttons, labels, code, "ON THIS PAGE" |

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght,SOFT@9..144,500..600,30&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
```

The serif display headline over mono UI labels is the main thing that sets this page apart. Keep it.

### Scale

| Token | Value |
|---|---|
| Headline | `clamp(40px, 4.4vw, 64px)`, line-height 1.08, letter-spacing -0.02em, weight 500 |
| Lead | 20px / 1.5, `--ink-2` |
| Nav item | 18px, gap 28px between items |
| Mono label | 13px, uppercase, letter-spacing 0.14em |
| Button label | 15px mono 500, uppercase, letter-spacing 0.12em |
| Radius | **0px everywhere** (square edges are part of the look), except the status dot |
| Space unit | 4px; use 8 / 16 / 24 / 32 / 48 / 64 / 96 |

## 3. Layout (desktop ≥ 1024 px)

```
┌──────────── sidebar (sticky, 100vh) ────────────┬──────────────── main ────────────────┐
│                         [logo] sia               │  [● pill text │ Read the changelog ↗] │
│                         [ Human | Agent ]        │                                       │
│                                                  │  Headline, 3 lines max                │
│                       ■ Home                     │  Lead sentence                        │
│                         How it learns            │  [■ START BUILDING] [READ THE DOCS ↗] │
│                         API                      │  [ COPY SETUP PROMPT        ⧉ │ ⌄ ]   │
│                         Pricing                  │  WORKS WITH  ◯ ◯ ◯ ◯ ◯                │
│                         Docs ↗                   │                                       │
│                         GitHub ↗                 │  ┌──────── hero canvas ────────┐      │
│                         - - - - - - - - - -      │  │   three orbiting loops      │      │
│                         ON THIS PAGE             │  └─────────────────────────────┘      │
│                         ━━ Mission               │  sections…                            │
│                         ╴  How it learns  …      │                                       │
└──────────────────────────────────────────────────┴───────────────────────────────────────┘
```

- `body` grid: `grid-template-columns: minmax(300px, 36vw) 1fr`.
- **Sidebar:** `position: sticky; top: 0; height: 100vh; border-right: 1px solid var(--line)`.
  Its inner block is **256 px wide, right-aligned**, with `padding-right: 72px` and `padding-top: 80px`.
- **Main:** `padding: 80px 64px 120px`, content `max-width: 800px`.

## 4. Components

### 4.1 Logo
`web/assets/logo.svg` is an open circular arrow (the loop) with an amber dot in the center:

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" aria-hidden="true">
  <path d="M26 16a10 10 0 1 1-3-7.1" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"/>
  <path d="M23.5 3.5v6h-6" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="16" cy="16" r="3.2" fill="#E39B2D"/>
</svg>
```
Mark 34 px + wordmark `sia` in Fraunces 600, 34 px, lowercase, gap 10 px. Links to `/`.

### 4.2 Human / Agent toggle
- Segmented control, 2 options, `role="radiogroup"`, arrow keys switch. 1px `--line` border, 4px inner padding.
  The selected option has a `--surface` fill + 1px border, 600 weight; the other is `--muted`.
- **Human:** the normal page. **Agent:** hides `main`'s visual content and shows `agent.md` rendered as
  plain preformatted text in mono, plus a "Copy as markdown" button. Store the choice in `localStorage`
  (wrapped in try/catch) and reflect it in the URL as `?view=agent`.

### 4.3 Primary nav
- Items: Home, How it learns, API, Pricing, Docs ↗ (`/docs`), GitHub ↗ (repo URL from a `data-repo` attr).
- Active item: an 8×8 px `--accent` square, 20 px to the left of the text; `aria-current="page"`.
- External links end with a 12 px ↗ glyph and open in a new tab with `rel="noopener"`.
- Hover: text goes `--ink`, no underline. Focus: 2px `--accent` outline, 4px offset.
- After the nav: a 1px **dashed** `--line` divider, 32 px margin above and below.

### 4.4 "ON THIS PAGE" scroll-spy
- Mono label, then a list of the main sections (Mission, How it learns, API in 60 seconds, Results, Pricing).
- On the left, a vertical "ladder": 6 px tick marks every 6 px in `--line`. The active entry gets a 28 px
  `--accent` bar and `--ink` text. Inactive entries get an 8 px tick and `--muted` text.
- Use `IntersectionObserver` (rootMargin `-40% 0px -55% 0px`). Clicking scrolls smoothly; reduced motion jumps.

### 4.5 Announcement pill
Two joined segments, height 40 px, no radius:
- Left: `--surface-2` fill, an 8 px `--signal` dot that pulses (opacity, 2 s, off under reduced motion),
  `--muted` text: **"New: playbook updates now need a measured win"**.
- Right: `--surface`, 1px `--line` border, `--ink` 500: **"Read the changelog ↗"**, linking to the repo's commits.

### 4.6 Headline + lead
- H1: **"Agents that learn your domain from every answer, correction and document."**
- Lead: **"Bring your own model key. Use it through a REST API, or self-host it with one Docker command."**
- Margins: pill → 48 px → H1 → 24 px → lead → 40 px → buttons.

### 4.7 Buttons
- **Primary:** height 56 px. Left: a 56×56 `--surface` square with a 2px `--accent` border holding the logo
  mark in `--accent`. Right: an `--accent` block with white mono label **START BUILDING WITH SIA**, 24 px side padding.
  Hover: the label block darkens to `--accent-ink` (light mode). Links to `#api`.
- **Secondary:** height 56 px, `--accent-soft` fill, `--accent-ink` mono label **READ THE DOCS ↗**, links to `/docs`.
  Instead of a full border, draw **four 10 px corner brackets** in `--accent` (pseudo-elements). This is the
  page's signature detail; reuse it on the hero frame.
- Gap 16 px. Both stack full-width under 560 px.

### 4.8 Setup prompt bar
- Width: the same as the two buttons together (min 100%, max 560 px). Height 44 px. Background: diagonal
  hatching, `repeating-linear-gradient(135deg, var(--surface-2) 0 6px, transparent 6px 12px)` over `--paper`.
- Left: mono **COPY SETUP PROMPT**. Right: a copy icon button, a 1px divider, then a chevron that opens a
  menu: **Claude Code · Cursor · curl**. Each copies a different variant of the prompt below.
- On copy: the label becomes **COPIED ✓** for 1.6 s. Announce it with an `aria-live="polite"` region.
  `navigator.clipboard` must sit in try/catch with a textarea fallback.
- Default prompt text (Claude Code / Cursor):

```text
Integrate the SIA self-improving agent API into this project.
- Base URL: read SIA_URL from env (default http://localhost:8000). Auth header: Authorization: Bearer $SIA_KEY.
- LLM key: send X-LLM-Provider and X-LLM-Key headers (default provider gemini). Never log or store the LLM key.
- Flow: POST /v1/agents to create one agent; POST /v1/agents/{id}/knowledge for docs;
  POST /v1/agents/{id}/run with {"task": ...}; POST /v1/agents/{id}/feedback with {"episode_id", "rating", "correction"}.
- Show the user the `answer`; keep `episode_id` so the user can rate it.
- Put the client in one module with typed request/response models and handle 400/401/404/502.
```

The curl variant is the `sia()` shell helper and the 6 calls from `README.md`.

### 4.9 Works with
- Mono label **WORKS WITH** in `--muted`, then a row of monochrome provider marks at 28 px, gap 24 px:
  Gemini, Claude, OpenAI, Groq, Ollama (all providers SIA actually supports).
- Use official monochrome SVGs (e.g. Simple Icons), follow each brand's trademark rules, and give each one
  `title` + `aria-label`. If a mark isn't available, use a mono text chip with the name instead. Never recolor
  a brand mark in another brand's colors.

### 4.10 Hero visual (canvas)
- Block: full main width, `aspect-ratio: 16/8.5`, `--hero-bg` fill, with corner brackets (as in 4.7) in `--signal`.
- `<canvas>` animation that tells SIA's story, **not** a generic particle cloud:
  three tilted elliptical orbits (the three learning loops) made of ~900 small square particles (2–3 px)
  that drift along their paths with slight noise.
  - teal `#3DB3A0` particles = knowledge, amber `#F0B457` = lessons, white at 40% = episodes.
  - About every 3 s, a few white particles turn amber as they pass the center ("a lesson is learned").
- Scale to `devicePixelRatio`, pause when off-screen (IntersectionObserver) or when the tab is hidden.
- `prefers-reduced-motion`: draw one static frame. Give the canvas `role="img"` and an `aria-label` that
  describes it.
- Must hold 60 fps on a mid-range laptop. Cap particles at 500 under 768 px wide.

## 5. Sections below the hero

Each section: `id`, an H2 in Fraunces 36 px, 96 px top margin, 1px `--line` top border with 32 px padding.

1. **Mission (`#mission`).** Two short paragraphs: most agents forget everything between calls, and
   fine-tuning is costly and provider-locked. SIA keeps what an agent learns as readable text (lessons,
   playbook, documents) that you can inspect, veto and roll back.
2. **How it learns (`#how`).** A 3-column grid of cards (1 column under 768 px). Square corners, 1px
   `--line`, 24 px padding, mono step number in `--accent`:
   - **01 Within a task:** draft, critique against your rubric, revise, keep the best.
   - **02 Across tasks:** short lessons that gain or lose trust based on outcomes; weak ones retire.
   - **03 Consolidation:** the playbook is rewritten, then adopted only if it beats the current one on your eval set.
   Under the grid, a single row of chips: **Feedback · Self-study · Rollback · BYO key**.
3. **API in 60 seconds (`#api`).** A code block (`--hero-bg` fill, Plex Mono 14 px, a Copy button) with
   create agent → add knowledge → run → feedback, taken from `README.md`. Tabs for curl / Python
   (`httpx`) / JavaScript (`fetch`). Write the Python and JS versions yourself, mirroring the curl calls.
4. **Results (`#results`).** A small table of the real demo runs from `README.md` (baseline, with lessons,
   candidate playbook, both runs), plus this caption verbatim: *"Six examples graded by the model are too
   noisy to prove a gain. We're expanding the eval set and will publish the next run here."* No other numbers.
5. **Pricing (`#pricing`).** Two cards: **Self-host: free.** MIT/your license, Docker, your keys.
   **Hosted: coming soon.** An email field that does nothing but `mailto:` (no backend). No made-up prices.
6. **Footer.** Logo, © 2026 SIA, links (Docs, GitHub, llms.txt, agent.md), theme switch (Light / Dark / System).

## 6. Theming

- Tokens on `:root`. Dark values go under `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {…} }`
  and again under `:root[data-theme="dark"]`.
- The theme switch sets `data-theme` on `<html>` and remembers it in `localStorage` (try/catch). An inline
  script in `<head>` applies it before first paint (no flash).
- `body { background: var(--paper); color: var(--ink); }`, set explicitly.

## 7. Responsive

| Width | Behaviour |
|---|---|
| ≥ 1280 | as specified |
| 1024–1279 | sidebar `minmax(260px, 30vw)`, main padding 48 px |
| < 1024 | sidebar becomes a 64 px top bar: logo left, toggle + hamburger right. Hamburger opens a full-height drawer with nav + ON THIS PAGE. Esc and the backdrop close it; focus is trapped while open. |
| < 560 | 16 px side gutters, buttons full-width, hero `aspect-ratio: 4/3`, setup bar label shortens to **SETUP PROMPT** |

## 8. Accessibility & quality

- Semantic landmarks: `<nav aria-label="Primary">`, `<nav aria-label="On this page">`, `<main>`, `<footer>`.
- A skip link as the first focusable element. Visible focus everywhere. All controls reachable by keyboard.
- Images and SVGs have alt text or `aria-hidden`. The heading order never skips a level.
- No layout shift from fonts: size fallback fonts with `size-adjust`.
- Total page weight (excluding fonts) < 150 KB. No third-party scripts, no analytics.
- `<title>SIA: Self-Improving Agent API</title>`, meta description, Open Graph tags, favicon from `logo.svg`.

## 9. `agent.md` and `llms.txt`

- `agent.md`: the whole page as clean markdown (headline, lead, sections, the API example, setup prompt).
- `llms.txt`: 10–20 lines: what SIA is, base URL pattern, auth headers, the 6 core endpoints, and links to
  `/docs` and `/agent.md`.

## 10. Done when (check each one)

- [ ] `python -m sia serve` → `http://127.0.0.1:8000/` shows the page; `/docs` and `/v1/...` still work
- [ ] `python -m pytest` passes, including the new `GET /` test
- [ ] Human/Agent toggle works with mouse and keyboard and survives reload
- [ ] The scroll-spy highlights the right section while scrolling; clicking jumps to it
- [ ] Copy setup prompt works for all 3 variants and shows COPIED ✓
- [ ] The hero animates, pauses off-screen, and is static under reduced motion
- [ ] Light, dark and system themes all render with no flash on load
- [ ] At 360 px wide: no horizontal scroll, drawer nav works, buttons stack
- [ ] Lighthouse accessibility ≥ 95; contrast checks from §2 pass
- [ ] No other company's logo, copy or illustration anywhere except the "works with" provider marks
- [ ] Commit after each step; never commit `.env`, `.venv`, `*.db`, `__pycache__`
