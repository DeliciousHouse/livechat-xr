"""Builds the two website teasers: promo/out/livechat-xr-pcvr.mp4 and promo/out/livechat-xr-standalone.mp4.

Gameplay is real Population: ONE footage; chat comments are staged. PC VR banners are rasterized by the shipped
layer DLL (livechatxr_render_test), so they are the overlay's real pixels. Run with the repo .venv (Pillow, 64-bit).
"""
import ctypes as C
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
WORK, OUT = ROOT / "_work", ROOT / "out"
REC = Path(r"D:\Quest 3 Recordings\pop1-auto-editor\processed_videos")
SRC_A = REC / "Record_2026-10-07-14-44-18.mkv_edited.mp4"
SRC_B = REC / "Record_2026-10-06-18-52-22.mkv_edited.mp4"
W, H, FPS = 1920, 1080, 30
GOLD, BLURPLE = (255, 196, 64), (88, 101, 242)
FONTS = Path(r"C:\Windows\Fonts")


def font(size, weight="sb"):
    return ImageFont.truetype(str(FONTS / {"sb": "seguisb.ttf", "b": "segoeuib.ttf", "r": "segoeui.ttf"}[weight]), size)


def save(img, name):
    p = WORK / name
    img.save(p)
    return p


def dll_banner(text, scale=0.92):
    """The real overlay raster (1024x320 RGBA), cropped to its box and scaled to on-screen size."""
    dll = C.CDLL(str(ROOT.parent / "layer" / "livechat_xr_layer.dll"))
    buf = (C.c_uint32 * (1024 * 320))()
    dll.livechatxr_render_test(text.encode(), buf)
    img = Image.frombuffer("RGBA", (1024, 320), bytes(buf), "raw", "RGBA", 0, 1)
    img = img.crop(img.getbbox())
    return img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)


def caption(text, accent):
    """Lower-left pill: accent bar + white text."""
    f = font(46)
    tw = int(f.getlength(text))
    img = Image.new("RGBA", (tw + 90, 92), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, img.width - 1, 91), 22, fill=(14, 14, 18, 215))
    d.rounded_rectangle((18, 20, 26, 72), 4, fill=accent)
    d.text((48, 44), text, font=f, fill="white", anchor="lm")
    return img


def title(kicker, headline, accent):
    """Full-frame hook: dark scrim, small accent kicker, big headline."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    scrim = Image.new("L", (W, H), 0)
    ImageDraw.Draw(scrim).ellipse((-300, 180, W + 300, H - 180), fill=190)
    img.putalpha(scrim.filter(ImageFilter.GaussianBlur(120)))
    d = ImageDraw.Draw(img)
    d.text((W // 2, 430), kicker.upper(), font=font(40, "b"), fill=accent, anchor="mm")
    for i, line in enumerate(headline.split("\n")):
        d.text((W // 2, 530 + i * 115), line, font=font(108, "b"), fill="white", anchor="mm")
    return img


def end_card(product, sub, cta, url, accent):
    img = Image.new("RGBA", (W, H), (11, 11, 15, 255))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((W // 2 - 700, 120, W // 2 + 700, 960), fill=accent + (60,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(160)))
    d = ImageDraw.Draw(img)
    d.text((W // 2, 360), product, font=font(132, "b"), fill="white", anchor="mm")
    d.text((W // 2, 470), sub, font=font(50, "r"), fill=(205, 205, 215), anchor="mm")
    f = font(52, "b")
    bw = int(f.getlength(cta)) + 120
    d.rounded_rectangle((W // 2 - bw // 2, 580, W // 2 + bw // 2, 690), 55, fill=accent)
    d.text((W // 2, 635), cta, font=f, fill=(15, 15, 20) if accent == GOLD else "white", anchor="mm")
    d.text((W // 2, 770), url, font=font(44), fill=(235, 235, 240), anchor="mm")
    return img


def discord_toast(body, when="now"):
    """Quest-style system notification for a Discord webhook post from 'LiveChat XR'."""
    head = f"#stream-chat · Discord · {when}"
    tw = 150 + max(int(font(36, "b").getlength("LiveChat XR")) + 16 + int(font(28, "r").getlength(head)),
                   int(font(36, "r").getlength(body))) + 44
    th = 158
    img = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, tw - 1, th - 1), 34, fill=(28, 29, 34, 238), outline=(255, 255, 255, 30), width=2)
    # app icon: blurple tile with a white chat bubble (generic, not the Discord logo)
    d.rounded_rectangle((26, 30, 124, 128), 24, fill=BLURPLE)
    d.rounded_rectangle((47, 55, 103, 95), 14, fill="white")
    d.polygon([(58, 92), (58, 110), (76, 93)], fill="white")
    for x in (62, 75, 88):
        d.ellipse((x - 4, 71, x + 4, 79), fill=BLURPLE)
    d.text((150, 50), "LiveChat XR", font=font(36, "b"), fill="white", anchor="lm")
    hx = 150 + int(font(36, "b").getlength("LiveChat XR")) + 16
    d.text((hx, 52), head, font=font(28, "r"), fill=(170, 172, 182), anchor="lm")
    d.text((150, 106), body, font=font(36, "r"), fill=(235, 236, 240), anchor="lm")
    return img


def build(name, segs, hook, beats, card, accent, slide):
    """segs: [(src, start, dur)]; hook: (kicker, headline); beats: [(t0, t1, overlay_img, (x, y), caption)]"""
    total_game = sum(s[2] for s in segs)
    card_dur = 3.5
    total = total_game + card_dur
    args = ["ffmpeg", "-v", "error", "-y"]
    for src, ss, dur in segs:
        args += ["-ss", str(ss), "-t", str(dur), "-i", str(src)]
    overlays = []  # (png, t0, t1, x, y, slide)
    overlays.append((save(title(*hook, accent), f"{name}_title.png"), 0.25, 2.9, 0, 0, False))
    for i, (t0, t1, img, (x, y), cap) in enumerate(beats):
        overlays.append((save(img, f"{name}_beat{i}.png"), t0, t1, x, y, slide))
        c = caption(cap, accent)
        overlays.append((save(c, f"{name}_cap{i}.png"), t0 + 0.15, t1 + 0.3, 70, H - 150 - 92, False))
    overlays.append((save(end_card(*card, accent), f"{name}_end.png"), total_game - 0.4, total, 0, 0, False))
    for png, *_ in overlays:
        args += ["-framerate", str(FPS), "-loop", "1", "-t", f"{total:.2f}", "-i", str(png)]

    n = len(segs)
    fg = []
    for i in range(n):
        # 1440x1080 capture -> 16:9 window that keeps the Pop1 HUD, then 1080p
        fg.append(f"[{i}:v]crop=1440:810:0:190,scale={W}:{H}:flags=lanczos,setsar=1,fps={FPS},setpts=PTS-STARTPTS[v{i}];"
                  f"[{i}:a]aresample=48000,asetpts=PTS-STARTPTS[a{i}];")
    fg.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[base][aud];")
    last = "base"
    for k, (png, t0, t1, x, y, sl) in enumerate(overlays):
        idx = n + k
        fade = 0.3
        fg.append(f"[{idx}:v]format=rgba,fade=t=in:st={t0}:d={fade}:alpha=1,fade=t=out:st={max(t0, t1 - fade):.2f}:d={fade}:alpha=1[o{k}];")
        yexpr = f"{y}+80*max(0\\,1-(t-{t0})/0.35)" if sl else str(y)
        fg.append(f"[{last}][o{k}]overlay=x={x}:y='{yexpr}':enable='between(t,{t0},{t1})'[m{k}];")
        last = f"m{k}"
    fg.append(f"[{last}]fade=t=in:st=0:d=0.4,format=yuv420p[vout];"
              f"[aud]volume=0.55,apad,atrim=0:{total:.2f},afade=t=out:st={total_game - 0.8:.2f}:d=1.2[aout]")
    out = OUT / f"{name}.mp4"
    args += ["-filter_complex", "".join(fg), "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.2f}",
             "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-profile:v", "high", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    subprocess.run(args, check=True)
    # web extras: poster frame + muted-autoplay-friendly webm
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "8.5", "-i", str(out), "-frames:v", "1", "-q:v", "3",
                    str(OUT / f"{name}-poster.jpg")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out), "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34",
                    "-row-mt", "1", "-c:a", "libopus", "-b:a", "96k", str(OUT / f"{name}.webm")], check=True)
    print("wrote", out)


def pcvr():
    bx = lambda img: ((W - img.width) // 2, 120)
    b1 = dll_banner("Kayla_VR: that shot was insane")
    b2 = dll_banner("\U0001F381 BigFan sent Galaxy x3\nViewer One: nice shot!")
    b3 = dll_banner("moe23: what gun is that \U0001F52B\nDragonTTV: GG ez\nsnipeQueen: clip it!!\n+6 more")
    build("livechat-xr-pcvr",
          [(SRC_B, 0, 7), (SRC_A, 158, 5), (SRC_A, 144, 5)],
          ("LiveChat XR · PC VR", "Your chat.\nInside your headset."),
          [(3.2, 6.7, b1, bx(b1), "Twitch & TikTok LIVE chat, pinned to your view"),
           (7.3, 11.7, b2, bx(b2), "Gifts, Bits & subs pop in gold"),
           (12.3, 16.7, b3, bx(b3), "Busy chat folds into one banner")],
          ("LiveChat XR", "Live chat overlay for PC VR · Twitch & TikTok LIVE",
           "Download for Windows", "github.com/DeliciousHouse/livechat-xr"),
          GOLD, slide=False)


def standalone():
    pos = lambda img: ((W - img.width) // 2, 330)
    t1 = discord_toast("Kayla_VR: that shot was insane")
    t2 = discord_toast("BigFan sent Galaxy x3  ·  Viewer One: nice shot!")
    t3 = discord_toast("moe23: what gun is that?  ·  DragonTTV: GG ez")
    build("livechat-xr-standalone",
          [(SRC_B, 375, 7), (SRC_B, 225, 5), (SRC_B, 420, 5)],
          ("LiveChat XR · Standalone Quest", "No PC?\nYour chat still finds you."),
          [(3.2, 6.7, t1, pos(t1), "Your TikTok or Twitch chat posts to Discord"),
           (7.3, 11.7, t2, pos(t2), "Quest pops it up mid-match"),
           (12.3, 16.7, t3, pos(t3), "No PC, no sideloading. Just Discord.")],
          ("LiveChat XR", "Live chat for standalone Quest · via Discord",
           "Connect your channel", "livechat.deliciouswines.org"),
          BLURPLE, slide=True)


if __name__ == "__main__":
    WORK.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    for job in sys.argv[1:] or ["pcvr", "standalone"]:
        globals()[job]()
