"""Enable Banking key uploaded on the Banks page instead of copied into the data folder by hand."""
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app import enablebanking as eb

APP_ID = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


@pytest.fixture(scope="module")
def pem():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def test_save_key_names_the_file_after_the_application(tmp_path, pem):
    assert eb.save_key(str(tmp_path), pem, APP_ID, "whatever.pem") == APP_ID
    assert (tmp_path / f"{APP_ID}.pem").read_bytes() == pem
    assert eb.load_credentials(str(tmp_path), env={}) == (APP_ID, str(tmp_path / f"{APP_ID}.pem"))
    if os.name != "nt":
        assert (tmp_path / f"{APP_ID}.pem").stat().st_mode & 0o077 == 0


def test_application_id_comes_from_the_file_name_when_left_empty(tmp_path, pem):
    assert eb.save_key(str(tmp_path), pem, "", f"{APP_ID}.pem") == APP_ID


def test_replacing_the_key_keeps_the_old_one_aside(tmp_path, pem):
    eb.save_key(str(tmp_path), pem, OTHER, "")
    eb.save_key(str(tmp_path), pem, APP_ID, "")
    assert eb.load_credentials(str(tmp_path), env={})[0] == APP_ID
    assert (tmp_path / f"{OTHER}.pem.old").exists()


@pytest.mark.parametrize("data,app_id,msg", [
    (b"hello", APP_ID, "private key"),
    (None, "not-a-uuid", "application ID"),
    (None, "", "application ID"),
    (b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n", APP_ID, "private key"),
])
def test_bad_uploads_are_refused_and_nothing_is_written(tmp_path, pem, data, app_id, msg):
    with pytest.raises(ValueError, match=msg):
        eb.save_key(str(tmp_path), pem if data is None else data, app_id, "key.pem")
    assert list(tmp_path.iterdir()) == []


def test_non_rsa_key_is_refused(tmp_path):
    k = ec.generate_private_key(ec.SECP256R1())
    data = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    with pytest.raises(ValueError, match="RSA"):
        eb.save_key(str(tmp_path), data, APP_ID, "")


def test_banks_page_upload(client, data_dir, pem):
    page = client.get("/banks").text
    assert 'action="/banks/key"' in page and 'type="file"' in page
    r = client.post("/banks/key", data={"app_id": APP_ID}, files={"key": ("k.pem", b"nope", "text/plain")})
    assert r.status_code == 400 and "private key" in r.text
    r = client.post("/banks/key", data={"app_id": APP_ID}, files={"key": ("k.pem", pem, "application/x-pem-file")},
                    follow_redirects=False)                       # no redirect follow: the page would call the bank API
    assert r.status_code == 303
    assert (data_dir / f"{APP_ID}.pem").exists()
