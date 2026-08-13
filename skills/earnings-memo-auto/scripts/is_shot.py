"""Render the income-statement table out of an SEC EX-99.1 (or any HTML) into a cropped PNG.

Keeps the issuer's own markup and heading text — nothing is retyped. Picks the table that has
both a per-share line and a revenue/net-sales line; ties broken by table size. Pass an explicit
--index to override when the heuristic guesses wrong.

usage: python is_shot.py <url> <out.png> [--index N] [--width 1500] [--scale 2]
"""
import argparse
import re
import subprocess

import requests
import lxml.html as LH
from PIL import Image, ImageChops

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
UA = {"User-Agent": "mengc research mznika0227@gmail.com"}


def candidates(doc):
    out = []
    for i, t in enumerate(doc.xpath("//table")):
        txt = " ".join(t.text_content().split())
        if re.search(r"per (common )?share", txt, re.I) and re.search(r"revenue|net sales", txt, re.I):
            out.append({"i": i, "len": len(txt), "el": t, "head": txt[:110]})
    out.sort(key=lambda c: -c["len"])
    return out


def heading_html(t, want=3):
    parts, p, n = [], t.getprevious(), 0
    while p is not None and n < 10 and len(parts) < want:
        # comments/PIs are siblings too and have no text_content()
        if not isinstance(p.tag, str):
            p, n = p.getprevious(), n + 1
            continue
        if " ".join(p.text_content().split()):
            parts.append(LH.tostring(p, encoding="unicode"))
        p, n = p.getprevious(), n + 1
    return "".join(reversed(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--index", type=int, default=None)
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--height", type=int, default=2600)
    a = ap.parse_args()

    r = requests.get(a.url, headers=UA, timeout=60)
    r.raise_for_status()
    doc = LH.fromstring(r.content)

    cands = candidates(doc)
    print("candidates:")
    for c in cands:
        print("  idx", c["i"], "len", c["len"], "|", c["head"])
    if a.index is not None:
        # explicit index addresses //table directly, so it still works when the keyword filter
        # found nothing -- e.g. an issuer that puts EPS in a table separate from revenue
        el = doc.xpath("//table")[a.index]
        chosen = {"i": a.index, "el": el, "head": " ".join(el.text_content().split())[:110]}
        print("explicit idx", a.index, "|", chosen["head"])
    elif cands:
        chosen = cands[0]
    else:
        print("no candidate table matched both keywords -- pick one of these with --index N:")
        for i, t in enumerate(doc.xpath("//table")):
            x = " ".join(t.text_content().split())
            if re.search(r"revenue|net sales|per share", x, re.I):
                print("  idx", i, "len", len(x), "|", x[:95])
        raise SystemExit(2)

    html = (
        '<!doctype html><meta charset="utf-8">'
        '<style>body{margin:0;padding:24px;background:#fff;font-family:Arial,Helvetica,sans-serif}'
        "*{font-size:15px !important;line-height:1.35 !important}"
        "p,div,span{margin:0 !important;padding:0 !important}"
        "table{border-collapse:collapse}td,th{padding:1px 6px !important}</style><body>"
        + heading_html(chosen["el"])
        + LH.tostring(chosen["el"], encoding="unicode")
        + "</body>"
    )
    tmp = a.out + ".html"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)

    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--force-device-scale-factor={a.scale}", f"--window-size={a.width},{a.height}",
         f"--screenshot={a.out}", "file:///" + tmp.replace("\\", "/")],
        check=True, capture_output=True,
    )

    im = Image.open(a.out).convert("RGB")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    bbox = ImageChops.difference(im, bg).getbbox()
    if bbox:
        pad = 16
        im = im.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                      min(im.width, bbox[2] + pad), min(im.height, bbox[3] + pad)))
        im.save(a.out)
    print("chosen idx", chosen["i"], "->", a.out, im.size)


if __name__ == "__main__":
    main()
