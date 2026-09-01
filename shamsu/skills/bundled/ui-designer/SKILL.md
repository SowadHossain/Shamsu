---
name: ui-designer
description: Build a front end that works and looks deliberate - any framework. Structure, then states, then check it in a real browser.
---
# Front-End Design

Loading a page is not using one. Finish with
`check_page(url, click="#startBtn", wait_seconds=3)` - it reports console
errors, what rendered, and how much of the canvas is drawn on and changing. A
screen that should be busy and comes back 1% covered is not working, whatever
loaded.

**Icons, never emoji.** Emoji differ per platform, cannot be recoloured or
sized, and are read out by their unicode name. Use one free set - Lucide,
Heroicons, Phosphor, Bootstrap Icons - or inline SVG with
`stroke="currentColor"`. Icon-only controls need `aria-label`.

**Splitting up.** Split by what a file owns (`player`, `hud`), never by type.
Two silent failures: a file nobody loads (plain HTML needs a `<script src>`, a
bundler an `import`), and a `class` or `const` declared twice, which stops the
second plain script running. Move it, never copy it.

**Frameworks.** React/Vue/Svelte: state above the components that read it; list
keys are stable ids, not indexes. Tailwind: that scale is its defaults. Angular:
logic in the class, not the template. Plain HTML/CSS/JS is often right.

**Order.** Semantic markup with real content, unstyled. Then layout. Then every
empty, loading, disabled and error state. Then check it. Spacing `4 8 12 16 24
32 48`, type `12 14 16 20 24 32`; one accent, one surface, one text colour.

**Not taste.** Readable at 360px and 1920px, keyboard reachable with a visible
`:focus`, contrast 4.5:1. A `<canvas>` with no `width`/`height` has a 0x0
surface and draws nothing.
