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


def test_donut_slices_follow_shares_in_given_order():
    d = charts.donut([("food", 75.0), ("home", 25.0)], size=200, thickness=24)
    assert [s["key"] for s in d["slices"]] == ["food", "home"]
    assert d["slices"][0]["path"].startswith("M") and abs(d["slices"][0]["share"] - 0.75) < 1e-9
    assert d["slices"][0]["large"] == 1 and d["slices"][1]["large"] == 0


def test_donut_single_slice_is_a_full_ring():
    d = charts.donut([("food", 10.0)], size=200, thickness=24)
    assert d["slices"][0]["full"] is True


def test_donut_empty():
    assert charts.donut([], size=200, thickness=24)["slices"] == []


def test_stacked_bars_scale_to_the_tallest_month():
    b = charts.stacked_bars([("2026-10", 100.0, 50.0), ("2026-11", 20.0, 0.0)], width=200, height=100, pad=0)
    first, second = b["bars"]
    assert first["top_y"] == 0 and round(first["h1"] + first["h2"], 1) == 100
    assert second["h2"] == 0 and second["x"] > first["x"]


def test_mini_bars_handle_all_zero():
    b = charts.bars([0.0, 0.0], width=100, height=40)
    assert all(r["h"] == 0 for r in b["bars"])
