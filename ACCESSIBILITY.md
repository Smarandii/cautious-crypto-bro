# Accessibility

## Scope

This project is a Dockerized Python service. It has no web interface, no
stylesheet, and no custom rendering of its own; it emits Telegram HTML, which
Telegram renders in the user's client. The accessible surfaces are:

- the Telegram approval bot messages and inline buttons
- the Markdown documentation in this repository
- the container logs

## Commitment

Documentation targets WCAG 2.2 level AA where the medium allows. There is no
formal conformance claim and no audit has been performed, so treat the statement
below as intent rather than certification.

## What is in place

- Documentation is plain Markdown with descriptive link text, no meaning carried
  by images, and heading structure that reads linearly.
- Approval cards are Telegram HTML with bold labels and a `<code>` payload block.
  State is never carried by colour or by a bare symbol: warnings pair the glyph
  with explicit text, such as `ACCOUNT-WIDE POSITION ACTION`.
- No time-limited interaction. A stale request is rejected with an explicit
  reason — `Position action is stale (Ns)` — rather than silently disappearing or
  acting on stale input.
- The approval bot delegates accessibility of its cards and inline buttons to the
  Telegram client in use.

## Known limitations

- Telegram message and inline-button accessibility is whatever the user's
  Telegram client provides. This project cannot test or guarantee it, and the
  approval flow has not been validated with a screen reader.
- Log output is line-oriented plain text. It is greppable and screen-reader
  readable, but dense multi-field lines are not structured for non-visual
  navigation.
- [STRATEGY.md](STRATEGY.md) contains wide tables that reflow poorly at narrow
  widths, and its information is prose-heavy by design.
- There are no localized docs. Everything is English-only, including in-app
  messages.
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
