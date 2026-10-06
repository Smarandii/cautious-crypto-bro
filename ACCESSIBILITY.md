# Accessibility

## Scope

Two surfaces:

- the Telegram approval bot messages and inline buttons
- the project landing page in [`docs/`](docs/), served by GitHub Pages

Container logs are operator-facing but are plain line-oriented text rather than a
designed interface, so they are noted rather than specified.

## Commitment

The landing page targets WCAG 2.2 level AA. There has been no formal audit and no
screen-reader testing, so treat this as a design intent backed by the checks
below, not as a conformance claim.

## Landing page

Verified in this repository, in both light and dark colour schemes:

- Semantic structure: one `h1`, `header`/`main`/`footer` landmarks, each `section`
  labelled by its heading, `lang="en"` set.
- Every `aria-labelledby` reference resolves to an existing `id`.
- The pipeline diagram is `role="img"` with both `<title>` and `<desc>`, so it
  has a text alternative rather than being an opaque graphic.
- The diagram sits in a horizontally scrollable region that is focusable
  (`tabindex="0"`, `role="region"`, `aria-label`) so it can be scrolled by
  keyboard, and it keeps its natural width below 640&nbsp;px instead of scaling
  its labels down to illegibility.
- Contrast meets AA for every text pair (minimum 6.57:1) and every non-text pair
  (minimum 3.37:1). Borders are not the sole means of conveying any grouping;
  headings and body text carry the structure.
- Light and dark themes are both first-class via `prefers-color-scheme`, not
  user-toggle-dependent.
- `prefers-reduced-motion` is honoured, and the page has no animation regardless.
- A skip link jumps to `main`, and `:focus-visible` outlines every interactive
  element.
- No meaning is carried by colour or by a bare glyph. Card icons are inline SVG
  marked `aria-hidden`, with the heading text carrying the meaning.
- Verified reflow without horizontal scrolling at 1280&nbsp;px, 360&nbsp;px and
  320&nbsp;px viewports.
- The only external request on the page is the CI status badge served from
  `github.com`. No web fonts, no CDN, no analytics.

## Operator surfaces

- Approval cards are Telegram HTML with bold labels and a `<code>` payload block.
  State is never carried by colour or by a bare symbol: warnings pair the glyph
  with explicit text, such as `ACCOUNT-WIDE POSITION ACTION`.
- No time-limited interaction. A stale request is rejected with an explicit
  reason — `Position action is stale (Ns)` — rather than silently disappearing or
  acting on stale input.
- The approval bot delegates accessibility of its cards and inline buttons to the
  Telegram client in use.

## Known limitations

- **No screen-reader testing.** The landing page and the approval flow have not
  been exercised with NVDA, JAWS or VoiceOver. The structure supports it; that is
  not the same as having verified it.
- **No formal audit.** The contrast figures above were computed from the stylesheet
  and the markup was checked by script, not by an automated or human audit tool.
  Reflow was verified at three widths, not exhaustively.
- Telegram message and inline-button accessibility is whatever the user's
  Telegram client provides. This project cannot test or guarantee it.
- Log output is line-oriented plain text. It is greppable and screen-reader
  readable, but dense multi-field lines are not structured for non-visual
  navigation.
- The pipeline diagram degrades to its `<desc>` text alternative where SVG is
  unsupported or blocked.
- [STRATEGY.md](STRATEGY.md) contains wide tables that reflow poorly at narrow
  widths, and its information is prose-heavy by design.
- There are no localized docs. Everything is English-only, including in-app
  messages and the landing page.
- Commands in docs assume a POSIX shell and Docker Compose, which excludes some
  environments entirely.

## Reporting a barrier

Open an issue describing the barrier and which surface it affects — the feature
request form is the closest fit. Include the assistive technology and browser or
client you used, if relevant.

Barriers in the Telegram client itself belong upstream with Telegram; this
repository cannot fix them, but a report noting the dependency is still useful.

## Accepted-exception policy

No exceptions are currently claimed. If one becomes necessary — for example,
retaining an operator-facing format that cannot be made screen-reader friendly
because the exchange API constrains it — it will be documented here with the
constraint that forced it.
