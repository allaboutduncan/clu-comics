---
name: Comic Library Utilities (CLU)
description: A self-hosted comic library manager whose chrome stays plain so the covers carry the colour.
colors:
  # Theme roles. Values are the stock-Bootstrap "default" theme; at runtime
  # every one of these resolves through the matching --bs-* variable, so
  # Bootswatch and local themes (Gruvbox, Catppuccin) replace them wholesale.
  primary: "#0d6efd"
  secondary: "#6c757d"
  success: "#198754"
  info: "#0dcaf0"
  warning: "#ffc107"
  danger: "#dc3545"
  body-bg: "#ffffff"
  body-color: "#212529"
  secondary-color: "rgba(33, 37, 41, 0.75)"
  tertiary-bg: "#f8f9fa"
  border-color: "#dee2e6"
  # Fixed colours. These sit on cover art or in the reader, never on the
  # themed page, so they are deliberately theme-independent.
  sticker-issue: "rgb(253 200 63 / 85%)"
  sticker-alert: "rgba(220, 53, 69, 0.85)"
  sticker-favourite: "rgba(255, 193, 7, 0.9)"
  sticker-scrim: "rgba(0, 0, 0, 0.6)"
  sticker-ink: "#ffffff"
  sticker-ink-dark: "#212529"
  reader-backdrop: "rgba(0, 0, 0, 0.95)"
  reader-bar: "rgba(0, 0, 0, 0.8)"
typography:
  headline:
    fontFamily: "var(--bs-body-font-family)"
    fontSize: "calc(1.325rem + 0.9vw)"
    fontWeight: 500
    lineHeight: 1.2
  title:
    fontFamily: "var(--bs-body-font-family)"
    fontSize: "1.25rem"
    fontWeight: 500
    lineHeight: 1.2
  body:
    fontFamily: "var(--bs-body-font-family)"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  card-title:
    fontFamily: "var(--bs-body-font-family)"
    fontSize: "0.9rem"
    fontWeight: 500
    lineHeight: 1.5
  card-meta:
    fontFamily: "var(--bs-body-font-family)"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.5
  sticker:
    fontFamily: "var(--bs-body-font-family)"
    fontSize: "0.7rem"
    fontWeight: 700
    lineHeight: 1.5
  mono:
    fontFamily: "var(--bs-font-monospace)"
    fontSize: "0.875em"
    fontWeight: 400
rounded:
  sticker: "4px"
  cover: "8px"
  control: "0.375rem"
  round: "50%"
spacing:
  sticker-inset: "8px"
  grid-gap: "1.5rem"
  grid-pad: "1rem"
  card-text-gap: "0.5rem"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.body-bg}"
    rounded: "{rounded.control}"
    padding: "0.375rem 0.75rem"
  button-toolbar:
    backgroundColor: "transparent"
    textColor: "{colors.secondary}"
    rounded: "{rounded.control}"
    padding: "0.25rem 0.5rem"
  cover-card:
    rounded: "{rounded.cover}"
    width: "minmax(150px, 1fr)"
  sticker-issue:
    backgroundColor: "{colors.sticker-issue}"
    textColor: "{colors.sticker-ink-dark}"
    typography: "{typography.sticker}"
    rounded: "{rounded.sticker}"
    padding: "2px 6px"
  sticker-alert:
    backgroundColor: "{colors.sticker-alert}"
    textColor: "{colors.sticker-ink}"
    typography: "{typography.sticker}"
    rounded: "{rounded.sticker}"
    padding: "2px 6px"
  cover-action:
    backgroundColor: "{colors.sticker-scrim}"
    textColor: "{colors.sticker-ink}"
    rounded: "{rounded.round}"
    size: "32px"
  cover-action-favourited:
    backgroundColor: "{colors.sticker-favourite}"
    textColor: "{colors.body-color}"
    rounded: "{rounded.round}"
    size: "32px"
  navbar:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.body-bg}"
---

# Design System: Comic Library Utilities (CLU)

## Overview

**Creative North Star: "The Back Room"**

CLU is the comic shop's back room, not its shop floor. Long boxes, a sorting table, everything labelled and filed. The colour in the room comes from the covers. Everything around them (navigation, toolbars, tables, forms) is plain, orderly and predictable, because the person in this room is working through a library of thousands of issues, not browsing a storefront. The feel is **orderly**: everything has a place, a label and a consistent position from page to page.

The system is built on Bootstrap 5.3 and has no palette of its own. Each user picks one of 26 Bootswatch themes or a local theme (Gruvbox Dark; Catppuccin Latte, Frappé, Macchiato, Mocha), and every colour on the page resolves through Bootstrap's `--bs-*` variables. CLU's own identity sits in the parts a theme does not reach: the 2:3 cover grid, the stickers on the covers, the hover lift, and the full-black reader. Those parts are the same on every theme.

Two looks are rejected outright. CLU is not a **generic SaaS dashboard** (gradient hero cards, KPI tiles, glassy panels), and it is not a **streaming app** (cinematic dark carousels and autoplay drama spread across the whole UI). The reader may go dark and immersive; the rest of the app does not.

**Key Characteristics:**
- Theme-agnostic chrome: every page colour comes from the active theme's Bootstrap variables.
- Covers are the only saturated content; the 2:3 cover frame is the system's signature shape.
- Small fixed-colour stickers on covers, legible over any artwork.
- Flat at rest; cards lift on hover; only overlays cast real shadows.
- Bootstrap components used as shipped, with outline buttons as the working default.

## Colors

The palette is borrowed: Bootstrap's semantic roles, filled in by whichever theme the user picked, plus a small set of fixed colours that only ever appear on top of cover art or inside the reader.

### Primary
- **Theme Primary** (`--bs-primary`): the header navbar fill, primary actions (one per view), active nav and pagination state, selection tint (`rgba(var(--bs-primary-rgb), 0.15)`). It is the theme's accent and the only accent the chrome uses.

### Secondary
- **Theme Secondary** (`--bs-secondary`): the workhorse outline buttons in toolbars, filter bars and pagers. Most buttons in CLU are `btn-outline-secondary`.

### Tertiary
- **Status roles** (`--bs-success`, `--bs-info`, `--bs-warning`, `--bs-danger`): status only. Success for completed jobs, info for drop targets and notices, warning for in-progress operations and update badges, danger for errors, failed downloads and destructive confirmations. Never decorative.

### Neutral
- **Page** (`--bs-body-bg`) and **Ink** (`--bs-body-color`): the page and its text.
- **Muted Ink** (`--bs-secondary-color`): metadata lines, captions, help text, empty states.
- **Raised Surface** (`--bs-tertiary-bg`): card hover fill, panel backgrounds, table stripes.
- **Rule** (`--bs-border-color`): dividers, list separators, input strokes.

### Fixed colours (cover and reader)
- **Price-Sticker Gold** (sticker-issue): the issue-number sticker in the top-right corner of a cover, in dark ink (white on this gold is about 1.6:1), with a thin black outline so it separates from yellow artwork.
- **Missing-Tag Red** (sticker-alert): the bottom-left sticker flagging a comic with no ComicInfo.xml.
- **Favourite Amber** (sticker-favourite): a favourited cover's round action button, with dark ink.
- **Cover Scrim** (sticker-scrim): the round translucent-black backing for cover actions (favourite, to-read, mark unread) before they are set.
- **Reader Black** (reader-backdrop, reader-bar): the reader's near-opaque backdrop and its header/footer bars. The reader is black on every theme so the page reads like paper under a lamp.

### Named Rules
**The Borrowed Palette Rule.** Page chrome never names a colour. It reads `var(--bs-*)` (or `rgba(var(--bs-*-rgb), α)`) so all 32 themes (stock Bootstrap, 26 Bootswatch, 5 local), light and dark, stay correct. A hex value in page CSS is a bug.

**The Sticker Exception.** Fixed colours are allowed only where the background is cover art or the reader's black, never the themed page. If it can sit on `--bs-body-bg`, it uses a theme variable.

**The Fill-and-Text Rule.** Themes drive `.bg-x` and `.text-x` from one variable, and templates put white text on fills and coloured text on the page. Never key a rule on a bare `[data-bs-theme=dark]`: the navbar carries that attribute on every theme, Latte included. A page rule that truly must differ on dark themes uses `:root[data-bs-theme="dark"]`; local theme files never branch on it at all.

**The Selection Rule.** Selected and drop-target covers use `--clu-select` (the theme primary pushed 30% toward `--bs-emphasis-color`, defined in `collection.css`), so a muted primary still marks a selection clearly and carries a white check.

### Known drift (to fix)
`collection.css` is clean. These still ignore the active theme and should move to variables when their file is next touched: the multi-select bulk action bar's navy gradient (`#1a1a2e → #16213e`, duplicated in `collection.css` and `series.html`; `var(--bs-dark)` is the theme-aware equivalent); `.ops-item-error` border `#dc3545` in `base.html` (should be `--bs-danger`); Bootstrap 4's `#28a745` green; and reader progress glows in `rgba(13, 110, 253, …)` instead of `--bs-primary-rgb`. `static/css/zephyr.css` is a vendored Bootswatch file that nothing links; it is not part of the system.

## Typography

**Display Font:** none; the system has no display face.
**Body Font:** the theme's `--bs-body-font-family` (stock Bootstrap: `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", "Noto Sans", "Liberation Sans", Arial, sans-serif`). Bootswatch themes may swap it.
**Label/Mono Font:** `--bs-font-monospace`, for paths, filenames, logs and XML.

**Character:** native, unbranded type. CLU looks like the user's own system, so the covers do the talking and a theme change can swap the face without breaking anything.

### Hierarchy
- **Headline** (500, h2 scale `calc(1.325rem + 0.9vw)`, 1.2): page titles. Most pages open with one heading, an icon and a muted subtitle.
- **Title** (500, 1.25rem, 1.2): card headers, modal titles, dashboard section headings.
- **Body** (400, 1rem, 1.5): forms, tables, settings copy.
- **Card Title** (500, 0.9rem): the comic or folder name under a cover, single line with an ellipsis and a tooltip carrying the full name.
- **Card Meta** (400, 0.75rem): the muted line under a card title (year, issue count, publisher).
- **Sticker** (700, 0.7rem): text on cover stickers and status pills.
- **Mono** (0.875em): file paths, log tails, ComicInfo.xml.

### Named Rules
**The No-Face Rule.** Don't load a web font or set a literal `font-family`. Read `--bs-body-font-family` or `--bs-font-monospace`, as the charts on Insights already do. Nothing in the app CSS sets a literal family today; keep it that way.

**The Filename Rule.** Anything the user might copy or compare (a path, a filename, a pattern) is set in mono.

## Layout

Pages sit in Bootstrap's `.container` / `.container-lg` under a full-width navbar, with a sticky footer (`body.d-flex.flex-column.min-vh-100`). The standard page is: heading row (title left, actions right), a toolbar of filters and search, then content.

The cover grid is the core layout: CSS grid, `repeat(auto-fill, minmax(150px, 1fr))`, gaps of 1.5rem and 1rem vertical padding. A "large cards" mode uses `minmax(173px, 190px)` centred, which grows cards by about 15% without dropping a column. Phones keep two columns. Dashboard rows are horizontal Swiper carousels with 165px slides. Filter bars scroll sideways rather than wrapping.

Breakpoints are Bootstrap's (576 / 768 / 992 / 1200 / 1400px). The navbar collapses below `lg`. Extra rules sit at 768px and 480px for the grid toolbar, and `pointer: coarse` raises pager targets to 2.75rem. Desktop, tablet and phone are all supported surfaces; tables that won't fit go in `.table-responsive`, never off-screen.

## Elevation & Depth

Flat by default. Surfaces lie on the page with Bootstrap borders and the theme's tonal steps (`--bs-tertiary-bg`). Depth appears in two places: as a response to hover on things you can open, and under real overlays. The reader goes further, with a black backdrop that takes over the screen.

### Shadow Vocabulary
- **Cover rest** (`box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1)`): the barely-there edge that separates a cover from the page.
- **Card hover** (`box-shadow: 0 0 11px rgba(33, 33, 33, 0.2)` plus `--bs-tertiary-bg` fill): any `.card` the pointer is over.
- **Overlay** (Bootstrap `.shadow`, or `0 8px 32px rgba(0, 0, 0, 0.5)` in the reader): dropdowns, modals, reader panels.

### Named Rules
**The Lift Rule.** Cover cards rise 5px (`transform: translateY(-5px)`, 0.2s) on hover and come forward (`z-index: 10`). Lift means "this opens". Don't lift anything that doesn't.

## Shapes

Gently rounded, never pill-shaped except for counts. Covers have 8px corners, `aspect-ratio: 2/3` and `overflow: clip`, so a cover of any size fills its frame at the same proportion; folder art is generated at 200×300 for the same reason. Stickers have 4px corners. Cover actions are 32px circles. Controls use the theme's `--bs-border-radius`. Count badges on nav items are `rounded-pill`.

**The 2:3 Rule.** A cover frame is always 2:3. Never let source pixels set the card size.

## Components

Bootstrap components as shipped, styled only through theme variables. CLU's own components are the ones that live on a cover.

### Buttons
- **Shape:** the theme's control radius (stock: 0.375rem).
- **Primary:** `btn-primary`, one per view, for the action the page exists for (Save, Run, Apply).
- **Working default:** `btn-outline-secondary btn-sm` for toolbars, filters, pagers and row actions. It is used about twice as often as primary.
- **Destructive:** `btn-danger`, only inside the shared delete-confirmation modal (`CLU.showDeleteConfirmation`).
- **Icons:** Bootstrap Icons (`bi-*`) with `me-2` before a label. Icon-only buttons carry a `title` and an `aria-label`.

### Cover Stickers (signature component)
Small fixed-colour labels pinned 8px in from a cover's corners, like price stickers on a bagged comic.
- **Issue sticker** (top right): Price-Sticker Gold, dark bold 0.7rem, thin black outline; adds a read icon when the issue is read, and shows only the icon for unnumbered issues.
- **Missing-tag sticker** (bottom left): Missing-Tag Red, for comics without ComicInfo.xml.
- **Cover actions** (favourite, to-read, info, mark unread): 32px Cover Scrim circles, hidden until the cover is hovered or the button has keyboard focus; a set state (favourited, marked) stays visible in its fill colour.

### Cards / Containers
- **Cover card:** 2:3 frame, cover-rest shadow, a 0.5rem gap, then card title and card meta. No border, no background.
- **Panel card:** Bootstrap `.card` with a `card-header` title; flat, theme background, hover per Elevation.
- **Internal padding:** Bootstrap's card spacing (1rem).

### Inputs / Fields
- **Style:** Bootstrap `.form-control` / `.form-select` with theme stroke and radius; `form-control-sm` in toolbars.
- **Focus:** the theme's focus ring.
- **Inline rename:** a borderless input with a single `--bs-border-color` bottom rule.

### Navigation
- **Navbar:** full-width, `bg-primary` with `data-bs-theme="dark"` on every theme, logo plus "CLU" wordmark at left, items right-aligned, collapsing below `lg`.
- **Grouping:** dropdowns gather related pages (Home: Library, Source Wall; Pull List: Releases, Wanted, Publishers, Status). Items are role-gated, so Readers see fewer.
- **State:** `.active` on the current item and its parent dropdown. Counts sit as pills on the item's top-right corner: warning for watched files, danger for failed downloads.
- **Active Operations:** a spinning warning-coloured icon with a count, opening a dropdown of progress rows (6px bars, ellipsised label and detail).

### Feedback
- **Toasts** (`CLU.showToast`, `CLU.showSuccess`, `CLU.showError`) for results and status; any page that uses them includes `partials/toast_container.html`.
- **Modals** for every confirmation or input. Never `alert()`, `confirm()` or `prompt()`.
- **Loading:** a pulsing muted italic metadata line on cover cards; `loading.svg` / `error.svg` placeholders in cover frames.

### Reader
Full-screen over a Reader Black backdrop at `100dvh`, with translucent black header and footer bars carrying a white title and page progress. The only surface where the theme steps aside entirely.

## Do's and Don'ts

### Do:
- **Do** read every page colour from `var(--bs-*)`, and check a new screen on a light theme, a dark Bootswatch theme and Catppuccin Latte before calling it done.
- **Do** keep cover frames at 2:3 with 8px corners, and put cover status in stickers 8px from the corners.
- **Do** use `btn-outline-secondary btn-sm` for toolbar actions and save `btn-primary` for the one action the view exists for.
- **Do** use Bootstrap Icons, Bootstrap components and the shared `CLU` toast and modal helpers before writing a new component.
- **Do** respect `prefers-reduced-motion` for any animation beyond a hover transition, as the floating pager already does.
- **Do** load static assets through `url_for`, never a hardcoded `/static/`.

### Don't:
- **Don't** write hex or rgb colours in page CSS; the only exceptions are stickers on covers and the reader.
- **Don't** key styles on a bare `[data-bs-theme=dark]`; use `:root[data-bs-theme="dark"]` when a page rule must differ.
- **Don't** build generic SaaS dashboard pieces: gradient hero cards, KPI tiles, glassy panels.
- **Don't** spread streaming-app styling (cinematic dark carousels, autoplay previews) beyond the reader.
- **Don't** load web fonts or set a literal `font-family`.
- **Don't** lift or shadow things that don't open; flat is the resting state.
- **Don't** use native `alert()`, `confirm()` or `prompt()`.
