# LiveChat XR — Visor App Tile v1

Status: approved direction, source integration ready for canonical review. Not installed, merged, deployed or published. Do not treat asset delivery as launch acceptance.

## Repository integration

All asset paths below resolve under [assets/brand](../assets/brand).
The original [integration contract](brand-integration.md) is retained for downstream acceptance lanes.
The [master](../assets/brand/svg/livechat-xr.svg), dedicated
[16 px](../assets/brand/png/livechat-xr-16.png) and
[32 px](../assets/brand/png/livechat-xr-32.png) art require canonical visual review.
The [ICO](../assets/brand/windows/LiveChatXR.ico) supplies EXE, installer and Tk icons;
the tray uses the native 32 px PNG. Missing runtime resources fail explicitly and
the source/frozen `--check` entrypoint verifies them without starting chat.
The [manifest](../assets/brand/manifest.json) records imported file hashes from the
approved export pack; unimported review mockups/QA remain in the marketing evidence tree.

The relay image contains the web exports. Public exact-match GET routes are
`/favicon.ico`, `/favicon.svg`, `/apple-touch-icon.png`, `/brand/header-dark.svg`
and `/brand/discord-avatar.png`; unknown paths/traversal return 404.
The header is the supplied outlined Outfit lockup, not a site-wide font/palette change;
functional styles continue to follow [DESIGN.md](../DESIGN.md).

Discord webhook posts retain the existing `LiveChat XR` username, disabled mentions
and the webhook owner's avatar. No automatic `avatar_url` override is added: the
current sender has no opt-in identity setting or reliable custom-avatar knowledge,
and a new public URL cannot be claimed live before reviewed deployment. The hosted
avatar export is available for a future explicit opt-in; no bot identity is changed.
The regression uses mocked transport only. No real webhook, social upload or promo.

This PR is source/packaging integration only. Dora must verify reviewed deployed
bytes/MIME/SHA via the existing pull controller. Native Windows QA must inspect the
actual installer, EXE, shortcut, tray, Settings and uninstall surfaces in an isolated
environment. Neither source CI nor a browser screenshot substitutes for those gates.

## Source and construction

Owner direction: `shotgun-r2/approved.json`, 2026-10-09T10:49:37Z, points to `variant-A3.png`. The metadata's approved_variant is literally `C` even though the filename is `variant-A3.png`; preserve both. Supersedes cyan and the first four marketing concepts.

White visor-shaped speech bubble with two true round lens cut-outs and a bottom-right tail, inside a solid violet rounded-square tile. Geometry is a new clean vector interpretation of the approved raster, not an exact automated trace. No third-party logo assets are included. Master grid is 128 units, tile corner radius 27. Small-size grid is 16 units with dedicated lens and tail geometry; use the provided 16/32 px art rather than blindly shrinking the master.

## Colours

- Primary violet: #6D28D9. White symbol only on this tile; no gradient.
- Ink: #18122B. Dark surface and wordmark on light.
- Light: #FAF8FF. Light surface.
- White: #FFFFFF. Reversed type and symbol.
- Muted light: #D8CCF1. Supporting text on Ink only.

Calculated WCAG contrast: white/violet 7.10:1; ink/light 17.18:1; white/ink 18.10:1; muted light/ink 11.92:1. These are colour-pair calculations, not whole-product accessibility certification. Do not use violet body text on Ink.

## Typography and name

Display name: LiveChat XR (space before XR). Font: Outfit ExtraBold, weight 800, geometric sans. Wordmark SVGs contain outlined glyph paths and need no installed font. Font source and SIL OFL 1.1 licence are included. Source: https://github.com/google/fonts/tree/main/ofl/outfit . Use system-ui for functional UI unless the product intentionally adopts Outfit; this handoff does not require a site-wide type change.

## Minimum size and clear space

Tile minimum: 16 px using pixel-grid SVG/PNG. Use 32+ for normal UI and 64+ for promotional contexts. Wordmark lockup minimum: 195 CSS px wide; use tile alone below that. Keep clear space at least 1/8 of the tile width around external compositions; internal tile whitespace is built in. Do not change icon-to-type gap or font proportions in supplied lockups.

Avatar PNGs are full-bleed violet with an inset mark; they are designed for circular crops. Do not crop the rounded-square app icon into a circle: use the social avatar export. The 1024 avatar and 512 Discord web avatar share identical geometry.

## Variants

- svg/livechat-xr.svg: primary rounded-square tile.
- svg/livechat-xr-{16,32}.svg: small-size optically corrected tile.
- svg/livechat-xr-mono.svg: Ink symbol on transparent.
- svg/livechat-xr-white.svg: white symbol on transparent.
- svg/livechat-xr-violet.svg: violet symbol on transparent.
- svg/livechat-xr-lockup-{light,dark}.svg: transparent, outlined type.
- png/livechat-xr-{16,32,48,64,128,256,512,1024}.png.
- windows/LiveChatXR.ico: six independently rendered sizes, 16 through 256.
- web/: favicon ICO/SVG, headers, apple touch icon and Discord avatar.
- social/: TikTok/Reddit/Discord avatars and 1280x640 GitHub preview.

Transparent lens cut-outs remain transparent in one-colour variants. Do not fill the holes with a surface-specific painted colour.

## Don't

Do not stretch, rotate, mirror the tail, close the lens holes, add extra eyes/dots, add shadows or gradients, redraw the mark with a platform logo, add Quest/Meta/TikTok/Discord endorsement badges, or use an old cyan/lime concept. Do not recolour the tile to Discord blurple. Don't infer live product capabilities from the illustrative review page.

## Similarity screen — not legal clearance

Reference pages viewed 2026-10-20:
- https://www.meta.com/brand/resources/meta/company-brand/
- https://www.meta.com/brand/resources/meta/our-trademarks/
- https://commons.wikimedia.org/wiki/File:Logo_Oculus_horizontal.svg

Meta uses a continuous crossing infinity ribbon. Oculus uses one elongated opening in a rounded loop. This redraw instead has two round openings, a solid visor, centre notch and asymmetric chat tail; neither third-party construction was copied.

Visual QA flagged a possible Discord association: violet field plus a white two-eyed silhouette. Do not erase that flag or call this a legal pass. The approved direction itself uses those ingredients. Canonical review must inspect the actual master, tail and tiny icons before shipping; a material concept change returns to the owner rather than being silently substituted. This is a bounded visual screen, not a comprehensive trademark search or legal advice.

## Rebuild

Install source/requirements.txt in a local venv, then run:

    python source/build_brand.py --out-dir <new-empty-output-directory>

The bundled variable font and OFL are local inputs; no paid API or network is required. resvg renders the SVG paths; Pillow encodes the ICO with native small-size frames. Review documents and integration contract are hand-authored and do not get regenerated by the export script.
