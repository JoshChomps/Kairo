"""Draw curves as an SVG picture, with no plotting library.

One chart: curves against a shared x axis, each on the same y axis, with
a grid, tick labels in engineering units and a legend. Enough to see a
start-up, an overshoot or a filter's roll-off; not a plotting package.
"""

from __future__ import annotations

import math
from xml.sax.saxutils import escape

Curve = tuple[tuple[float, ...], tuple[float, ...]]

#: The picture's size and margins, in SVG units.
WIDTH, HEIGHT = 900, 420
LEFT, RIGHT, TOP, BOTTOM = 70, 20, 40, 50
#: The most points drawn per curve; longer curves are thinned.
MAX_POINTS = 1200
#: Line colours, chosen to stay apart for the common kinds of colour
#: blindness (Okabe and Ito's palette).
COLOURS = ("#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000")
_PREFIXES = {-12: "p", -9: "n", -6: "u", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}


def engineering(value: float, unit: str) -> str:
    """A number with an SI prefix, for example ``220 us`` or ``1.59 kHz``.

    Args:
        value: The number.
        unit: Its unit; may be empty.

    Returns:
        The text, to four significant figures.
    """
    if value == 0 or not math.isfinite(value):
        return f"{value:g} {unit}".strip()
    exponent = int(math.floor(math.log10(abs(value)) / 3) * 3)
    exponent = max(-12, min(9, exponent))
    return f"{value / 10**exponent:.4g} {_PREFIXES[exponent]}{unit}".strip()


def thin(curve: Curve, limit: int = MAX_POINTS) -> Curve:
    """Reduce a curve's points without losing its peaks.

    The curve is cut into equal runs of samples and the lowest and
    highest of each run are kept, in order, so a narrow spike survives.

    Args:
        curve: The x and y values.
        limit: The most points to keep.

    Returns:
        The thinned curve; the curve itself if it is already short.
    """
    x, y = curve
    if len(x) <= limit:
        return curve
    size = math.ceil(len(x) / (limit // 2))
    keep: list[int] = []
    for start in range(0, len(x), size):
        run = range(start, min(start + size, len(x)))
        low = min(run, key=lambda i: y[i])
        high = max(run, key=lambda i: y[i])
        keep.extend(sorted({low, high}))
    if keep[-1] != len(x) - 1:
        keep.append(len(x) - 1)
    return tuple(x[i] for i in keep), tuple(y[i] for i in keep)


def _ticks(low: float, high: float, count: int = 6) -> list[float]:
    """Round tick values covering a range: steps of 1, 2 or 5 times a power of ten."""
    if high <= low:
        return [low]
    raw = (high - low) / count
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 5, 10) if m * power >= raw)
    first = math.ceil(low / step - 1e-9) * step
    ticks: list[float] = []
    value = first
    while value <= high + step * 1e-9:
        ticks.append(0.0 if abs(value) < step * 1e-9 else value)
        value += step
    return ticks


def line_chart(
    curves: dict[str, Curve],
    *,
    title: str,
    x_unit: str,
    y_unit: str,
    log_x: bool = False,
) -> str:
    """Draw curves on one pair of axes.

    Args:
        curves: Each curve's x and y values, by its legend label.
        title: The chart's title.
        x_unit: The unit of x, for the tick labels.
        y_unit: The unit of y.
        log_x: Whether x is drawn on a logarithmic scale, as for
            frequency.

    Returns:
        The SVG document as text.

    Raises:
        ValueError: If there is nothing to draw, or a log axis is asked
            for values that are not all above zero.
    """
    drawn = {label: thin(curve) for label, curve in curves.items() if len(curve[0]) > 1}
    if not drawn:
        raise ValueError("a chart needs at least one curve of two points")
    xs = [v for x, _ in drawn.values() for v in x]
    ys = [v for _, y in drawn.values() for v in y]
    if log_x and min(xs) <= 0:
        raise ValueError("a logarithmic axis needs every x above zero")

    def scale_x(value: float) -> float:
        return math.log10(value) if log_x else value

    x_low, x_high = scale_x(min(xs)), scale_x(max(xs))
    y_low, y_high = min(ys), max(ys)
    if y_high == y_low:
        y_low, y_high = y_low - 1.0, y_high + 1.0
    margin = 0.05 * (y_high - y_low)
    y_low, y_high = y_low - margin, y_high + margin
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM

    def px(value: float) -> float:
        return LEFT + (scale_x(value) - x_low) / ((x_high - x_low) or 1.0) * plot_w

    def py(value: float) -> float:
        return TOP + (y_high - value) / (y_high - y_low) * plot_h

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'font-family="sans-serif" font-size="12">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="white"/>',
        f'<text x="{LEFT}" y="22" font-size="15" font-weight="bold">'
        f"{escape(title)}</text>",
    ]
    for tick in _ticks(y_low, y_high):
        y = py(tick)
        out.append(
            f'<line x1="{LEFT}" y1="{y:.1f}" x2="{WIDTH - RIGHT}" y2="{y:.1f}" '
            'stroke="#dddddd"/>'
        )
        out.append(
            f'<text x="{LEFT - 6}" y="{y + 4:.1f}" text-anchor="end">'
            f"{escape(engineering(tick, y_unit))}</text>"
        )
    if log_x:
        x_ticks = [
            10.0**e
            for e in range(math.ceil(x_low - 1e-9), math.floor(x_high + 1e-9) + 1)
        ]
    else:
        x_ticks = _ticks(x_low, x_high, 8)
    for tick in x_ticks:
        x = px(tick)
        out.append(
            f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{HEIGHT - BOTTOM}" '
            'stroke="#dddddd"/>'
        )
        out.append(
            f'<text x="{x:.1f}" y="{HEIGHT - BOTTOM + 16}" text-anchor="middle">'
            f"{escape(engineering(tick, x_unit))}</text>"
        )
    out.append(
        f'<rect x="{LEFT}" y="{TOP}" width="{plot_w}" height="{plot_h}" '
        'fill="none" stroke="#333333"/>'
    )
    for index, (label, (x, y)) in enumerate(drawn.items()):
        colour = COLOURS[index % len(COLOURS)]
        points = " ".join(f"{px(a):.1f},{py(b):.1f}" for a, b in zip(x, y, strict=True))
        out.append(
            f'<polyline points="{points}" fill="none" stroke="{colour}" '
            'stroke-width="1.6"/>'
        )
        legend_x = LEFT + 10 + (index % 4) * 205
        legend_y = HEIGHT - 12 - (index // 4) * 14
        out.append(
            f'<line x1="{legend_x}" y1="{legend_y - 4}" x2="{legend_x + 18}" '
            f'y2="{legend_y - 4}" stroke="{colour}" stroke-width="3"/>'
        )
        out.append(f'<text x="{legend_x + 24}" y="{legend_y}">{escape(label)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"
