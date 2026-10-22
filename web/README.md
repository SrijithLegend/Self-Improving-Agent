# `web/`: the SIA landing page

Plain `index.html` + `styles.css` + `app.js`. No framework, no npm, no build step.

## How it's served

`sia/api.py` mounts this folder at `/` with FastAPI's `StaticFiles(html=True)`, **after** every route is defined, so
`/docs`, `/health`, `/v1/*` and `/dashboard` keep priority. Run it with:

```bash
python -m sia serve          # http://127.0.0.1:8000/  (landing page)
                             # http://127.0.0.1:8000/dashboard, /docs, /v1/...
```

To work on the page without the API, any static server works (`python -m http.server -d web`), except that `/docs`
links will 404.

| File | What it is |
|---|---|
| `index.html` | the page (Human view) |
| `styles.css` | tokens, layout, components, light/dark themes |
| `app.js` | Human/Agent toggle, scroll-spy, copy prompt, code tabs, hero diagram, mobile drawer |
| `site.js` | shared by every page: the saved theme (before first paint) and the contact links |
| `privacy.html`, `terms.html` | served at `/privacy` and `/terms`; linked from every footer, the signup field and the dashboard |
| `404.html` | served by the mount for any unknown page (unknown `/v1/...` paths still get JSON) |
| `agent.md` | the same content as markdown (Agent view and a direct link) |
| `llms.txt` | short machine-readable summary for coding agents |
| `assets/logo.svg` | the loop mark; also the favicon |
| `assets/og.png` | the 1200×630 link preview image |

Weight without fonts is about 75 KB. No third-party scripts and no analytics; the only outside request is Google
Fonts.

## Checked

- `pytest`: `GET /` returns 200 with the DevTools H1 and without the old one; `/docs`, `/health`, `/v1/me`, `/dashboard`,
  `/agent.md` and `/llms.txt` still answer.
- Lighthouse 12.6 (desktop, mobile, and mobile in dark mode): accessibility 100, best practices 100, SEO 100.
- In Chrome: the toggle by mouse and arrow keys, which survives a reload and sets `?view=agent`; the scroll-spy
  while scrolling, when clicking and at the page bottom; all three prompt variants copied, with COPIED and a
  live-region message; the hero token pauses off-screen and rests in place under reduced motion; light, dark and
  system themes; at 360 px no horizontal scroll, the drawer opens, traps focus and closes on Esc, the backdrop or a
  link, and the buttons stack.

## Default choices made

- **Colors adjusted for contrast** (§2 asked for this when a check fails):
  - light `--muted` is `#5E6978`, not `#6B7686`, which was 4.2:1 on paper; it's now 5.1:1, and 4.7:1 on
    `--surface-2`.
  - light `--accent` is `#187A6C`, not `#1F8A7A`, which gave white text 4.2:1; it's now 5.2:1.
  - In dark mode white on `#3DB3A0` is only 2.6:1, and no single teal is dark enough for white text yet light
    enough for links on the dark page. So dark mode keeps `#3DB3A0` and puts dark text (`--on-accent: #0A131E`,
    7.3:1) on the primary button.
- **Dashboard moved to `/dashboard`.** `/` is now the landing page. `sia dashboard` prints the new URL.
- **Static path** is resolved from the package (`<repo>/web`), not the working directory, and the mount is skipped
  when the folder is missing (a `pip install` of the SDK ships no landing page).
- **One product.** The page sells SIA DevTools, as the README does (docs/LAUNCH_SPEC.md, Task 1). The managed agents API
  still exists and is documented in the README, but it isn't marketed here.
- **Results** is the README "Demo results" table for `examples/devtools_demo.py`, shortened to five rows, with its
  "What this does and does not show" caveat.
- **Pricing.** The Self-host card names the project license, FSL-1.1-ALv2 (`LICENSE.md`). No prices are shown for
  hosted.
- **Hosted waitlist** opens a `mailto:` addressed to `<body data-contact="">`. It's empty by default, so the
  visitor's mail app opens with a blank To: field. **Set `data-contact` to a real address before publishing.**
- **Provider marks** are the monochrome Simple Icons 13.21.0 paths for Google Gemini, Claude, OpenAI and Ollama,
  inlined with `title` and `aria-label` and drawn in the page's ink color. Simple Icons has no Groq mark, so Groq
  is a mono text chip.
- **Cursor prompt variant** is the Claude Code prompt wrapped as a Cursor project rule (frontmatter, so it can be
  saved as `.cursor/rules/sia.mdc`). The **Shell** variant is the install, demo, dashboard and gate commands.
- **Hero.** A static, labeled SVG diagram of the five steps (capture, feedback, dataset, eval, release). The one
  animation is an amber square, one failed run, that travels from capture to release every 14 s. It pauses
  off-screen or in a hidden tab, and rests between feedback and dataset under reduced motion.
- **How it works** is five steps in order, each with the real result it produced in the README demo run of
  `examples/devtools_demo.py` (20 October 2026). The excerpts are result fields, not raw terminal output.
- **Scroll-spy.** The `IntersectionObserver` (rootMargin `-40% 0px -55% 0px`) triggers an update, and the update
  picks the last section whose top is above the band; Pricing becomes active once the page bottom is reached.
  Firing only on intersection skipped sections on fast scrolls and could never reach the last short section.
- **In-page links** move focus to the section heading (for keyboard and screen-reader users) and close the drawer.
- **Agent view** fetches `/agent.md` on first use and shows it in a `<pre>`, with "Open raw" and "Copy as
  markdown". The footer and sidebar stay.
- **Font fallbacks**: Georgia, Arial and Courier New `@font-face` aliases with `size-adjust` and ascent/descent
  overrides, to limit layout shift while the web fonts load.

## docs/DESIGN_RULES.md

The page and the dashboard follow [`docs/DESIGN_RULES.md`](../docs/DESIGN_RULES.md). Where a rule is bent, this is the one-sentence reason
(Appendix B):

- **Corner brackets** appear in two places only: the secondary button and the hero frame. They're drawn with
  solid-color `linear-gradient` strokes, not color sweeps, because that's the only way to paint four corners with
  one pseudo-element.
- **The scroll-spy tick ladder** (a `repeating-linear-gradient`) is a measured scale, one tick per 6 px, that shows
  where you are on the page; it isn't decoration.
- **Code blocks and the hero use a dark fill** in both themes so code and the diagram read the same everywhere.
  There's no window chrome and no typing effect.
- **One shadow**, reserved for the prompt-variant menu, which floats over the page.
- **Type**: four sizes (`--t-display`, `--t-title`, `--t-body`, `--t-small`) on the page, and four in the
  dashboard (`--t-1` to `--t-4`). Text inside the SVG diagram is in SVG units and scales with it.
- **Contact** links read `<body data-contact>`. Until it's set they point to the GitHub issues page. **Set it on
  every page before publishing**, since the privacy policy relies on it for data requests.
- **Legal pages** describe what the code actually stores and logs. They were written without a lawyer: have one
  review them, and confirm the refund terms, before taking payments.

