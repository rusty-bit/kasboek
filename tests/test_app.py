from app import db


def test_overview_empty_state(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Kasboek" in r.text
    assert "Import your first statement" in r.text


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_startup_seeds_categories_in_data_dir(client, data_dir):
    assert (data_dir / "kasboek.db").exists()
    with db.db() as con:
        assert "groceries" in db.cat_ids(con)


def test_htmx_is_vendored_not_cdn(client):
    r = client.get("/")
    assert "/static/htmx.min.js" in r.text
    assert "unpkg" not in r.text and "cdn" not in r.text.lower()
    assert client.get("/static/htmx.min.js").status_code == 200
