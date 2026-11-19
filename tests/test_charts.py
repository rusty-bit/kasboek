from datetime import date, timedelta

from app import charts


def test_line_geometry_scales_into_the_box_and_marks_lowest_point():
    start = date(2026, 11, 19)
    series = [(start + timedelta(days=i), v) for i, v in enumerate([100, 50, -20, 80])]
    g = charts.line(series, threshold=0, width=300, height=100, pad=10)
    pts = [tuple(map(float, p.split(","))) for p in g["points"].split()]
    assert len(pts) == 4 and pts[0][0] == 10 and pts[-1][0] == 290
    assert min(p[1] for p in pts) == 10 and max(p[1] for p in pts) == 90     # highest value on top
    assert g["low"]["value"] == -20 and g["low"]["x"] == pts[2][0]
    assert 10 < g["threshold_y"] < 90


def test_flat_series_does_not_divide_by_zero():
    g = charts.line([(date(2026, 1, 1), 5.0), (date(2026, 1, 2), 5.0)], threshold=None)
    assert g["threshold_y"] is None and len(g["points"].split()) == 2
