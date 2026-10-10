#!/usr/bin/env python3
"""Verify the invariants a treemap can silently break.

A treemap's whole claim is that **area is the encoding**. Every way of breaking
that claim renders perfectly - nothing errors, nothing disappears, and neither
`lint-skin.py` (colors, fonts, a11y) nor `verify-geometry.py` (label masks vs
later-painted nodes) reads a cell against the number printed inside it.

1. AREA FIDELITY - a cell's share of the drawn area must match the share its
   own label claims. This is checked as **relative** error, not percentage
   points: a 0.13pp slip is noise on a 59% cell and a quarter of a 0.5% cell,
   so an absolute threshold passes exactly the cell most likely to be wrong.
   The failure is real - the smallest cell absorbs the whole 4px-grid residue,
   because 4px is a third of a sliver and a rounding error on a giant.

2. LABEL AND MARKER FIT - a label or information marker must stay inside the
   cell it names. Cells have no clip path, so overflow bleeds into a neighbour
   or off the viewBox and reads as belonging to the wrong thing. A cell too
   small for its annotation is not a nudge-the-coordinates problem: drop the
   annotation (the cell keeps its honest area) or merge the tail into a named
   "Other".

3. TILING AND NO TRANSFORMS - cells tile the plot without overlapping (a
   shared edge is fine), and no transform may move or resize a cell, a hosted
   label or a marker, by any carrier (the `transform` attribute, an inline
   `style`, a rule in a <style> block), on the element or on an ancestor
   <g>/<svg>. Both defeat check 1 while every number still matches: a cell
   laid over its neighbour keeps its honest width x height, and
   `transform="scale(0.5)"` quarters the drawn area behind attributes that
   still state the full size. Transforms are rejected, not resolved, after
   verify-marimekko.py; the one exception is a label's `rotate(±90 x y)` about
   its own anchor, which the fit check models.

Markup is read through the stdlib html.parser.HTMLParser, not a regex, so a
double-, single- or un-quoted attribute is read exactly as the browser reads
it, a quoted `>` does not end a tag, a repeated attribute keeps its first
value, and nothing inside an HTML comment is live.

Text extent is estimated from font metrics rather than measured in a browser,
deliberately: every other gate here is pure Python and runs in CI with no
browser. The Latin advances below are calibrated against Chromium renderings of
the shipped Geist / Geist Mono faces and rounded UP. Unicode wide/full-width
characters use a conservative 1em advance, so both estimates report overflow
slightly before real overflow, never after.

Usage:
    python3 scripts/verify-treemap.py --all
    python3 scripts/verify-treemap.py skills/diagram-design/assets/example-treemap.html

Exit: 0 clean, 1 findings, 2 usage.
"""

from __future__ import annotations

import argparse
import html
import math
import re
import sys
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = ROOT / "skills/diagram-design/assets"

# A label may turn a quarter about its OWN anchor and nothing else: that is
# the one transform label_box models. `rotate(-90)` without a centre turns about
# the origin and throws the label across the canvas, so the centre is required.
NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
ROTATE_RE = re.compile(
    rf"^\s*rotate\(\s*(?P<deg>{NUM})\s*[,\s]\s*(?P<cx>{NUM})\s*[,\s]\s*(?P<cy>{NUM})\s*\)\s*$"
)
TAG_RE = re.compile(r"<[^>]+>")
# CSS properties that move or resize a mark without touching the attributes
# this checker reads, after verify-marimekko.py. Anchored to a declaration
# start so `text-transform:` never matches; a vendor prefix is optional so
# `-webkit-transform:` is not a free pass.
CSS_TRANSFORM_PROPS = (
    r"transform|translate|rotate|scale|offset(?:-(?:path|distance|position|anchor|rotate))?"
)
CSS_TRANSFORM_RE = re.compile(
    r"(?:^|[{;}\n])\s*(?:-(?:webkit|moz|ms|o)-)?(?P<prop>" + CSS_TRANSFORM_PROPS + r")\s*:",
    re.IGNORECASE,
)
# On a cell rect itself, SVG 2 geometry properties also redraw it behind its
# attributes: `style="width: 100px"` paints a size data-share was never checked
# against.
CSS_CELL_GEOMETRY_RE = re.compile(
    r"(?:^|[{;}\n])\s*(?:-(?:webkit|moz|ms|o)-)?(?P<prop>" + CSS_TRANSFORM_PROPS
    + r"|x|y|width|height)\s*:",
    re.IGNORECASE,
)
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
CSS_RULE_RE = re.compile(r"(?P<selector>[^{}]*)\{(?P<body>[^{}]*)\}")
GROUP_TAGS = ("g", "svg")  # the only ancestors whose transform is inherited
# A cell states its size one of two ways, and the two must not be confused: a
# magnitude ("4.78B") is a value to be normalised against the other cells, while
# a percentage ("59%", "0.6%") is already the claimed share. Reading "0.6%" as
# the number 0.6 against cells measured in billions produces nonsense, so the
# percentage pattern is tried first and anchored on the sign.
PCT_RE = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*%")
VALUE_RE = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>[BMK])\b")

MONO_ADVANCE = 0.62
SANS_ADVANCE = 0.60
WIDE_ADVANCE = 1.00
ASCENT = 0.74
UNITS = {None: 1.0, "K": 1e3, "M": 1e6, "B": 1e9}

# A treemap cell is large enough to hold a label and small enough not to be a
# page background or diagram container.
CELL_MIN_AREA = 400.0
CELL_MAX_W = 900.0
CELL_MAX_H = 460.0
EPSILON = 1.0
# Relative area tolerance. Grid snapping on a 1000x500 viewBox cannot do better
# than a couple of percent on a sub-1% cell; 8% is loose enough never to fire on
# honest rounding and tight enough to have caught the 30%-undersized sliver this
# check exists for.
AREA_TOLERANCE = 8.0
MARKER_EPSILON = 0.01


class Box:
    __slots__ = (
        "x",
        "y",
        "w",
        "h",
        "line",
        "share",
        "share_errors",
        "rect_count",
        "transforms",
    )

    def __init__(
        self, x: float, y: float, w: float, h: float, line: int = 0, share: float | None = None
    ) -> None:
        self.x, self.y, self.w, self.h, self.line = x, y, w, h, line
        # (line, how) for every rect of this cell that a transform reaches.
        self.transforms: list[tuple[int, str]] = []
        # Declared share of the whole, from data-share. None means the cell
        # never stated one, which for a treemap is itself a finding.
        self.share = share
        self.share_errors: list[str] = []
        self.rect_count = 1

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def area(self) -> float:
        return self.w * self.h

    def contains_point(self, x: float, y: float) -> bool:
        return (
            self.x - EPSILON <= x <= self.right + EPSILON
            and self.y - EPSILON <= y <= self.bottom + EPSILON
        )


def plain(body: str) -> str:
    return html.unescape(TAG_RE.sub("", body)).strip()


def blank_css_comments(css: str) -> str:
    """A browser reads `/* */` as whitespace, so `style="/**/transform:..."` is live."""
    return CSS_COMMENT_RE.sub(lambda found: " " + "\n" * found.group().count("\n"), css)


def first_wins(attrs) -> dict[str, str]:
    """Attributes as the browser keeps them: on a repeat, the FIRST wins."""
    seen: dict[str, str] = {}
    for name, value in attrs:
        seen.setdefault(name, "" if value is None else value)
    return seen


def transform_carrier(attrs: dict[str, str], pattern: re.Pattern[str] = CSS_TRANSFORM_RE):
    """How this element carries a transform, phrased for a finding, or None."""
    if "transform" in attrs:
        return "transform=%r" % attrs["transform"]
    style = attrs.get("style")
    if style is not None:
        found = pattern.search(blank_css_comments(style))
        if found is not None:
            return "style=%r (the %s property)" % (style, found.group("prop").lower())
    return None


class Element:
    __slots__ = ("tag", "attrs", "line", "body", "ancestor")

    def __init__(self, tag: str, attrs: dict[str, str], line: int, ancestor: str | None) -> None:
        self.tag = tag
        self.attrs = attrs  # first-wins, names lower-cased, values unescaped
        self.line = line
        self.body = ""  # character data up to the matching end tag
        self.ancestor = ancestor  # how the nearest transformed <g>/<svg> moves it, or None


class _Scanner(HTMLParser):
    """Collect rects, circles, texts and <style> bodies, with ancestor transforms.

    Markup is read the way a browser tokenizes it, after verify-marimekko.py:
    HTMLParser accepts double-, single- and un-quoted values, keeps a quoted
    `>` inside the value it belongs to, and never reports tag-like text inside
    a comment. A regex that read only `name="value"` let `transform='...'` or
    `transform=scale(.5)` hide from the checker while the browser applied it.
    """

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.rects: list[Element] = []
        self.circles: list[Element] = []
        self.texts: list[Element] = []
        self.styles: list[Element] = []
        self.error: str | None = None
        self._groups: list[tuple[str, str | None]] = []
        self._open: list[tuple[str, Element]] = []
        try:
            self.feed(source)
            self.close()
        except Exception as exc:  # noqa: BLE001 - any parser failure fails closed
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def _ancestor(self) -> str | None:
        for _tag, how in reversed(self._groups):
            if how is not None:
                return how
        return None

    def handle_starttag(self, tag, attrs):
        self._start(tag, first_wins(attrs), closes=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, first_wins(attrs), closes=True)

    def _start(self, tag: str, attrs: dict[str, str], closes: bool) -> None:
        if tag in GROUP_TAGS:
            if not closes:
                how = transform_carrier(attrs)
                self._groups.append((tag, None if how is None else f"an ancestor <{tag}> {how}"))
            return
        if tag not in ("rect", "circle", "text", "style"):
            return
        element = Element(tag, attrs, self.getpos()[0], self._ancestor())
        if tag == "rect":
            self.rects.append(element)
        elif tag == "circle":
            self.circles.append(element)
        else:
            (self.texts if tag == "text" else self.styles).append(element)
            if not closes:
                self._open.append((tag, element))

    def handle_endtag(self, tag):
        stack = self._groups if tag in GROUP_TAGS else self._open if tag in ("text", "style") else None
        if stack is None:
            return
        for index in range(len(stack) - 1, -1, -1):
            if stack[index][0] == tag:
                del stack[index:]
                return

    def handle_data(self, data):
        if self._open:
            self._open[-1][1].body += data


def estimated_advance(text: str, mono: bool) -> float:
    """Conservative text advance in em, preserving the calibrated Latin baseline."""
    narrow_advance = MONO_ADVANCE if mono else SANS_ADVANCE
    advance = 0.0
    for char in text:
        # Nonspacing and enclosing marks modify the preceding glyph; counting
        # them as another glyph rejects labels that do fit. Spacing combining
        # marks (Mc) retain the narrow estimate because they can advance.
        if unicodedata.category(char) in {"Mn", "Me"}:
            continue
        if unicodedata.east_asian_width(char) in {"W", "F"}:
            advance += WIDE_ADVANCE
        else:
            advance += narrow_advance
    return advance


def parse_cells(source: str, findings: list[str] | None = None) -> list[Box]:
    """Deduped cell rects. Each cell is painted twice: paper mask, then body.

    `data-share` may sit on either rect of the pair, so the declared share is
    merged across duplicates rather than read off whichever came first.
    """
    return _cells_from(_Scanner(source), findings)


def _cells_from(doc: _Scanner, findings: list[str] | None = None) -> list[Box]:
    seen: list[Box] = []
    for element in doc.rects:
        attrs = element.attrs
        has_share_metadata = "data-share" in attrs
        if attrs.get("rx") != "2" and not has_share_metadata:
            continue
        try:
            box = Box(
                float(attrs["x"]),
                float(attrs["y"]),
                float(attrs["width"]),
                float(attrs["height"]),
                element.line,
            )
        except (KeyError, ValueError) as exc:
            if has_share_metadata and findings is not None:
                findings.append(
                    f"line {element.line}: data-share rect has missing or "
                    f"unparseable x/y/width/height geometry ({exc})"
                )
            continue
        coordinates = (box.x, box.y, box.w, box.h)
        if has_share_metadata and (
            not all(math.isfinite(value) for value in coordinates) or box.w <= 0 or box.h <= 0
        ):
            if findings is not None:
                findings.append(
                    f"line {element.line}: data-share rect geometry must have "
                    "finite x/y/width/height and strictly positive width/height; "
                    f"got x={box.x:g}, y={box.y:g}, width={box.w:g}, height={box.h:g}"
                )
            continue
        how = transform_carrier(attrs, CSS_CELL_GEOMETRY_RE) or element.ancestor
        if how is not None:
            box.transforms.append((element.line, how))
        if "data-share" in attrs:
            raw_share = attrs["data-share"]
            try:
                parsed_share = float(raw_share)
            except ValueError:
                box.share_errors.append(f"data-share {raw_share!r} is not numeric")
            else:
                if not math.isfinite(parsed_share):
                    box.share_errors.append(f"data-share {raw_share!r} is not finite")
                elif not 0 < parsed_share <= 100:
                    box.share_errors.append(
                        f"data-share {raw_share!r} must be greater than 0 and at most 100"
                    )
                else:
                    box.share = parsed_share
        twin = next(
            (
                s
                for s in seen
                if abs(s.x - box.x) < 0.01
                and abs(s.y - box.y) < 0.01
                and abs(s.w - box.w) < 0.01
                and abs(s.h - box.h) < 0.01
            ),
            None,
        )
        if twin is not None:
            twin.rect_count += 1
            twin.transforms.extend(box.transforms)
            twin.share_errors.extend(box.share_errors)
            if twin.share is not None and box.share is not None and not math.isclose(
                twin.share, box.share, rel_tol=0.0, abs_tol=1e-9
            ):
                twin.share_errors.append(
                    f"duplicate rects declare conflicting data-share values "
                    f"{twin.share:g} and {box.share:g}"
                )
            if twin.share is None:
                twin.share = box.share
            continue
        seen.append(box)
    return [
        box
        for box in seen
        if box.share is not None
        or box.share_errors
        or (box.area >= CELL_MIN_AREA and box.w <= CELL_MAX_W and box.h <= CELL_MAX_H)
    ]


def label_box(attrs: dict[str, str], body: str) -> Box | None:
    """Estimated rendered box of a <text>, in root user space."""
    try:
        x = float(attrs["x"])
        y = float(attrs["y"])
        size = float(attrs.get("font-size", "12"))
    except (KeyError, ValueError):
        return None
    text = plain(body)
    if not text:
        return None
    mono = "mono" in attrs.get("font-family", "").lower()
    width = size * estimated_advance(text, mono)
    anchor = attrs.get("text-anchor", "start")

    transform = attrs.get("transform", "")
    if transform:
        # Only a quarter turn about the label's own anchor is modelled; the
        # caller reports anything else rather than guessing at it.
        if not is_modelled_rotation(attrs):
            return None
        if anchor == "middle":
            top = y - width / 2
        elif anchor == "end":
            top = y
        else:
            top = y - width
        return Box(x - size * ASCENT, top, size, width)

    if anchor == "middle":
        left = x - width / 2
    elif anchor == "end":
        left = x - width
    else:
        left = x
    return Box(left, y - size * ASCENT, width, size)


def is_modelled_rotation(attrs: dict[str, str]) -> bool:
    """True when the label's transform is `rotate(±90 x y)` about its own anchor."""
    m = ROTATE_RE.match(attrs.get("transform", ""))
    if m is None:
        return False
    try:
        x, y = float(attrs["x"]), float(attrs["y"])
    except (KeyError, ValueError):
        return False
    return (
        abs(abs(float(m.group("deg"))) - 90.0) <= 0.01
        and abs(float(m.group("cx")) - x) <= 0.01
        and abs(float(m.group("cy")) - y) <= 0.01
    )


def host_of(cells: list[Box], x: float, y: float) -> int | None:
    """Index of the smallest cell containing the point, or None."""
    host_index = None
    for index, cell in enumerate(cells):
        if not cell.contains_point(x, y):
            continue
        if host_index is None or cell.area < cells[host_index].area:
            host_index = index
    return host_index


TRANSFORM_ADVICE = (
    "this checker validates raw cell and label coordinates, so a transform moves "
    "the rendered mark away from the share it was checked against. Bake the "
    "offset into the coordinates instead"
)


def check_style_rules(doc: _Scanner, findings: list[str], name: str) -> None:
    """A <style> rule that can transform or resize a cell is rejected outright.

    Nothing here resolves which marks a selector reaches, so a transform family
    declaration fails on any selector; `x`/`y`/`width`/`height` only when the
    selector could reach a rect (`rect`, `*`, or an attribute selector), so
    the usual `svg { width: 100% }` boilerplate stays legal.
    """
    for element in doc.styles:
        css = blank_css_comments(element.body)
        for rule in CSS_RULE_RE.finditer(css):
            selector = rule.group("selector")
            reaches_rect = re.search(r"\brect\b|\*|\[", selector) is not None
            pattern = CSS_CELL_GEOMETRY_RE if reaches_rect else CSS_TRANSFORM_RE
            found = pattern.search(rule.group("body"))
            if found is None:
                continue
            line = element.line + css.count("\n", 0, rule.start("body") + found.start())
            findings.append(
                f"{name}:{line}: a CSS `{found.group('prop').lower()}` declaration in "
                f"`{selector.strip()[:40]}` can transform or resize treemap cells — "
                + TRANSFORM_ADVICE
            )


def check_overlaps(cells: list[Box], findings: list[str], name: str) -> None:
    """Cells tile the plot: no two may share more than an edge.

    A cell laid over its neighbour keeps its own honest width x height, so
    every share still matches while the reader sees the neighbour painted
    over. EPSILON lets touching edges and sub-pixel snapping through.
    """
    for i, a in enumerate(cells):
        for b in cells[i + 1 :]:
            dx = min(a.right, b.right) - max(a.x, b.x)
            dy = min(a.bottom, b.bottom) - max(a.y, b.y)
            if dx > EPSILON and dy > EPSILON:
                findings.append(
                    f"{name}:{b.line}: cell {b.w:g}x{b.h:g} at ({b.x:g},{b.y:g}) overlaps "
                    f"cell {a.w:g}x{a.h:g} at ({a.x:g},{a.y:g}) by {dx:g}x{dy:g} — treemap "
                    "cells must tile without overlapping, or the covered area is drawn "
                    "twice and read once"
                )


def _number(match: re.Match[str] | None) -> float | None:
    if match is None:
        return None
    try:
        return float(match.group("num").replace(",", ""))
    except ValueError:
        return None


def parse_claim(text: str) -> tuple[float | None, float | None]:
    """(percentage, value) claimed by a label. Either may be absent."""
    percentage = _number(PCT_RE.search(text))
    value_match = VALUE_RE.search(text)
    value = _number(value_match)
    if value is not None and value_match is not None:
        value *= UNITS[value_match.group("unit")]
    return percentage, value


def check(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    findings: list[str] = []
    doc = _Scanner(source)
    if doc.error is not None:
        return [f"{path.name}: markup could not be parsed ({doc.error}) — failing closed"]
    parse_findings: list[str] = []
    cells = _cells_from(doc, parse_findings)
    findings.extend(f"{path.name}:{finding}" for finding in parse_findings)
    check_style_rules(doc, findings, path.name)
    if len(cells) < 3:
        findings.append(
            f"{path.name}: expected at least 3 parseable treemap cells, found {len(cells)} — "
            "the area contract cannot be verified"
        )
        return findings

    labels: dict[int, list[str]] = {index: [] for index in range(len(cells))}

    # No transform may move a cell: scale(0.5) quarters the drawn area while
    # x/y/width/height still match data-share. Rejected rather than resolved,
    # after verify-marimekko.py - a partial SVG transform stack looks like
    # coverage and is not.
    for cell in cells:
        for line, how in cell.transforms:
            findings.append(
                f"{path.name}:{line}: cell {cell.w:g}x{cell.h:g} carries {how} — "
                + TRANSFORM_ADVICE
            )
    check_overlaps(cells, findings, path.name)

    for element in doc.texts:
        attrs = element.attrs
        text = plain(element.body)
        try:
            anchor_x, anchor_y = float(attrs["x"]), float(attrs["y"])
        except (KeyError, ValueError):
            continue
        # Host is resolved from the text's ANCHOR, not its box corner. An
        # end- or middle-anchored label extends left of its anchor, so a box
        # corner can land outside the very cell the label belongs to - which
        # would silently attribute it to a neighbour, or to nothing at all.
        host_index = host_of(cells, anchor_x, anchor_y)
        if host_index is None:
            continue
        # A label may carry only the quarter turn label_box models; any other
        # transform attribute, a style transform, or a transformed ancestor
        # moves it away from the box measured below.
        how = element.ancestor
        if how is None and "transform" in attrs and not is_modelled_rotation(attrs):
            how = transform_carrier(attrs)
        if how is None:
            how = transform_carrier({k: v for k, v in attrs.items() if k != "transform"})
        if how is not None:
            findings.append(
                f"{path.name}:{element.line}: label {text[:38]!r} carries {how} — "
                + TRANSFORM_ADVICE
            )
            continue
        box = label_box(attrs, element.body)
        if box is None:
            continue
        host = cells[host_index]
        labels[host_index].append(text)

        # All four edges. Checking only right and bottom passes an end-anchored
        # label hanging off the left, or a label whose ascent clears the top -
        # both of which read as belonging to the neighbouring cell.
        overflow = {
            "left": host.x - box.x,
            "top": host.y - box.y,
            "right": box.right - host.right,
            "bottom": box.bottom - host.bottom,
        }
        breached = {side: over for side, over in overflow.items() if over > EPSILON}
        if breached:
            detail = " / ".join(f"{over:.1f} {side}" for side, over in breached.items())
            findings.append(
                f"{path.name}:{element.line}: label "
                f"{text[:38]!r} overflows its {host.w:g}x{host.h:g} cell "
                f"by {detail} — drop the label or merge the cell into a named Other"
            )

    # Information marks are 8–12px discs placed inside sliver cells. Their
    # one-letter text can fit while the surrounding disc still crosses a cell
    # boundary, so circles need their own containment check.
    for element in doc.circles:
        attrs = element.attrs
        try:
            cx = float(attrs["cx"])
            cy = float(attrs["cy"])
            radius = float(attrs["r"])
        except (KeyError, ValueError):
            continue
        if not 4.0 <= radius <= 6.0:
            continue
        host_index = host_of(cells, cx, cy)
        if host_index is None:
            continue
        how = transform_carrier(attrs) or element.ancestor
        if how is not None:
            findings.append(
                f"{path.name}:{element.line}: information marker carries {how} — "
                + TRANSFORM_ADVICE
            )
            continue
        host = cells[host_index]
        overflow = {
            "left": host.x - (cx - radius),
            "top": host.y - (cy - radius),
            "right": (cx + radius) - host.right,
            "bottom": (cy + radius) - host.bottom,
        }
        breached = {
            side: over for side, over in overflow.items() if over > MARKER_EPSILON
        }
        if breached:
            detail = " / ".join(f"{over:.1f} {side}" for side, over in breached.items())
            findings.append(
                f"{path.name}:{element.line}: information marker "
                f"overflows its {host.w:g}x{host.h:g} cell by {detail} — "
                "drop the marker and identify the sliver in the legend"
            )

    # Every cell is checked against its OWN declared share, so the cell most
    # likely to be wrong is not the one exempt from checking. Deriving the
    # basis from in-cell labels instead leaves an unlabelled sliver - exactly
    # the cell a 4px grid distorts most - verified by nothing at all.
    for cell in cells:
        for error in cell.share_errors:
            findings.append(
                f"{path.name}:{cell.line}: cell {cell.w:g}x{cell.h:g} "
                f"has invalid share metadata: {error}"
            )
        if cell.rect_count > 2:
            findings.append(
                f"{path.name}:{cell.line}: cell {cell.w:g}x{cell.h:g} "
                f"is painted by {cell.rect_count} duplicate rects — expected at most a mask/body pair"
            )

    undeclared = [cell for cell in cells if cell.share is None and not cell.share_errors]
    if undeclared:
        # Fail closed. A treemap whose cells cannot be parsed is not a passing
        # treemap; it is an unchecked one, and saying "OK" to it is the bug.
        for cell in undeclared:
            findings.append(
                f"{path.name}:{cell.line}: cell {cell.w:g}x{cell.h:g} "
                f"has no data-share, so its area is unverifiable — every treemap cell "
                f"must declare its share of the whole, including cells too small to label"
            )
        return findings

    drawn_total = sum(cell.area for cell in cells)
    if not math.isfinite(drawn_total) or drawn_total <= 0:
        findings.append(
            f"{path.name}: parsed cell area total must be finite and positive; got {drawn_total:g}"
        )
        return findings
    if any(cell.share_errors for cell in cells):
        return findings

    share_total = sum(cell.share for cell in cells if cell.share is not None)
    if not 99.0 <= share_total <= 101.0:
        findings.append(
            f"{path.name}: data-share values total {share_total:g}%, not approximately 100% — "
            "every cell must declare its share of the same whole"
        )
        return findings

    for index, cell in enumerate(cells):
        assert cell.share is not None
        area_share = cell.area / drawn_total * 100
        declared = cell.share / share_total * 100
        relative = (area_share - declared) / declared * 100
        if abs(relative) > AREA_TOLERANCE:
            findings.append(
                f"{path.name}:{cell.line}: cell {cell.w:g}x{cell.h:g} "
                f"draws {area_share:.2f}% of the area but declares {declared:.2f}% — "
                f"{relative:+.1f}% relative. Area is the only encoding; resize the cell"
            )

        # A label and the metadata are two statements of one fact. Read every
        # percentage in the hosted labels: search() alone silently accepted a
        # later contradictory claim when the first happened to be correct.
        label_text = " ".join(labels[index])
        percentages = [
            value
            for match in PCT_RE.finditer(label_text)
            if (value := _number(match)) is not None
        ]
        distinct_percentages: list[float] = []
        for percentage in percentages:
            if not any(
                math.isclose(percentage, known, rel_tol=0.0, abs_tol=1e-9)
                for known in distinct_percentages
            ):
                distinct_percentages.append(percentage)
        if len(distinct_percentages) > 1:
            rendered = ", ".join(f"{value:g}%" for value in distinct_percentages)
            findings.append(
                f"{path.name}:{cell.line}: cell {cell.w:g}x{cell.h:g} "
                f"has conflicting percentage claims ({rendered}) — every hosted label "
                "must state the same share"
            )
            continue

        if distinct_percentages and declared > 0:
            percentage = distinct_percentages[0]
            drift = abs(percentage - declared)
            # 1pp covers honest rounding of a displayed integer percentage.
            if drift > 1.0:
                findings.append(
                    f"{path.name}:{cell.line}: cell {cell.w:g}x{cell.h:g} "
                    f"is labelled {percentage:g}% but declares {declared:.2f}% — "
                    f"the label and data-share must state the same fact"
                )
    return findings


def targets(args: argparse.Namespace) -> list[Path]:
    if args.all:
        return sorted(ASSET_DIR.glob("example-treemap*.html"))
    return [Path(p) for p in args.paths]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify treemap area fidelity and label/marker fit."
    )
    parser.add_argument("paths", nargs="*", help="HTML files to check")
    parser.add_argument("--all", action="store_true", help="check every shipped treemap example")
    args = parser.parse_args()
    if not args.all and not args.paths:
        parser.print_help()
        return 2

    findings: list[str] = []
    checked = 0
    for path in targets(args):
        if not path.exists():
            print(f"error: {path} does not exist", file=sys.stderr)
            return 2
        findings.extend(check(path))
        checked += 1

    for finding in findings:
        print(finding)
    if findings:
        print(f"\n{len(findings)} treemap finding(s) across {checked} file(s).")
        return 1
    print(
        f"OK treemap: {checked} file(s), area matches labels, cells tile untransformed "
        "and every label/marker fits its cell"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
