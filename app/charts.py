"""Geometry for the small server-drawn SVG charts (no chart library, no JS build). Pure functions."""


def line(series, threshold=None, width=640, height=180, pad=12):
    """series: [(date, value)]. Returns SVG points, the y of the threshold line and the lowest point,
    in a width x height box with `pad` inside. Higher values are drawn higher up."""
    values = [v for _, v in series]
    lo = min(values + ([threshold] if threshold is not None else []))
    hi = max(values + ([threshold] if threshold is not None else []))
    span = (hi - lo) or 1.0
    n = max(len(series) - 1, 1)

    def x(i):
        return round(pad + (width - 2 * pad) * i / n, 1)

    def y(v):
        return round(pad + (height - 2 * pad) * (hi - v) / span, 1)

    pts = [(x(i), y(v)) for i, (_, v) in enumerate(series)]
    i_low = min(range(len(series)), key=lambda i: (values[i], i))
    return {
        "width": width, "height": height, "pad": pad,
        "points": " ".join(f"{px},{py}" for px, py in pts),
        "coords": [{"x": px, "y": py, "date": d.isoformat(), "value": v} for (px, py), (d, v) in zip(pts, series)],
        "threshold_y": y(threshold) if threshold is not None else None,
        "low": {"x": pts[i_low][0], "y": pts[i_low][1], "value": values[i_low], "date": series[i_low][0]},
        "hi": hi, "lo": lo, "y_hi": y(hi), "y_lo": y(lo),
    }


def _polar(cx, cy, r, angle):
    import math
    return cx + r * math.sin(angle), cy - r * math.cos(angle)


def donut(items, size=200, thickness=24, gap_deg=1.6):
    """items: [(key, value)] in drawing order (keep it fixed: colours follow the item, and neighbours stay
    the validated neighbour pairs). Returns ring-slice SVG paths, clockwise from 12 o'clock, with a small
    surface gap between slices."""
    import math
    total = sum(v for _, v in items if v > 0)
    c, r_out = size / 2, size / 2
    r_in = r_out - thickness
    out, angle = [], 0.0
    items = [(k, v) for k, v in items if v > 0]
    for key, v in items:
        share = v / total
        sweep = share * 2 * math.pi
        if len(items) == 1:
            out.append({"key": key, "share": 1.0, "full": True, "large": 1, "path": "", "r_mid": r_out - thickness / 2})
            break
        gap = math.radians(gap_deg) / 2 if sweep > math.radians(gap_deg * 2) else 0
        a0, a1 = angle + gap, angle + sweep - gap
        large = 1 if (a1 - a0) > math.pi else 0
        x0, y0 = _polar(c, c, r_out, a0)
        x1, y1 = _polar(c, c, r_out, a1)
        x2, y2 = _polar(c, c, r_in, a1)
        x3, y3 = _polar(c, c, r_in, a0)
        path = (f"M{x0:.2f},{y0:.2f} A{r_out},{r_out} 0 {large} 1 {x1:.2f},{y1:.2f} "
                f"L{x2:.2f},{y2:.2f} A{r_in},{r_in} 0 {large} 0 {x3:.2f},{y3:.2f} Z")
        out.append({"key": key, "share": share, "full": False, "large": large, "path": path})
        angle += sweep
    return {"size": size, "thickness": thickness, "c": c, "r_mid": r_out - thickness / 2, "slices": out}


def round_top(x, y, w, h, r=4):
    """A bar as a path with only its top corners rounded: the rounded end is the data end."""
    if h <= 0 or w <= 0:
        return ""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + w - r:.1f} "
            f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def stacked_bars(items, width=640, height=160, pad=4, gap=0.3):
    """items: [(label, lower, upper)] -> bars with the lower segment at the baseline. Heights scale to the
    tallest total; gap is the share of each slot left empty between bars."""
    n = max(len(items), 1)
    top = max((a + b for _, a, b in items), default=0) or 1.0
    slot = (width - 2 * pad) / n
    w = slot * (1 - gap)
    usable = height - 2 * pad
    bars = []
    for i, (label, a, b) in enumerate(items):
        h1, h2 = usable * max(a, 0) / top, usable * max(b, 0) / top
        x = pad + i * slot + (slot - w) / 2
        base = height - pad
        y1, y2 = base - h1, base - h1 - h2
        lower_path = round_top(x, y1, w, h1) if h2 <= 0 else (f"M{x:.1f},{base:.1f} V{y1:.1f} H{x + w:.1f} V{base:.1f} Z"
                                                              if h1 > 0 else "")
        bars.append({"label": label, "x": round(x, 1), "w": round(w, 1), "h1": round(h1, 1), "h2": round(h2, 1),
                     "y1": round(y1, 1), "y2": round(y2, 1), "top_y": round(y2, 1),
                     "lower_path": lower_path, "upper_path": round_top(x, y2, w, h2 - 1.5) if h2 > 1.5 else "",
                     "lower": a, "upper": b, "cx": round(x + w / 2, 1)})
    return {"width": width, "height": height, "bars": bars, "base": height - pad, "max": top}


def bars(values, width=120, height=36, gap=0.25):
    """Small single-series bars, scaled to the largest value."""
    n = max(len(values), 1)
    top = max(values, default=0) or 1.0
    slot = width / n
    w = slot * (1 - gap)
    out = []
    for i, v in enumerate(values):
        h = round(height * max(v, 0) / top, 1)
        x = i * slot + (slot - w) / 2
        out.append({"x": round(x, 1), "w": round(w, 1), "h": h, "y": round(height - h, 1), "v": v,
                    "path": round_top(x, height - h, w, h, r=2.5)})
    return {"width": width, "height": height, "bars": out}
