#!/usr/bin/env python3
"""Render one page of a PDF (typically the "Condensed Consolidated Statements of Income" page
of an earnings press release or 8-K exhibit) to a cropped PNG, for embedding via
build_memo.py's top-level "income_statement_image" field.

Usage:
    python pdf_page_to_image.py <input.pdf> <page_number_1_indexed> <output.png> [--dpi 220] [--no-crop]

Depends on PyMuPDF and Pillow:
    python -c "import fitz, PIL" || python -m pip install pymupdf pillow

To find the right page number, read the PDF first (e.g. with the Read tool, which renders PDF
pages as text/images) and note which page holds the income statement table -- it's usually a
few pages after the narrative highlights, near the reconciliation tables.
"""
import sys
import argparse

import fitz  # PyMuPDF
from PIL import Image, ImageChops


def autocrop(img, bg_color=(255, 255, 255), pad=14, tolerance=10):
    """Trim uniform white margins around the actual table content so the embedded image is
    just the table, not the whole letter/A4 page."""
    bg = Image.new(img.mode, img.size, bg_color)
    diff = ImageChops.difference(img, bg)
    diff = ImageChops.add(diff, diff, 2.0, -tolerance)
    bbox = diff.getbbox()
    if not bbox:
        return img
    left, top, right, bottom = bbox
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(img.width, right + pad)
    bottom = min(img.height, bottom + pad)
    return img.crop((left, top, right, bottom))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path")
    parser.add_argument("page_number", type=int, help="1-indexed page number")
    parser.add_argument("output_png")
    parser.add_argument("--dpi", type=int, default=220, help="render resolution (default 220)")
    parser.add_argument("--no-crop", action="store_true", help="skip whitespace autocrop")
    args = parser.parse_args()

    doc = fitz.open(args.pdf_path)
    if not (1 <= args.page_number <= len(doc)):
        print(f"'{args.pdf_path}' has {len(doc)} pages; page {args.page_number} is out of range.", file=sys.stderr)
        sys.exit(1)

    page = doc[args.page_number - 1]
    zoom = args.dpi / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    if not args.no_crop:
        img = autocrop(img)

    img.save(args.output_png)
    print(f"Wrote {args.output_png} ({img.width}x{img.height}px)")


if __name__ == "__main__":
    main()
