"""Reproducible LiveChat XR approved Visor App Tile exports. No image API."""
from pathlib import Path
import argparse
import json
import shutil
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.pens.svgPathPen import SVGPathPen
import resvg_py

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--out-dir', type=Path, default=ROOT / 'generated')
OUT = parser.parse_args().out_dir.resolve()
FONT = ROOT / 'font-source/Outfit.ttf'
LICENSE = ROOT / 'font-source/OFL.txt'
if not FONT.exists():
    FONT = ROOT / 'Outfit-variable.ttf'
    LICENSE = ROOT.parent / 'font/OFL.txt'
OUT.mkdir(parents=True, exist_ok=True)
for d in ['svg', 'png', 'windows', 'web', 'social', 'font', 'source', 'qa']:
    (OUT / d).mkdir(exist_ok=True)
VIOLET, INK, LIGHT = '#6D28D9', '#18122B', '#FAF8FF'
SIZES = [16, 32, 48, 64, 128, 256, 512, 1024]
BODY = ('M30 39 C48 36 80 36 98 39 C107 40 111 45 112 54 L114 66 '
        'C115 77 108 84 98 84 L102 97 Q103 100 99 98 L81 90 L77 82 '
        'C72 79 70 72 64 72 C58 72 56 79 51 82 C44 85 37 85 30 84 '
        'C20 84 13 77 14 66 L16 54 C17 45 22 40 30 39 Z')

def circle(x, y, r):
    return f'M{x-r} {y} a{r} {r} 0 1 0 {2*r} 0 a{r} {r} 0 1 0 {-2*r} 0 Z'

HOLES = circle(43, 60, 10) + circle(85, 60, 10)
MICRO = ('M4 5 C6 4.5 10 4.5 12 5 Q14 5 14 7 L14 9 '
         'Q14 10.5 12.5 10.5 L13 12.5 L10.5 11 L9.5 10 '
         'Q8 8.5 7 10 Q5.5 11 3.5 10.5 Q2 10 2 8.5 L2 7 Q2 5 4 5 Z')

def svg(body, w=128, h=128, label='LiveChat XR'):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-label="{label}"><title>{label}</title>{body}</svg>')

def mark(color: str = 'white', tile: str | None = VIOLET):
    bg = f'<rect width="128" height="128" rx="27" fill="{tile}"/>' if tile else ''
    return bg + f'<path fill="{color}" fill-rule="evenodd" d="{BODY + HOLES}"/>'

def micro(n):
    scale = n / 16
    return svg(f'<g transform="scale({scale})"><rect width="16" height="16" rx="3.5" fill="{VIOLET}"/>'
               f'<path fill="white" fill-rule="evenodd" d="{MICRO + circle(5.5, 7.5, 1.25) + circle(10.5, 7.5, 1.25)}"/></g>', n, n)

def write(rel, text):
    (OUT / rel).write_text(text + '\n', encoding='utf-8', newline='\n')

def render(rel, text, width, height=None):
    data = resvg_py.svg_to_bytes(svg_string=text, width=width, height=height or width, skip_system_fonts=True)
    (OUT / rel).write_bytes(data)

font = instantiateVariableFont(TTFont(FONT), {'wght': 800}, inplace=False)
font.recalcTimestamp = False
font.save(OUT / 'font/Outfit-ExtraBold.ttf')
shutil.copy2(LICENSE, OUT / 'font/OFL.txt')
glyphs = font.getGlyphSet()
cmap = font.getBestCmap()
upm = font['head'].unitsPerEm

def text_paths(text, x, y, px, color):
    parts = []
    cursor = 0
    for c in text:
        g = glyphs[cmap[ord(c)]]
        pen = SVGPathPen(glyphs)
        g.draw(pen)
        parts.append(f'<path transform="translate({x+cursor*px/upm:.3f},{y}) scale({px/upm:.5f}, {-px/upm:.5f})" d="{pen.getCommands()}"/>')
        cursor += g.width
    return f'<g fill="{color}">' + ''.join(parts) + '</g>'

master = svg(mark())
write('svg/livechat-xr.svg', master)
write('svg/livechat-xr-mono.svg', svg(mark(INK, None)))
write('svg/livechat-xr-white.svg', svg(mark('white', None)))
write('svg/livechat-xr-violet.svg', svg(mark(VIOLET, None)))
for n in [16, 32]:
    write(f'svg/livechat-xr-{n}.svg', micro(n))
for n in SIZES:
    render(f'png/livechat-xr-{n}.png', micro(n) if n in (16, 32) else master, n)

for theme, color, bg in [('light', INK, LIGHT), ('dark', '#FFFFFF', INK)]:
    content = '<g transform="translate(16,16)">' + mark() + '</g>' + text_paths('LiveChat XR', 176, 105, 80, color)
    lockup = svg(content, 650, 160)
    write(f'svg/livechat-xr-lockup-{theme}.svg', lockup)
    render(f'png/livechat-xr-lockup-{theme}.png', lockup, 1300, 320)
    write(f'svg/livechat-xr-lockup-{theme}-background.svg', svg(f'<rect width="650" height="160" fill="{bg}"/>' + content, 650, 160))

# The ICO stores independently rendered 16/32 px art, not downscales of the master.
frames = [Image.open(OUT / f'png/livechat-xr-{n}.png').convert('RGBA') for n in SIZES if n <= 256]
frames[-1].save(OUT / 'windows/LiveChatXR.ico', format='ICO', sizes=[i.size for i in frames], append_images=frames[:-1])
shutil.copy2(OUT / 'windows/LiveChatXR.ico', OUT / 'web/favicon.ico')
shutil.copy2(OUT / 'svg/livechat-xr.svg', OUT / 'web/favicon.svg')
for theme in ['light', 'dark']:
    shutil.copy2(OUT / f'svg/livechat-xr-lockup-{theme}.svg', OUT / f'web/header-{theme}.svg')
render('web/apple-touch-icon.png', master, 180)
# Full-bleed avatars move the entire white symbol into the safe circle area.
avatar = svg(f'<rect width="128" height="128" fill="{VIOLET}"/><g transform="translate(12.8,12.8) scale(.8)">' + mark('white', None) + '</g>')
write('social/avatar.svg', avatar)
for platform in ['tiktok', 'reddit', 'discord']:
    render(f'social/{platform}-avatar-1024.png', avatar, 1024)
render('web/discord-avatar.png', avatar, 512)
card = svg(f'<rect width="1280" height="640" fill="{INK}"/>'
           '<g transform="translate(72,132) scale(2.5)">' + mark() + '</g>'
           + text_paths('LiveChat XR', 438, 290, 104, 'white')
           + text_paths('Your stream. In view.', 442, 360, 46, '#D8CCF1')
           + text_paths('Live-stream chat for VR', 442, 422, 32, '#D8CCF1'), 1280, 640)
write('social/github-social-preview.svg', card)
render('social/github-social-preview-1280x640.png', card, 1280, 640)
write('source/requirements.txt', 'pillow==13.0.0\nfonttools==4.67.0\nresvg-py==0.5.0')
shutil.copy2(__file__, OUT / 'source/build_brand.py')
shutil.copy2(FONT, OUT / 'source/Outfit-variable.ttf')

# A contact sheet at real native sizes plus nearest-neighbour enlargement.
sheet = Image.new('RGB', (1500, 1060), LIGHT)
d = ImageDraw.Draw(sheet)
d.text((36, 18), 'LiveChat XR / Approved Visor App Tile / Final vector redraw v1', fill=INK, font_size=26)
large = Image.open(OUT / 'png/livechat-xr-512.png')
sheet.paste(large, (35, 75), large)
d.rounded_rectangle((575, 75, 1465, 300), radius=20, fill=INK)
for name, x, y, width in [('light',575,330,880), ('dark',575,105,880)]:
    im = Image.open(OUT / f'png/livechat-xr-lockup-{name}.png')
    im.thumbnail((width, 220))
    sheet.paste(im, (x,y), im)
for idx,n in enumerate([16,32,48,64,128]):
    x=35+idx*280
    im=Image.open(OUT/f'png/livechat-xr-{n}.png')
    sheet.paste(im,(x,650),im)
    blown=im.resize((192,192),Image.Resampling.NEAREST)
    sheet.paste(blown,(x,800),blown)
    d.text((x,610),f'{n}px native / enlarged below',fill=INK,font_size=17)
sheet.save(OUT/'qa/contact-sheet.png')

# Verify file signatures, dimensions, XML and native eye holes.
checks = []
for n in SIZES:
    p=OUT/f'png/livechat-xr-{n}.png'
    im=Image.open(p).convert('RGBA')
    assert im.size==(n,n)
    assert im.getbbox()
    assert im.getpixel((n//2,n//4))[3]>0
    checks.append(f'PNG {n}: dimensions/alpha PASS')
for n in [16,32]:
    im=Image.open(OUT/f'png/livechat-xr-{n}.png').convert('RGBA')
    s=n/16
    for x in [5.5,10.5]:
        pixel=im.getpixel((int(x*s),int(7.5*s)))
        assert pixel[:3]==(109,40,217), (n,x,pixel)
    checks.append(f'{n}px two open violet lens centres PASS')
ico=Image.open(OUT/'windows/LiveChatXR.ico')
expected={(n,n) for n in SIZES if n<=256}
assert ico.ico.sizes()==expected, ico.ico.sizes()
for n in [16,32]:
    assert ico.ico.getimage((n,n)).convert('RGBA').tobytes()==Image.open(OUT/f'png/livechat-xr-{n}.png').convert('RGBA').tobytes()
checks.append(f'ICO {len(expected)} frames {sorted(expected)} + native 16/32 equality PASS')
for p in OUT.rglob('*.svg'):
    ET.parse(p)
    assert '<text' not in p.read_text(), p
checks.append('SVG XML and outlined typography PASS')
assert Image.open(OUT/'social/github-social-preview-1280x640.png').size==(1280,640)
checks.append('GitHub social preview dimensions PASS')
checks.append('No production, account avatar or repository changes performed')
write('qa/export-checks.json', json.dumps({'status':'PASS','checks':checks},indent=2))
print(json.dumps({'output':str(OUT),'checks':checks},indent=2))
