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
