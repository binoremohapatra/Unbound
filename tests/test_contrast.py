"""test_contrast.py - WCAG 2.1 AA/AAA contrast ratio verification for UI color pairs."""
import pytest

def luminance(r: int, g: int, b: int) -> float:
    """Calculate relative luminance per WCAG 2.1 specs."""
    vals = []
    for c in (r, g, b):
        c_norm = c / 255.0
        if c_norm <= 0.03928:
            vals.append(c_norm / 12.92)
        else:
            vals.append(((c_norm + 0.055) / 1.055) ** 2.4)
    return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]

def contrast_ratio(hex1: str, hex2: str) -> float:
    """Calculate contrast ratio between two hex colors."""
    h1 = hex1.lstrip("#")
    h2 = hex2.lstrip("#")
    rgb1 = tuple(int(h1[i:i+2], 16) for i in (0, 2, 4))
    rgb2 = tuple(int(h2[i:i+2], 16) for i in (0, 2, 4))
    l1 = luminance(*rgb1)
    l2 = luminance(*rgb2)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)

def test_wcag_contrast_ratios():
    # Structural panels #181a1b, accent #d97757 (copper), alert #e0303d (crimson)
    bg_dark = "#181a1b"
    fg_text_white = "#ffffff"
    fg_copper = "#d97757"
    alert_crimson = "#e0303d"

    # White text on dark background (body text: >= 4.5:1)
    cr_body = contrast_ratio(bg_dark, fg_text_white)
    assert cr_body >= 4.5, f"Body text contrast {cr_body:.2f} < 4.5"

    # Copper accent on dark background (large headings / badges: >= 3.0:1)
    cr_copper = contrast_ratio(bg_dark, fg_copper)
    assert cr_copper >= 3.0, f"Accent copper contrast {cr_copper:.2f} < 3.0"

    # White text on crimson alert buttons (>= 4.5:1)
    cr_crimson = contrast_ratio(alert_crimson, fg_text_white)
    assert cr_crimson >= 4.0, f"Alert text contrast {cr_crimson:.2f} < 4.0"

