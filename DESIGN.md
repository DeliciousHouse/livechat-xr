---
# gstack: design-md-format=spec
name: LiveChat XR
description: Night-mode creator tool; near-black surfaces, one violet action, gold as the warm accent, calm and quick.
colors:
  background: "#0f0d16"
  surface: "#17141f"
  surface-chip: "#1b1826"
  line: "#2a2536"
  line-field: "#332d42"
  text: "#eceaf4"
  text-secondary: "#c9c5d8"
  text-muted: "#a9a5bc"
  text-label: "#9a96ad"
  text-faint: "#6f6a84"
  placeholder: "#5f5a72"
  primary: "#7c4dff"
  on-primary: "#ffffff"
  focus: "#8b5cf6"
  link: "#b39dff"
  accent: "#ffc440"
  accent-soft: "#ffd77a"
  on-accent: "#1a1300"
  error: "#5a1f2a"
  os-notification: "#2b2d33"
  os-notification-text: "#f2f2f2"
  discord: "#5865f2"
typography:
  display:
    fontFamily: Bricolage Grotesque
    fontWeight: 800
    fontSize: 52px
    lineHeight: 52px
    letterSpacing: 0
  heading:
    fontFamily: Bricolage Grotesque
    fontWeight: 800
    fontSize: 19px
    lineHeight: 24px
  body:
    fontFamily: Bricolage Grotesque
    fontSize: 16px
    lineHeight: 1.55
  label:
    fontFamily: Bricolage Grotesque
    fontWeight: 600
    fontSize: 13px
  os:
    fontFamily: system-ui
    fontSize: 14px
    lineHeight: 1.4
rounded:
  sm: 9px
  md: 12px
  lg: 14px
  xl: 16px
  card: 22px
  full: 9999px
spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 22px
  2xl: 30px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.lg}"
  input:
    backgroundColor: "{colors.background}"
    borderColor: "{colors.line-field}"
    rounded: "{rounded.md}"
  card:
    backgroundColor: "{colors.surface}"
    borderColor: "{colors.line}"
    rounded: "{rounded.card}"
  step-number:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.md}"
  chip:
    backgroundColor: "{colors.surface-chip}"
    textColor: "{colors.accent-soft}"
    rounded: "{rounded.full}"
  nav-link:
    textColor: "{colors.text-label}"
---

# LiveChat XR

## Overview

**Creative North Star:** a calm night-mode tool for streamers who are about to go live: dark like the room they stream from, one obvious action, nothing shouting.
**Product context:** LiveChat XR puts TikTok LIVE / Twitch chat in front of a VR streamer. Two paths: a Windows tray app + OpenXR overlay (PC VR), and a hosted Discord relay (`server/server.py`, livechat.deliciouswines.org) for standalone Quest.
**Mode per surface:** relay home = Persuade (one sign-up form); manage page = Operate (status, test, stop); privacy = Read; in-headset banner (PC overlay) = Experience.
**Key characteristics:**
- Near-black page, one violet button per screen.
- Gold only as warmth: headline accent, step numbers, price chip.
- A plain grey stock Quest notification as the product shot, never a stylised one.
- Short, scannable copy; setup is three numbered steps.

## Colors

**Strategy:** Restrained. Neutrals carry the page; `primary` violet marks the single action, `accent` gold marks emphasis and order (step numbers), never interaction.
**Light or dark:** dark only. Streamers set this up at night next to a headset, and the brand already lives in dark violet. Light-mode users get the same palette on purpose.
Neutrals are violet-tinted greys stepping up from `background` to `surface` to `line`; hierarchy comes from these steps plus borders, not from lightness inversion. `focus` is the violet ring on every focusable element. `os-notification*` and `discord` exist only to depict real OS/Discord chrome and must not be used as brand colors.

## Typography

Bricolage Grotesque (Google Fonts, weights 400/600/800) for everything we own: 800 for display and step headings, 600 for labels and chips, 400 body. Display has no negative letter-spacing: Pretext measures the `font` shorthand only, so tracking would make computed heights wrong. The stock-notification mock uses `system-ui` so it reads as OS chrome, not as our UI. Display steps down 56 / 52 / 44 / 36px across the breakpoints.

## Layout

One centered column (600px, 640px at >=1440px) with a 1080px header bar. Breakpoints at 375, 600, 768, 1024, 1440. The display headline may break out of the column by 40px each side at >=769px to hold two lines. Rhythm: tight inside components (4/8/12), loose between sections (22/26/30).

## Elevation & Depth

Depth is borders and surface steps. Two shadows only: the primary button's soft violet drop (`0 10px 30px -10px` at ~67% violet) and the OS notification mock's neutral drop (`0 8px 24px` black 40%). No glows.

## Shapes

Radius scale sm 9 / md 12 / lg 14 / xl 16 / card 22 / full. Inner elements use a smaller step than their container (segmented-control thumb `sm` inside an `md` track; inputs `md` inside a `card`). Chips and pills are `full`.

## Components

- **Primary button:** violet, white 800 text, `lg` radius, full width in forms. Hover brightens about 8%, active nudges down 1px, focus-visible shows the violet ring. One per screen.
- **Secondary button:** `line`-colored fill, no shadow (e.g. "Connect with webhook").
- **Segmented control:** real radio inputs; checked label inverts to `text` on `surface`; keyboard focus shows the ring on the label.
- **Input:** `background` fill, `line-field` border, ring plus `focus` border on focus, `placeholder` color for hints.
- **Stepper card:** numbered gold squares joined by a 2px `line` rail; a step the user does later (on the Quest) gets a muted number.
- **Quest notification mock:** stock grey card, the real Discord app icon (official white Clyde mark, inline SVG, on a `discord` blurple tile), "Discord / now", channel line, then the batched chat as plain text exactly as `chat.batch()` produces it (up to 3 lines plus "+N more"; gift lines are plain text starting with 🎁).

## Do's and Don'ts

- Do show the Quest result as a standard Meta Quest system notification from the Discord app.
- Do keep one violet primary action per screen.
- Do use real radio/inputs that post to the server without JavaScript.
- Do keep focus-visible rings on every interactive element.
- Don't imply gold, custom bubbles or any special styling inside the headset for the Discord/Quest path; gold gift lines exist only in the PC overlay.
- Don't use gradient text, purple-blue gradients, glows or decorative blobs.
- Don't add negative letter-spacing to Pretext-measured text.
- Don't use `discord` / `os-notification` colors as brand colors.

## Motion

- **Approach:** minimal-functional.
- **Easing:** enter(ease-out) exit(ease-in) move(ease-in-out)
- **Duration:** micro 120ms on button hover/press; nothing else animates.
- **The one authored moment:** none; respect `prefers-reduced-motion` by disabling transitions.

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-10-09 | Initial design system created | Extracted by /design-html from the approved relay sign-up design (variant F of /design-shotgun: B's stepper, dark, C's gold accents) |
| 2026-10-09 | Quest path shown as stock notifications | Brendan: "The headset shows standard meta notifications just like any other app" |
| 2026-10-09 | No display letter-spacing | Pretext height computation cannot see letter-spacing; measured and rendered heights now match at 1440/1024/768/500px |
