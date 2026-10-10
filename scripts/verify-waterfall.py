#!/usr/bin/env python3
"""Verify the invariants a waterfall chart can silently break.

A waterfall's whole claim is that **the running total is conserved**: a start
total, a sequence of signed bridges, and an end total that reconciles. Every
way of breaking that claim renders perfectly — nothing errors, nothing
disappears, and neither `lint-skin.py` (colors, fonts, a11y) nor
`verify-geometry.py` (label masks vs later-painted nodes) reads a bar against
the arithmetic it participates in.

Each bar declares its role and value (`data-role`, `data-value`, `data-name`)
and each connector declares the running total it transports (`data-carry`).
The checker recomputes the walk from the declarations and holds the drawing to
it:

1. CONSERVATION — start + every signed delta must equal each subtotal and the
   end total, exactly. Declared values are data, so there is no tolerance.
2. SHARED SCALE AND BRIDGE GEOMETRY — the scale is fitted from the start
   anchor (baseline at its bottom, px-per-unit from its height over its
   value). Every total and subtotal must anchor at that baseline, and every
   bridge must span exactly the running totals before and after it, within
   GEOMETRY_TOLERANCE for integer-pixel rounding. Data coordinates round to
   the nearest pixel; they never snap to the 4px layout grid.
3. CARRIES — each gap between adjacent bars must be crossed by exactly one
   horizontal connector drawn at the running total it declares, spanning the
   full gap. A missing carry breaks the walk; a carry at the wrong level
   reconnects it to a different total.
4. PRINTED VALUES — every bar prints its declared value, and a delta prints
   it with an explicit sign. Geometry the reader cannot check against a
   number is taken on trust.
5. SIGN TREATMENT — increases share one fill and decreases another, and the
   two differ, so direction survives greyscale and colour-vision deficiency.
   At most one bar may take the accent focal treatment, which replaces its
   sign fill (the signed label and geometry still carry the direction).

6. NO TRANSFORMS — the checker reads raw coordinates, so a transform on a
   bar or carry, or on any <g>/<svg> ancestor of one, moves the rendered mark
   away from the arithmetic it was checked against. All three carriers are
   refused: the `transform` attribute, an inline `style`, and a rule in a
   <style> block. A value label carrying a transform (own or ancestor) is not
   counted as printed on its bar, because it is not drawn where it is read.

Markup is read through the stdlib html.parser.HTMLParser, never a regex,
following verify-slopegraph.py, so a tag is recognized exactly when a browser
would recognize it: a quoted `>` inside an attribute value does not end the
tag, a repeated attribute keeps its FIRST value, unquoted and single-quoted
values parse as their canonical form, and a comment's contents are never live
markup. The double-quote-only `[^>]*` regex reader this replaced let
`transform='translate(0,120)'` move the Headcount bridge 120px while the
checker printed OK. A <rect> that the raw text says declares data-role but
the parser cannot emit is reported, never dropped.

Degenerate data fails closed with a named finding: a start total of zero or
less has no scale to fit, and a zero delta is invisible ink that still
occupies a column.

Usage:
    python3 scripts/verify-waterfall.py --all
    python3 scripts/verify-waterfall.py skills/diagram-design/assets/example-waterfall.html

Exit: 0 clean, 1 findings, 2 usage.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = ROOT / "skills/diagram-design/assets"

COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Detection only - a raw-text count read alongside the parser, because the
# parser is exactly what a broken quote defeats. Geometry is never read here.
DECLARES_BAR_RE = re.compile(r"<rect\b[^>]*\bdata-role\s*=", re.IGNORECASE)
CSS_MOVES_MARK_RE = re.compile(
    r"(?:^|[{;}\n])\s*(?:-(?:webkit|moz|ms|o)-)?"
    r"(?P<prop>transform|translate|rotate|scale|x|y"
    r"|offset(?:-(?:path|distance|position|anchor|rotate))?)\s*:",
    re.IGNORECASE,
)

# Integer-pixel rounding on a shared scale cannot miss by more than 0.5px per
# edge; 0.75 leaves headroom for the fit itself while staying far below the
# smallest honest bridge this budget allows.
GEOMETRY_TOLERANCE = 0.75
# A carry must reach both bars it connects. 2px forgives stroke caps, nothing
# more.
CARRY_SPAN_TOLERANCE = 2.0
HORIZONTAL_TOLERANCE = 1e-9
MAX_BARS = 8
MAX_SUBTOTALS = 1
ACCENT_STROKES = {"#eb6c36", "#f08a59"}
SIGN_CHARS = {"+": 1, "-": -1, "\u2212": -1}


GROUP_TAGS = ("g", "svg")          # the only ancestors whose transform is inherited
BODY_TAGS = ("text", "style")      # elements whose character data is read
MARK_TAGS = ("rect", "line")       # the elements this contract binds


class Element:
    """One start tag this checker cares about, as the browser tokenized it."""

    __slots__ = ("tag", "attrs", "body", "ancestor")

    def __init__(self, tag: str, attrs: dict[str, str], ancestor: str | None) -> None:
        self.tag = tag
        self.attrs = attrs          # first-wins dict, names lower-cased, values unescaped
        self.body = ""              # character data up to the matching end tag
        self.ancestor = ancestor    # how the nearest transformed <g>/<svg> moves it, or None


def first_wins(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    """Attributes as the browser keeps them: on a repeat, the FIRST wins.

    HTML parsing drops a duplicate attribute rather than overwriting it, so a
    second `y` on a bar is not in the document at all. A present-but-valueless
    attribute is an empty string, not an absent one.
    """
    seen: dict[str, str] = {}
    for name, value in attrs:
        seen.setdefault(name, "" if value is None else value)
    return seen


class Scanner(HTMLParser):
    """Collect rects, lines, texts and styles, each with transformed ancestry.

    The stdlib parser, never a `[^>]*` regex, following verify-slopegraph.py.
    HTMLParser lowercases names, reads unquoted and single-quoted values,
    unescapes entities, keeps a quoted `>` inside the value it belongs to,
    and never emits tag-like text inside a comment or <script>/<style> raw
    text. The regex reader this replaced read only double-quoted values and
    stopped at the first `>`, so `transform='translate(0,120)'` on a bridge,
    or `<g data-note=">" transform="...">` around one, moved a bar 120px
    while the checker printed OK - and a commented-out bar was still read as
    live data.
    """

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.rects: list[Element] = []
        self.lines: list[Element] = []
        self.texts: list[Element] = []
        self.styles: list[Element] = []
        self.error: str | None = None
        self._groups: list[tuple[str, str | None]] = []
        self._open: list[tuple[str, Element]] = []
        try:
            self.feed(source)
            self.close()
        except Exception as exc:  # noqa: BLE001 - any parser failure fails closed
            self.error = f"{type(exc).__name__}: {exc}"

    def _ancestor(self) -> str | None:
        for _tag, how in reversed(self._groups):
            if how is not None:
                return how
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, first_wins(attrs), closes=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, first_wins(attrs), closes=True)

    def _start(self, tag: str, attrs: dict[str, str], closes: bool) -> None:
        if tag in GROUP_TAGS:
            if not closes:
                self._groups.append((tag, transform_carrier(attrs)))
            return
        if tag not in MARK_TAGS and tag not in BODY_TAGS:
            return
        element = Element(tag, attrs, self._ancestor())
        if tag == "rect":
            self.rects.append(element)
            return
        if tag == "line":
            self.lines.append(element)
            return
        (self.texts if tag == "text" else self.styles).append(element)
        if not closes:
            self._open.append((tag, element))

    def handle_endtag(self, tag: str) -> None:
        stack = self._groups if tag in GROUP_TAGS else self._open if tag in BODY_TAGS else None
        if stack is None:
            return
        # Pop back to the matching open tag. An unclosed transformed group is
        # never popped, so it covers everything after it.
        for index in range(len(stack) - 1, -1, -1):
            if stack[index][0] == tag:
                del stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._open:
            self._open[-1][1].body += data


def parse_number(raw: str) -> float | None:
    try:
        value = float(raw)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def parse_exact_number(raw: str) -> Fraction | None:
    """Keep finite decimal declarations exact; geometry still uses floats."""
    projected = parse_number(raw)
    if projected is None:
        return None
    if projected == 0:
        # Inspect only the decimal coefficient/exponent before constructing a
        # rational denominator; nonzero underflow is not representable geometry.
        try:
            if not Decimal(raw).is_zero():
                return None
        except InvalidOperation:
            return None
        return Fraction(0)
    try:
        return Fraction(raw)
    except (ValueError, ZeroDivisionError):
        return None


def transform_carrier(attrs: dict[str, str]) -> str | None:
    """Describe an attribute or inline style that can move verified geometry."""
    if "transform" in attrs:
        return f"transform={attrs['transform']!r}"
    style = attrs.get("style")
    if style is not None:
        found = CSS_MOVES_MARK_RE.search(style)
        if found is not None:
            return f"style={style!r} (the {found.group('prop').lower()} property)"
    return None


def check_transforms(doc: Scanner, errors: list[str]) -> None:
    """Reject transforms that make rendered bar/carry geometry differ from raw coordinates.

    All three carriers - the `transform` attribute, an inline style, and a
    rule in a <style> block - on the element and on every <g>/<svg> above it.
    Attributes are read as the parser tokenized them, so an unquoted or
    single-quoted transform, or one behind a quoted `>`, is still read.
    """
    for elements, contract_attr, label in (
        (doc.rects, "data-role", "waterfall bar"),
        (doc.lines, "data-carry", "waterfall carry"),
    ):
        for element in elements:
            if contract_attr not in element.attrs:
                continue
            how = transform_carrier(element.attrs)
            if how is None and element.ancestor is not None:
                how = f"ancestor {element.ancestor}"
            if how is not None:
                errors.append(
                    f"{label} carries {how}; bake the movement into its coordinates so "
                    "the verifier checks what the browser draws"
                )

    for element in doc.styles:
        found = CSS_MOVES_MARK_RE.search(element.body)
        if found is not None:
            errors.append(
                f"CSS {found.group('prop').lower()!r} declaration can move verified waterfall "
                "geometry; bake the movement into the coordinates instead"
            )


class Bar:
    __slots__ = ("role", "value", "exact_value", "name", "x", "y", "w", "h", "fill", "stroke", "index")

    def __init__(self, index: int, role: str, value: Fraction, name: str,
                 x: float, y: float, w: float, h: float, fill: str, stroke: str) -> None:
        self.index = index
        self.role = role
        self.value = float(value)
        self.exact_value = value
        self.name = name
        self.x, self.y, self.w, self.h = x, y, w, h
        self.fill = fill
        self.stroke = stroke

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def label(self) -> str:
        return f"{self.name!r} (bar {self.index + 1})"


def parse_signed(raw: str, *, signed_required: bool) -> tuple[Fraction | None, bool]:
    """Return (value, had_explicit_sign) for a declared or printed value."""
    text = raw.strip()
    if not text:
        return None, False
    sign = SIGN_CHARS.get(text[0])
    if sign is not None:
        magnitude = parse_exact_number(text[1:].strip())
        if magnitude is None:
            return None, True
        return sign * magnitude, True
    if signed_required:
        return parse_exact_number(text), False
    return parse_exact_number(text), False


def parse_bars(doc: Scanner, source: str, errors: list[str]) -> list[Bar]:
    bars: list[Bar] = []
    contract_rects = 0
    for element in doc.rects:
        attrs = element.attrs
        role = attrs.get("data-role")
        if role is None:
            continue
        contract_rects += 1
        if role not in {"total", "delta", "subtotal"}:
            errors.append(f"bar {len(bars) + 1} declares unknown data-role {role!r}")
            continue
        name = attrs.get("data-name", f"#{len(bars) + 1}")
        raw_value = attrs.get("data-value", "")
        value, signed = parse_signed(raw_value, signed_required=(role == "delta"))
        if value is None:
            errors.append(f"{name!r}: data-value {raw_value!r} is not a number")
            continue
        if role == "delta" and not signed:
            errors.append(
                f"{name!r}: a delta must declare an explicit sign in data-value; got {raw_value!r}"
            )
        geometry = [parse_number(attrs.get(key, "")) for key in ("x", "y", "width", "height")]
        if any(v is None for v in geometry):
            errors.append(f"{name!r}: bar rect is missing numeric x/y/width/height")
            continue
        x, y, w, h = geometry  # type: ignore[assignment]
        if w <= 0 or h <= 0 or not all(map(math.isfinite, (x + w, y + h))):
            errors.append(f"{name!r}: bar geometry must have finite edges and positive dimensions")
            continue
        bars.append(
            Bar(len(bars), role, value, name, x, y, w, h,
                attrs.get("fill", ""), attrs.get("stroke", ""))
        )
    # A <rect> whose raw text declares data-role but which the parser never
    # emitted with one is markup the browser reads differently from its
    # author - most often a broken quote that swallows the tag. Dropping it
    # silently would close the walk around a missing bar.
    declared = len(DECLARES_BAR_RE.findall(COMMENT_RE.sub("", source)))
    if declared > contract_rects:
        errors.append(
            f"{declared} <rect> tag(s) declare data-role in the raw text but "
            f"{declared - contract_rects} could not be parsed as bars; check the quoting of every bar"
        )
    return bars


def check_structure(bars: list[Bar], errors: list[str]) -> bool:
    if len(bars) < 3:
        errors.append(f"a waterfall needs a start total, at least one delta, and an end total; found {len(bars)} bars")
        return False
    if len(bars) > MAX_BARS:
        errors.append(f"waterfall exceeds the budget of {MAX_BARS} bars: found {len(bars)}")
    if bars[0].role != "total":
        errors.append(f"the first bar must be the start total; {bars[0].label} declares {bars[0].role!r}")
        return False
    if bars[-1].role != "total":
        errors.append(f"the last bar must be the end total; {bars[-1].label} declares {bars[-1].role!r}")
        return False
    interior = bars[1:-1]
    for bar in interior:
        if bar.role == "total":
            errors.append(f"{bar.label}: an interior resting point is a subtotal, not a total")
            return False
    subtotals = [bar for bar in interior if bar.role == "subtotal"]
    if len(subtotals) > MAX_SUBTOTALS:
        errors.append(
            f"a walk that needs {len(subtotals)} subtotals is two walks; the budget allows {MAX_SUBTOTALS}"
        )
    if not any(bar.role == "delta" for bar in interior):
        errors.append("a waterfall with no delta bars is a bar chart wearing carries")
        return False
    for bar in bars:
        if bar.role in {"total", "subtotal"} and bar.value <= 0:
            errors.append(
                f"{bar.label}: a {bar.role} of {bar.value:g} has no anchored height on this grammar; "
                "totals must be positive"
            )
            return False
        if bar.role == "delta" and bar.value == 0:
            errors.append(f"{bar.label}: a zero delta is dropped, never drawn")
    return True


def running_levels(bars: list[Bar], errors: list[str]) -> list[tuple[Fraction, Fraction]] | None:
    """Return each bar's (level_before, level_after); None when conservation fails."""
    levels: list[tuple[Fraction, Fraction]] = []
    running = bars[0].exact_value
    levels.append((Fraction(0), running))
    conserved = True
    for bar in bars[1:]:
        if bar.role == "delta":
            before = running
            running += bar.exact_value
            try:
                finite_running = math.isfinite(float(running))
            except OverflowError:
                finite_running = False
            if not finite_running:
                errors.append(f"{bar.label}: the running total must remain finite")
                return None
            levels.append((before, running))
            if running < 0:
                errors.append(
                    f"{bar.label}: the running total falls to {float(running):g}; this grammar anchors totals "
                    "at a zero floor and cannot draw a negative walk"
                )
                conserved = False
        else:
            if running != bar.exact_value:
                errors.append(
                    f"{bar.label}: declares {bar.value:g} but the walk arrives at {float(running):g}; "
                    "the running total must conserve"
                )
                conserved = False
            levels.append((running, running))
            running = bar.exact_value
    return levels if conserved else None


def check_geometry(bars: list[Bar], levels: list[tuple[Fraction, Fraction]], errors: list[str]) -> tuple[float, float] | None:
    start = bars[0]
    if start.h <= 0:
        errors.append(f"{start.label}: the start anchor has no height; no scale can be fitted")
        return None
    baseline = start.bottom
    scale = start.h / start.value
    for bar, (before, after) in zip(bars, levels):
        if bar.role in {"total", "subtotal"}:
            lo, hi = 0.0, bar.value
        else:
            lo, hi = min(before, after), max(before, after)
        expected_top = baseline - scale * hi
        expected_bottom = baseline - scale * lo
        if scale <= 0 or not all(map(math.isfinite, (scale, expected_top, expected_bottom))):
            errors.append(f"{bar.label}: shared scale and derived geometry must be finite and nonzero")
            return None
        if abs(bar.y - expected_top) > GEOMETRY_TOLERANCE:
            errors.append(
                f"{bar.label}: top edge drawn at y={bar.y:g} but the shared scale puts "
                f"the {float(hi):g} level at y={expected_top:.1f}"
            )
        if abs(bar.bottom - expected_bottom) > GEOMETRY_TOLERANCE:
            errors.append(
                f"{bar.label}: bottom edge drawn at y={bar.bottom:g} but the shared scale puts "
                f"the {float(lo):g} level at y={expected_bottom:.1f}"
            )
    return baseline, scale


def check_carries(doc: Scanner, bars: list[Bar], levels: list[tuple[Fraction, Fraction]],
                  baseline: float, scale: float, errors: list[str]) -> None:
    carries: list[tuple[float, float, float, Fraction, str]] = []
    for element in doc.lines:
        attrs = element.attrs
        declared = attrs.get("data-carry")
        if declared is None:
            continue
        value = parse_exact_number(declared)
        coords = [parse_number(attrs.get(key, "")) for key in ("x1", "y1", "x2", "y2")]
        if value is None or any(v is None for v in coords):
            errors.append(f"carry {declared!r}: non-numeric data-carry or coordinates")
            continue
        x1, y1, x2, y2 = coords  # type: ignore[assignment]
        if abs(y1 - y2) > HORIZONTAL_TOLERANCE:
            errors.append(f"carry {declared!r}: a carry is horizontal; drawn from y={y1:g} to y={y2:g}")
            continue
        carries.append((min(x1, x2), max(x1, x2), y1, value, declared))

    for left, right, (_, level) in zip(bars, bars[1:], levels):
        gap_start, gap_end = left.right, right.x
        matched = [
            carry for carry in carries
            if carry[0] <= gap_start + CARRY_SPAN_TOLERANCE and carry[1] >= gap_end - CARRY_SPAN_TOLERANCE
            and carry[0] >= left.x and carry[1] <= right.right
        ]
        gap_label = f"gap between {left.name!r} and {right.name!r}"
        if not matched:
            errors.append(f"{gap_label}: no carry spans it; the running total is not conserved across the gap")
            continue
        if len(matched) > 1:
            errors.append(f"{gap_label}: {len(matched)} carries cross one gap; a gap has one running total")
            continue
        _, _, y, value, declared = matched[0]
        if value != level:
            errors.append(
                f"{gap_label}: carry declares {declared} but the running total between these bars is {float(level):g}"
            )
        expected_y = baseline - scale * level
        if abs(y - expected_y) > GEOMETRY_TOLERANCE:
            errors.append(
                f"{gap_label}: carry drawn at y={y:g} but the {float(level):g} level sits at y={expected_y:.1f}"
            )


def printed_values(doc: Scanner, bar: Bar) -> list[tuple[Fraction, bool]]:
    values: list[tuple[Fraction, bool]] = []
    for element in doc.texts:
        attrs = element.attrs
        x, y = (parse_number(attrs.get(key, "")) for key in ("x", "y"))
        if any(key in attrs for key in ("style", "display", "visibility", "opacity", "fill-opacity")):
            continue
        # A transformed label is not drawn at the x/y read here, so it cannot
        # be the label that sits on this bar.
        if "transform" in attrs or element.ancestor is not None:
            continue
        expected_y = bar.bottom + 12 if bar.role == "delta" and bar.value < 0 else bar.y - 8
        if x is None or y is None or attrs.get("text-anchor") != "middle":
            continue
        if abs(x - (bar.x + bar.w / 2)) > GEOMETRY_TOLERANCE or abs(y - expected_y) > GEOMETRY_TOLERANCE:
            continue
        body = element.body.strip()
        value, signed = parse_signed(body, signed_required=False)
        if value is not None:
            values.append((value, signed))
    return values


def check_printed(doc: Scanner, bars: list[Bar], errors: list[str]) -> None:
    for bar in bars:
        printed = printed_values(doc, bar)
        needs_sign = bar.role == "delta"
        hit = any(
            value == bar.exact_value and (signed or not needs_sign)
            for value, signed in printed
        )
        if not hit:
            wanted = f"{bar.value:+g}" if needs_sign else f"{bar.value:g}"
            errors.append(
                f"{bar.label}: no printed label states its value {wanted}"
                + (" with an explicit sign" if needs_sign else "")
            )


def check_sign_treatment(bars: list[Bar], errors: list[str], paper: str) -> None:
    focal = [bar for bar in bars if bar.stroke.lower() in ACCENT_STROKES]
    if len(focal) > 1:
        errors.append(
            "the accent focal treatment is editorial and marks at most one bar; found "
            + ", ".join(bar.label for bar in focal)
        )
    focal_set = set(id(bar) for bar in focal)
    # Absolute check: the documented mapping is tint-for-increase and hollow
    # paper-for-decrease, both over the muted stroke. Comparing the two polarity
    # sets against each other cannot see a single-polarity walk, or one whose
    # only bar of a polarity is focal, or a chart with the treatments swapped.
    # Only the two shipped themes have a documented pair; an unrecognised paper
    # falls through to the relative checks below rather than failing the file.
    THEME_TREATMENTS = {
        "#2d3142": ("rgba(191,192,192,0.15)", "#bfc0c0"),
        "#f5f5f5": ("rgba(79,93,117,0.15)", "#4f5d75"),
    }
    if paper in THEME_TREATMENTS:
        tint, stroke = THEME_TREATMENTS[paper]
        for bar in bars:
            if bar.role != "delta" or id(bar) in focal_set:
                continue
            expected_fill = tint if bar.value > 0 else paper
            if re.sub(r"\s+", "", bar.fill.lower()) != expected_fill or bar.stroke.lower() != stroke:
                errors.append(
                    f"{bar.label}: increases require the muted tint and decreases require "
                    f"hollow paper, both over the muted stroke"
                )
    rises = {bar.fill for bar in bars if bar.role == "delta" and bar.value > 0 and id(bar) not in focal_set}
    falls = {bar.fill for bar in bars if bar.role == "delta" and bar.value < 0 and id(bar) not in focal_set}
    if len(rises) > 1:
        errors.append(f"increases must share one fill; found {sorted(rises)}")
    if len(falls) > 1:
        errors.append(f"decreases must share one fill; found {sorted(falls)}")
    if len(rises) == 1 and len(falls) == 1 and rises == falls:
        errors.append(
            "increases and decreases share the same fill; sign must survive greyscale, "
            "so the two directions need distinct fill weights"
        )


def verify_file(path: Path) -> list[str]:
    errors: list[str] = []
    source = path.read_text(encoding="utf-8")
    paper_match = re.search(r'--color-paper\s*:\s*(#[0-9a-fA-F]{6})', source)
    paper = paper_match[1].lower() if paper_match else None
    doc = Scanner(source)
    if doc.error is not None:
        errors.append(f"could not be parsed as HTML ({doc.error}); refusing to report OK on it")
        return errors
    bars = parse_bars(doc, source, errors)
    if not bars:
        errors.append("no bars declare data-role/data-value; the waterfall data contract is missing")
        return errors
    if not check_structure(bars, errors):
        return errors
    check_transforms(doc, errors)
    levels = running_levels(bars, errors)
    if levels is None:
        return errors
    fitted = check_geometry(bars, levels, errors)
    if fitted is not None:
        baseline, scale = fitted
        check_carries(doc, bars, levels, baseline, scale, errors)
    check_printed(doc, bars, errors)
    check_sign_treatment(bars, errors, paper)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify waterfall running-total conservation and bridge geometry.")
    parser.add_argument("paths", nargs="*", help="waterfall example HTML files")
    parser.add_argument("--all", action="store_true", help="check every shipped example-waterfall*.html")
    args = parser.parse_args()

    if args.all:
        paths = sorted(ASSET_DIR.glob("example-waterfall*.html"))
        if not paths:
            print("FAIL waterfall: --all found no example-waterfall*.html under the skill assets")
            return 2
    elif args.paths:
        paths = [Path(p) for p in args.paths]
    else:
        parser.print_usage()
        return 2

    failed = False
    for path in paths:
        if not path.is_file():
            print(f"FAIL {path}: no such file")
            failed = True
            continue
        findings = verify_file(path)
        if findings:
            failed = True
            print(f"FAIL {path}")
            for finding in findings:
                print(f"  - {finding}")
        else:
            print(f"OK {path}")
    if failed:
        return 1
    print(f"OK waterfall: {len(paths)} file(s) conserve the running total and draw it where they claim")
    return 0


if __name__ == "__main__":
    sys.exit(main())
