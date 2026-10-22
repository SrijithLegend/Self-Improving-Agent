# DESIGN_RULES.md: Don't Ship a Site That Looks Vibecoded

> Give this file to Claude, ChatGPT or Cursor **before** asking for any UI. It overrides default styling
> choices. If a rule here conflicts with a spec, follow this file and tell me about the conflict.

## 0. The one idea behind all 30 rules

A site looks "vibecoded" when **every choice is the AI's default instead of a decision**. Inter, purple
gradients, three feature cards, emoji bullets, glass blur: none of these is bad on its own. Together, they
tell the visitor *nobody thought about this*. That's fatal for a product that asks for trust (and an API key).

So the test for every element is:

> **"Would this exist if the AI hadn't suggested it? Can I say why it's here, for *this* product?"**

If the answer is no, cut it or replace it with something specific to the product.

Three rules sit above everything else:

1. **Show the real thing.** Real product output, real numbers and real code beat any decoration.
2. **One accent, one idea per section, one hero element.** Restraint reads as confidence.
3. **Missing basics look worse than ugly styling.** A page without a privacy policy, a terms page or
   loading states looks unfinished, however pretty it is.

---

## 1. Color

| # | Don't | Why it reads as vibecoded | Do instead |
|---|---|---|---|
| 1 | **Harsh gradients** (purple→pink→orange text, gradient buttons, gradient backgrounds) | It's the default "AI startup" look from 2023–2026 | Flat color. If you need depth, use a 2–4% lightness shift in the same hue, never a hue sweep. |
| 4 | **Rainbow coloring** (each card or icon a different color) | Color with no meaning is noise | Color carries meaning only: one accent for action, one for status. Everything else is neutral. |
| 20 | **Purple and black** | The single most common AI-generated palette | Choose a palette from the product's world (for SIA: ink navy, teal, amber, from the deck). |
| 29 | **Neon colors** (glowing cyan or magenta, text-shadow glow) | Looks like a template, and hurts contrast | Saturated color only on small areas (a dot, a button), never on large surfaces or text. |
| 30 | **Basic pastel colors** (mint/lavender/peach cards) | The "soft SaaS" template default | Tinted neutrals derived from your accent (e.g. accent at 6–10% on paper). |
| 3 | **Pure white `#FFFFFF` background** | The unchosen default | A deliberate paper tone (`#F7F5F0`, `#F4F4F2`) or a committed dark mode. White is fine for cards *on* paper. |

**Palette rule:** at most **1 neutral family + 1 accent + 1 signal color**. Write them as CSS tokens and
use no hex value outside the token file. Check contrast: body text ≥ 7:1, small text ≥ 4.5:1.

## 2. Typography

| # | Don't | Do instead |
|---|---|---|
| 10 | **Inter / Geist / Space Grotesk** as the only font, chosen because it's the default | Pick a pairing with a reason: a characterful display face (a serif like Fraunces, or a grotesk with quirks) + a workhorse body face + a mono for technical labels. Inter is a good font; it's banned here only because it's the default. |
| 9 | **Em dashes (—)** sprinkled through copy | Use periods, commas, colons or parentheses. Short sentences. |
| 15 | **"It's not X, it's Y"** phrasing ("It's not a tool, it's a teammate") | State what it does in plain words: "SIA stores what your agent learns as text you can read and roll back." |

**Copy rules (the words matter as much as the pixels):**
- No hype words: *revolutionary, seamless, supercharge, unlock, next-gen, effortless, game-changing, leverage.*
- No "Introducing…" / "Meet X" / "Say goodbye to…" / "The future of…" openers.
- Every claim is specific and checkable ("adopts a new playbook only if it scores higher on your eval set"),
  never vague ("gets smarter over time").
- Headline ≤ 12 words, says what it does for whom. Write it yourself. Don't accept the AI's first draft.
- Type scale: max 4 sizes on a page. Line length 60–75 characters. Never center paragraphs.

## 3. Icons, emojis and decoration

| # | Don't | Do instead |
|---|---|---|
| 2 | **Lucide icons** (or any default icon set used everywhere) | Fewer icons. Where one helps, use a small custom set drawn on a consistent grid, or no icon at all. Text labels beat icons. |
| 7 | **Emojis** in headings, bullets or buttons (🚀 ✨ 🔥 ⚡) | None. Zero. In UI and in copy. |
| 16 | **Checkmark bullets** (✅ / ✓ lists everywhere) | Plain sentences, or a real table comparing real things. Checkmarks only in a true yes/no comparison. |
| 24 | **Sparkle icons** (✨ "AI" sparkles) | Never signal "AI" with a sparkle. Show what the AI produced instead. |
| 22 | **Radial orbs** (blurry glowing blobs behind the hero) | No ambient blobs. If the hero needs a visual, it shows the product or a diagram of how it works. |
| 23 | **Dot grids** (dotted background pattern) | Plain background. Texture only if it means something (e.g. a measured grid in a data tool). |
| 11 | **Colored left stripe** on cards or callouts | Separate cards with spacing, a tint or a 1px border on all sides. |
| 8 | **Liquid glass** (`backdrop-filter: blur`, frosted panels) | Solid surfaces. Blur only for a real overlay (a modal backdrop), never as a style. |

## 4. Shape, depth and motion

| # | Don't | Do instead |
|---|---|---|
| 5 | **Drop shadows** on every card and button | Flat, with 1px borders. At most one subtle shadow level, reserved for things that float (menus, modals). |
| 19 | **Soft corner radius** (12–24px rounded everything) | Pick one radius on purpose and apply it everywhere: 0px (sharp, editorial), or 2–4px. Never mix. |
| 28 | **Hover animations** (cards lifting, scaling, glowing) | Hover changes color or underline only, within 120ms. Things that aren't clickable never react to hover. |
| 25 | **Animated arrows** (bouncing chevrons, arrows that slide on hover) | Static `→` / `↗` glyphs. Motion only where it explains something. |

**Motion rule:** the page has **at most one** animated element, and it must explain the product (e.g. a
diagram of the loop). Respect `prefers-reduced-motion`: no animation at all when it's set.

## 5. Layout patterns

| # | Don't | Do instead |
|---|---|---|
| 6 | **3 feature cards in a row** (icon + title + 2 lines, ×3) | One idea per section, told with a real artifact: a screenshot, a code sample, a before/after, a table. If there are 3 steps, show them as a sequence with real output at each step. |
| 13 | **Bento grids** | A normal reading flow: headline → proof → how it works → how to start. |
| 14 | **Fake terminal window** (traffic-light dots + typed-out command) | A real, copyable code block with a language label and a Copy button. No fake macOS chrome, no typewriter effect. |
| 17 | **3 pricing tiers** by reflex (Starter / Pro / Enterprise, middle one "Most popular") | Price the way the product is actually sold. If that's free self-host + one paid plan, show two. Only show tiers you can deliver today. Never invent a "Most popular" badge. |

## 6. Trust and proof (the non-negotiables)

These are the ones that make a site look *fake*, not just generic.

| # | Don't | Do instead |
|---|---|---|
| 12 | **Fake testimonials** (stock faces, invented quotes, "Sarah, CTO") | Real quotes from named, consenting users only, or **no testimonial section**. Fabricated reviews are dishonest and can break consumer-protection law. |
| 18 | **No real product demos** | A real screenshot, recording or live example of the product doing its job, with real output. For an API: a request and its actual response. |
| — | Fake logos ("Trusted by…" with companies that don't use you) | Only logos of actual customers, with permission. Integration/provider marks only where you really integrate, labeled "Works with". |
| — | Invented metrics ("10,000+ developers", "99.9% uptime") | Only numbers you can prove. Fewer real numbers beat many fake ones. |

## 7. Things that MUST exist (their absence is the giveaway)

| # | Missing piece | Requirement |
|---|---|---|
| 21 | **No skeleton loaders** | Every async view shows a layout-matching skeleton (or a progress state with text) within 100ms. Empty states and error states are designed too, with a next action. |
| 26 | **No Terms of Service** | `/terms` page linked in the footer. Covers acceptable use, the license, liability, and termination. Have a lawyer review it before taking payments. |
| 27 | **No privacy policy** | `/privacy` page linked in the footer and next to any signup form. Says what you collect, why, where it's stored, and how to delete it. For SIA, also state that provider API keys are never stored or logged. Check India's DPDP Act and GDPR (for EU users) with a lawyer. |
| — | Basics a real product has | Favicon, page `<title>`, meta description, OG image, 404 page, a contact email, visible pricing or "contact", a changelog or last-updated date, keyboard focus styles, alt text. |

---

## 8. Review checklist (run before every ship)

**Automated (paste into a terminal at the repo root):**

```bash
# emojis (Python, not grep -P: that errors with "code point too large" without a UTF-8 locale, and matches image bytes)
python -c "import pathlib,re,sys; r=re.compile('[\U0001F300-\U0001FAFF☀-➿]'); bad=[str(p) for p in pathlib.Path('web').rglob('*') if p.suffix in ('.html','.md','.txt','.css','.js','.svg') and r.search(p.read_text(encoding='utf-8'))]; print('\n'.join(bad)); sys.exit(1 if bad else 0)" || echo "FAIL: emojis"
# em dashes in copy
grep -rn "—" web/*.html web/*.md && echo "FAIL: em dashes"
# default fonts / icon sets
grep -rniE "Inter|Geist|Space Grotesk|lucide" web/ && echo "FAIL: default font or icon set"
# glass, gradients, glow
grep -rnE "backdrop-filter|linear-gradient|radial-gradient|text-shadow" web/*.css && echo "CHECK: justify each one"
# hype words and the X-not-Y pattern
grep -rniE "seamless|supercharge|unlock|revolution|next-gen|effortless|game.?chang|it's not .*, it's" web/ && echo "FAIL: copy"
# legal pages
test -f web/privacy.html && test -f web/terms.html || echo "FAIL: missing privacy/terms"
```

**Manual:**
- [ ] I can say *why* each color, font and icon is there, for this product specifically
- [ ] The hero shows the real product or real output, not decoration
- [ ] Every number and quote on the page is real and I can source it
- [ ] Every async view has loading, empty and error states
- [ ] Footer links to Privacy, Terms and Contact, and they're real pages
- [ ] One accent color, one radius, one shadow level (or none), ≤ 4 type sizes
- [ ] At most one animation, and it explains something; off under reduced motion
- [ ] I read the copy aloud and it doesn't sound like an AI wrote it
- [ ] Squint test: blur the screenshot. Does it still look like *this* brand, or like any AI startup?

---

## 9. How to prompt an AI with this file

Paste this at the top of any UI request:

```text
Follow DESIGN_RULES.md strictly. Before writing code, list:
1) the palette tokens, fonts and radius you'll use, and why each fits this product;
2) which real product artifact (screenshot, code sample, real output) each section shows.
Do not use any pattern listed in DESIGN_RULES.md sections 1–6. Implement everything in section 7.
After writing the code, run the section 8 automated checks and report the results.
```

---

## Appendix A: Applying this to SIA's `FRONTEND_SPEC.md`

These are the parts of the existing spec that conflict with this file. **This file wins.**

| Spec item | Conflict | Change to |
|---|---|---|
| §5.2 "How it learns": 3-column cards | #6 three feature cards | A vertical sequence of 3 steps, each showing a real `/run` response excerpt (draft score → revised score → lesson saved). |
| §5.3 API code block with dark fill | #14 terminal window (borderline) | Keep it as a plain code block: no window chrome, no typing effect. Keep the Copy button. |
| §4.10 hero: ~900 animated particles | #22 orbs / #23 dot-grid feel | A labeled diagram of the 3 loops (static SVG), with one slow animation (a lesson token moving round the loop), off under reduced motion. |
| §4.5 pulsing status dot | #28 needless motion | Static dot. |
| §4.7 primary button hover darkens | OK | Keep (color change only). |
| §4.7 / §4.10 corner brackets | Not on the list, but watch it | Use them in ≤ 2 places, or they become the new template. |
| §5.5 pricing: 2 cards | OK under #17 | Keep two. Don't add a third tier until it's real. |
| §5.6 footer | Missing #26, #27 | Add `/privacy` and `/terms` pages, plus a contact email. |
| Whole page | #21 | The Agent view toggle and the copy menu need loading/error states. Add a designed 404. |
| Fonts: Fraunces + IBM Plex | OK | Keep. Don't swap to Inter "for readability". |
| Radius 0px everywhere | OK (#19) | Keep. Never mix in rounded elements. |

## Appendix B: When breaking a rule is fine

These rules target **unchosen defaults**, not the techniques themselves. You can break any rule in
sections 1–5 if you can write one sentence explaining why that choice fits *this* product better than
the alternative, and you apply it consistently. You can't break sections 6–7.
