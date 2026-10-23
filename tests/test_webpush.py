"""Web Push: alerts on a phone through Cabin installed on its home screen.

The crypto is checked against the worked example in RFC 8291 and by
decrypting what is sent, the signature against its own public key, and the
rest is about who may do what: a phone belongs to the user who turned it on,
a push may only go to a browser's push service, and nothing in a test reaches
one (``conftest.push_services_never_reached``).
"""

import hashlib
import json
import os
import re
import stat
import struct
import threading
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

import auth
import notifications
import server
import webpush

ROOT = Path(__file__).resolve().parent.parent
CABIN_DIR = ROOT / "app" / "static" / "ui" / "cabin"
CABIN = (CABIN_DIR / "cabin.js").read_text(encoding="utf-8")
SW = (CABIN_DIR / "sw.js").read_text(encoding="utf-8")
CORE = (ROOT / "app" / "static" / "ui" / "shared" / "core.js").read_text(encoding="utf-8")
SESSION = "pytest-fixed-session-id"
APPLE = "https://web.push.apple.com/QGuQyavXutnMH_kNbfmk"


# -- A phone, as far as the server can tell ----------------------------------


class Phone:
    """A browser's half of a subscription: its key pair and auth secret."""

    def __init__(self, endpoint=APPLE):
        self.endpoint = endpoint
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.public = self.private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
        self.auth = os.urandom(16)

    def subscription(self):
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": webpush.b64url_encode(self.public),
                     "auth": webpush.b64url_encode(self.auth)},
        }

    def decrypt(self, body: bytes) -> bytes:
        """RFC 8291 from the receiving side, written independently of encrypt()."""
        salt, (rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
        as_public = body[21:21 + idlen]
        ciphertext = body[21 + idlen:]
        assert rs == 4096 and idlen == 65 and len(ciphertext) <= rs
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
        shared = self.private.exchange(ec.ECDH(), peer)

        def hkdf(salt_, ikm, info, length):
            import hmac
            prk = hmac.new(salt_, ikm, hashlib.sha256).digest()
            return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]

        ikm = hkdf(self.auth, shared, b"WebPush: info\x00" + self.public + as_public, 32)
        cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        padded = AESGCM(cek).decrypt(nonce, ciphertext, None)
        assert padded.endswith(b"\x02")
        return padded[:-1]


class Recorder:
    """Stands in for the push service."""

    def __init__(self, status=201, answer=b""):
        self.status, self.answer, self.calls = status, answer, []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, dict(headers)))
        return self.status, self.answer


@pytest.fixture
def push_service(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(webpush, "http_post", recorder)
    return recorder


@pytest.fixture
def users(tmp_path, monkeypatch):
    """A private user store holding the test session's admin, and one user."""
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "users.json")
    auth.init_user_store()
    users = auth.load_users()
    users["kari"] = {"password_hash": auth.hash_password("x" * 8), "role": "user"}
    auth.save_users(users)
    yield
    for sid in [s for s in auth.sessions if s != SESSION]:
        auth.sessions.pop(sid, None)


@pytest.fixture
def client(users):
    with TestClient(server.app) as c:
        c.cookies.set("session_id", SESSION)
        yield c


def _session_for(username):
    sid = f"session-{username}-push"
    auth.sessions[sid] = {"username": username, "created": time.time()}
    return sid


# -- RFC 8291: message encryption --------------------------------------------


def test_encryption_matches_the_worked_example_in_rfc_8291():
    d = webpush.b64url_decode
    as_private = ec.derive_private_key(
        int.from_bytes(d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1(),
    )
    out = webpush.encrypt(
        d("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
        d("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
        d("BTBZMqHH6r4Tts7J_aSIgg"),
        salt=d("DGv6ra1nlYgDCS1FRnbzlw"),
        as_private=as_private,
    )
    # The RFC gives the header and the ciphertext as two separate encodings.
    header = d("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")
    ciphertext = d("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ")
    assert out == header + ciphertext
    assert len(out) == 144


def test_each_message_gets_its_own_key_and_salt():
    phone = Phone()
    a = webpush.encrypt(b"same", phone.public, phone.auth)
    b = webpush.encrypt(b"same", phone.public, phone.auth)
    assert a[:16] != b[:16] and a[21:86] != b[21:86]
    assert phone.decrypt(a) == phone.decrypt(b) == b"same"


# -- RFC 8292: VAPID ---------------------------------------------------------


def test_the_vapid_token_is_signed_by_the_key_it_names():
    key = webpush.load_or_create_key()
    header = webpush.vapid_authorization(APPLE + "?x=1", key, now=1_000_000)
    m = re.fullmatch(r"vapid t=([^,]+),k=(.+)", header)
    token, k = m.groups()
    assert k == webpush.public_key()

    head, claims, sig = token.split(".")
    assert json.loads(webpush.b64url_decode(head)) == {"typ": "JWT", "alg": "ES256"}
    body = json.loads(webpush.b64url_decode(claims))
    # The audience is the push service's origin, never the path or query.
    assert body == {"aud": "https://web.push.apple.com",
                    "exp": 1_000_000 + webpush.VAPID_LIFETIME_SECONDS,
                    "sub": webpush.VAPID_SUBJECT}
    # At most 24 hours ahead, which is the limit RFC 8292 sets.
    assert webpush.VAPID_LIFETIME_SECONDS <= 24 * 3600

    raw = webpush.b64url_decode(sig)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.b64url_decode(k))
    public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_the_key_is_made_once_and_kept_private(tmp_path):
    first = webpush.public_key()
    assert webpush.public_key() == first
    folder = tmp_path / "webpush"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "vapid_private.pem").stat().st_mode) == 0o600


def test_a_damaged_key_is_never_silently_replaced(tmp_path):
    folder = tmp_path / "webpush"
    folder.mkdir()
    (folder / "vapid_private.pem").write_bytes(
        ec.generate_private_key(ec.SECP384R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    with pytest.raises(RuntimeError):
        webpush.load_or_create_key()


# -- Where a push may go -----------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://web.push.apple.com/abc",
    "https://api.push.apple.com/abc",
    "https://fcm.googleapis.com/fcm/send/abc",
    "https://updates.push.services.mozilla.com/wpush/v2/abc",
    "https://wns2-db5p.notify.windows.com/w/?token=abc",
    "https://web.push.apple.com:443/abc",
])
def test_browser_push_services_are_allowed(url):
    webpush.check_allowed(url)


@pytest.mark.parametrize("url", [
    "http://web.push.apple.com/abc",
    "https://user:pw@web.push.apple.com/abc",
    "https://web.push.apple.com:8443/abc",
    "https://web.push.apple.com:bad/abc",
    "https://10.81.0.1/abc",
    "https://localhost/abc",
    "https://push.apple.com.evil.example/abc",
    "https://evilpush.apple.com.example/abc",
    "https://notpush.apple.com/abc",
    "https://example.com/web.push.apple.com",
    "file:///etc/passwd",
    "",
])
def test_anything_else_is_refused(url):
    with pytest.raises(webpush.ForbiddenEndpoint):
        webpush.check_allowed(url)


def test_the_real_transport_checks_the_address_before_connecting(real_webpush_http_post):
    with pytest.raises(webpush.ForbiddenEndpoint):
        real_webpush_http_post("https://10.81.0.1/x", b"", {})


def test_a_redirect_is_never_followed():
    handler = webpush._NoRedirect()
    with pytest.raises(webpush.ForbiddenEndpoint):
        handler.redirect_request(None, None, 301, "Moved", {}, "https://evil.example/")


@pytest.mark.parametrize("change", [
    {"endpoint": "https://evil.example/x"},
    {"endpoint": APPLE + "x" * 1100},
    {"endpoint": None},
    {"p256dh": "not base64 !!"},
    {"p256dh": webpush.b64url_encode(b"\x04" + b"\x00" * 64)},  # not on the curve
    {"p256dh": webpush.b64url_encode(b"\x02" + b"\x01" * 32)},  # compressed
    {"auth": webpush.b64url_encode(b"short")},
    {"auth": None},
])
def test_a_bad_subscription_is_refused(change):
    phone = Phone()
    fields = {"endpoint": phone.endpoint,
              "p256dh": webpush.b64url_encode(phone.public),
              "auth": webpush.b64url_encode(phone.auth), **change}
    with pytest.raises(webpush.InvalidSubscription):
        webpush.validate_subscription(fields["endpoint"], fields["p256dh"], fields["auth"])


# -- Storage -----------------------------------------------------------------


def _add(phone, username="admin", label="iPhone"):
    s = phone.subscription()
    return webpush.add_subscription(username, s["endpoint"], s["keys"]["p256dh"],
                                    s["keys"]["auth"], label)


def test_the_store_is_private_and_the_interface_never_sees_the_address(tmp_path):
    sub = _add(Phone())
    path = tmp_path / "webpush" / "subscriptions.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    shown = webpush.public_device(sub)
    assert APPLE not in json.dumps(shown)
    assert sub["p256dh"] not in json.dumps(shown) and sub["auth"] not in json.dumps(shown)
    assert shown["endpoint_hash"] == hashlib.sha256(APPLE.encode()).hexdigest()


def test_the_same_browser_is_one_device_and_belongs_to_whoever_signed_in_last():
    phone = Phone()
    first = _add(phone, "admin")
    again = _add(phone, "kari")
    assert first["id"] == again["id"]
    assert [s["username"] for s in webpush.subscriptions()] == ["kari"]


def test_there_is_a_limit(monkeypatch):
    monkeypatch.setattr(webpush, "MAX_SUBSCRIPTIONS", 2)
    _add(Phone(APPLE + "1"))
    _add(Phone(APPLE + "2"))
    with pytest.raises(webpush.InvalidSubscription):
        _add(Phone(APPLE + "3"))


def test_a_damaged_store_reads_as_no_devices(tmp_path):
    (tmp_path / "webpush").mkdir()
    (tmp_path / "webpush" / "subscriptions.json").write_text("{not json")
    assert webpush.subscriptions() == []


def test_deleting_and_renaming_a_user_follow_their_devices():
    _add(Phone(APPLE + "a"), "kari")
    _add(Phone(APPLE + "b"), "admin")
    webpush.rename_user("kari", "kari2")
    assert sorted(s["username"] for s in webpush.subscriptions()) == ["admin", "kari2"]
    assert webpush.remove_user("kari2") == 1
    assert [s["username"] for s in webpush.subscriptions()] == ["admin"]


# -- Sending -----------------------------------------------------------------


def test_an_alert_arrives_readable_with_the_right_headers(push_service):
    phone = Phone()
    _add(phone)
    counts = webpush.deliver_all("⚠ Frost", "Room: Kitchen\nIt is 4 °C.", "critical", "frost:3")
    assert counts == {"sent": 1, "gone": 0, "failed": 0}

    url, body, headers = push_service.calls[0]
    assert url == APPLE
    assert json.loads(phone.decrypt(body)) == {
        "title": "⚠ Frost", "body": "Room: Kitchen\nIt is 4 °C.",
        "severity": "critical", "url": "/", "tag": "frost:3",
    }
    assert headers["Content-Encoding"] == "aes128gcm"
    assert headers["Urgency"] == "high"
    assert headers["TTL"] == str(24 * 3600)
    assert headers["Topic"] == webpush.topic_for("frost:3")
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,32}", headers["Topic"])
    assert headers["Authorization"].startswith("vapid t=")
    assert webpush.subscriptions()[0]["last_success"] is not None


@pytest.mark.parametrize("severity,urgency", [("info", "low"), ("warning", "normal")])
def test_urgency_follows_severity(push_service, severity, urgency):
    _add(Phone())
    webpush.deliver_all("t", "b", severity, None)
    headers = push_service.calls[0][2]
    assert headers["Urgency"] == urgency
    assert "Topic" not in headers


def test_a_long_alert_still_fits():
    payload = webpush.build_payload("t", "ø" * 5000, "info", "x" * 200)
    assert len(payload) <= webpush.MAX_PAYLOAD_BYTES
    assert len(json.loads(payload)["tag"]) == 64


@pytest.mark.parametrize("status", [404, 410])
def test_a_phone_that_has_gone_is_forgotten(push_service, status):
    push_service.status = status
    _add(Phone())
    assert webpush.deliver_all("t", "b")["gone"] == 1
    assert webpush.subscriptions() == []


def test_a_refusal_is_recorded_and_the_device_kept(push_service):
    push_service.status, push_service.answer = 403, b"BadJwtToken"
    _add(Phone())
    assert webpush.deliver_all("t", "b")["failed"] == 1
    [sub] = webpush.subscriptions()
    assert sub["last_error"]["detail"] == "403 BadJwtToken"


def test_apples_json_refusal_is_reduced_to_its_reason(push_service):
    push_service.status, push_service.answer = 400, b'{"reason":"BadWebPushToken"}'
    _add(Phone())
    webpush.deliver_all("t", "b")
    assert webpush.subscriptions()[0]["last_error"]["detail"] == "400 BadWebPushToken"


def test_no_answer_at_all_is_a_failure_not_a_crash(monkeypatch):
    def down(url, body, headers):
        raise OSError("timed out")

    monkeypatch.setattr(webpush, "http_post", down)
    _add(Phone())
    assert webpush.deliver_all("t", "b")["failed"] == 1


def test_one_bad_phone_does_not_stop_the_others(monkeypatch):
    good = Recorder()

    def transport(url, body, headers):
        if url.endswith("bad"):
            raise OSError("refused")
        return good(url, body, headers)

    monkeypatch.setattr(webpush, "http_post", transport)
    _add(Phone(APPLE + "bad"))
    _add(Phone(APPLE + "good"))
    assert webpush.deliver_all("t", "b") == {"sent": 1, "gone": 0, "failed": 1}


def test_no_phones_means_no_key_is_even_made(tmp_path):
    assert webpush.deliver_all("t", "b") == {"sent": 0, "gone": 0, "failed": 0}
    assert not (tmp_path / "webpush" / "vapid_private.pem").exists()


# -- The notifier: phones beside email ---------------------------------------


def _notifier(email=False):
    n = notifications.Notifier()
    n.settings = notifications.load_settings()
    n.settings["enabled"] = True
    n.settings["events"]["hub_offline"] = True
    n.settings["min_minutes_between"] = 0
    if email:
        n.settings["email"].update(host="smtp.example.invalid", from_addr="pi@example.invalid",
                                   to_addrs=["me@example.invalid"])
    done = threading.Event()
    n.pushed, n.mailed = [], []
    n.push_impl = lambda *a: (n.pushed.append(a), done.set())
    n.send_impl = lambda cfg, subject, body, html=None: n.mailed.append(subject)
    n.done = done
    return n


def test_phones_alone_receive_alerts_and_no_email_is_attempted():
    n = _notifier(email=False)
    n.notify("hub_offline", "Hub offline", "Prose.", severity="critical",
             facts=(("Since", "17:42"),))
    assert n.done.wait(5)
    assert n.mailed == []
    [(title, body, severity, tag)] = n.pushed
    assert title == "⚠ Hub offline"
    assert body == "Since: 17:42\nProse."
    assert severity == "critical" and tag is None


def test_both_channels_when_both_are_set():
    n = _notifier(email=True)
    n.notify("hub_offline", "Hub offline", "Prose.")
    assert n.done.wait(5)
    assert len(n.mailed) == 1 and len(n.pushed) == 1


def test_a_condition_carries_its_key_so_the_recovery_replaces_the_alert():
    n = _notifier()
    n.set_condition("hub_offline", "hub", True, "Hub offline", "Down.")
    assert n.done.wait(5)
    assert n.pushed[-1][3] == "hub"


def test_a_failing_phone_does_not_stop_the_email():
    n = _notifier(email=True)
    sent = threading.Event()

    def broken(*a):
        raise RuntimeError("push service down")

    n.push_impl = broken
    n.send_impl = lambda *a, **k: sent.set()
    n.notify("hub_offline", "Hub offline", "Prose.")
    assert sent.wait(5)


def test_quiet_hours_and_disabled_events_hold_back_phones_too(monkeypatch):
    n = _notifier()
    n.settings["events"]["hub_offline"] = False
    n.notify("hub_offline", "Hub offline", "Prose.")
    n.settings["events"]["hub_offline"] = True
    monkeypatch.setattr(n, "_quiet_now", lambda now=None: True)
    n.notify("hub_offline", "Hub offline", "Prose.", severity="warning")
    assert not n.done.wait(0.3)
    assert n.pushed == []


def test_the_server_notifier_sends_to_phones():
    assert server.notifier.push_impl is webpush.deliver_all


# -- The API -----------------------------------------------------------------


def test_every_push_route_needs_a_session(users):
    with TestClient(server.app) as anon:
        anon.cookies.clear()
        for method, path in [("get", "/api/push"), ("post", "/api/push/subscriptions"),
                             ("delete", "/api/push/subscriptions/x"),
                             ("post", "/api/push/subscriptions/x/test")]:
            assert getattr(anon, method)(path, follow_redirects=False).status_code == 401, path


def test_turning_a_phone_on(client):
    phone = Phone()
    r = client.post("/api/push/subscriptions", json=phone.subscription(),
                    headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"})
    assert r.status_code == 200
    device = r.json()
    assert device["label"] == "iPhone" and device["username"] == "admin"
    assert "endpoint" not in device

    got = client.get("/api/push").json()
    assert got["public_key"] == webpush.public_key()
    assert [d["id"] for d in got["devices"]] == [device["id"]]
    assert got["is_admin"] is True


@pytest.mark.parametrize("body", [
    {}, [], {"endpoint": APPLE}, {"endpoint": APPLE, "keys": "x"},
    {"endpoint": "https://evil.example/", "keys": {"p256dh": "a", "auth": "b"}},
])
def test_a_bad_subscription_is_a_400(client, body):
    assert client.post("/api/push/subscriptions", json=body).status_code == 400
    assert webpush.subscriptions() == []


def test_a_user_sees_and_manages_only_their_own_phones(client):
    mine = _add(Phone(APPLE + "kari"), "kari")
    admins = _add(Phone(APPLE + "admin"), "admin")
    client.cookies.set("session_id", _session_for("kari"))

    got = client.get("/api/push").json()
    assert [d["id"] for d in got["devices"]] == [mine["id"]]
    assert got["is_admin"] is False
    # Somebody else's phone does not exist, as far as kari can tell.
    assert client.delete(f"/api/push/subscriptions/{admins['id']}").status_code == 404
    assert client.post(f"/api/push/subscriptions/{admins['id']}/test").status_code == 404
    assert client.delete(f"/api/push/subscriptions/{mine['id']}").status_code == 200
    assert [s["id"] for s in webpush.subscriptions()] == [admins["id"]]


def test_an_administrator_sees_and_removes_every_phone(client):
    karis = _add(Phone(APPLE + "kari"), "kari")
    _add(Phone(APPLE + "admin"), "admin")
    assert len(client.get("/api/push").json()["devices"]) == 2
    assert client.delete(f"/api/push/subscriptions/{karis['id']}").status_code == 200


def test_a_test_alert(client, push_service):
    phone = Phone()
    sub = _add(phone)
    assert client.post(f"/api/push/subscriptions/{sub['id']}/test").json() == {"status": "sent"}
    assert json.loads(phone.decrypt(push_service.calls[0][1]))["title"] == "Test alert"


def test_a_test_alert_to_a_phone_that_has_gone(client, push_service):
    push_service.status = 410
    sub = _add(Phone())
    assert client.post(f"/api/push/subscriptions/{sub['id']}/test").status_code == 410
    assert webpush.subscriptions() == []


def test_a_test_alert_that_is_refused_says_why(client, push_service):
    push_service.status, push_service.answer = 403, b"BadJwtToken"
    sub = _add(Phone())
    r = client.post(f"/api/push/subscriptions/{sub['id']}/test")
    assert r.status_code == 502
    assert "BadJwtToken" in r.json()["detail"]


def test_removing_a_user_turns_off_their_phones(client):
    _add(Phone(), "kari")
    assert client.delete("/auth/admin/users/kari").status_code == 200
    assert webpush.subscriptions() == []


def test_renaming_a_user_keeps_their_phones(client):
    _add(Phone(), "kari")
    assert client.patch("/auth/admin/users/kari", json={"new_username": "kari2"}).status_code == 200
    assert webpush.subscriptions()[0]["username"] == "kari2"


def test_renaming_yourself_keeps_your_phones(client):
    _add(Phone(), "kari")
    client.cookies.set("session_id", _session_for("kari"))
    assert client.post("/auth/rename", json={"new_username": "kari2"}).status_code == 200
    assert webpush.subscriptions()[0]["username"] == "kari2"


# -- Turning alerts on with phones and no email ------------------------------


@pytest.fixture
def fresh_notifier(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "DATA_DIR", tmp_path)
    monkeypatch.setattr(notifications, "NOTIFICATIONS_FILE", tmp_path / "notifications.json")
    monkeypatch.setattr(server, "notifier", notifications.Notifier())


def test_phones_only_may_be_turned_on(client, fresh_notifier):
    _add(Phone())
    assert client.put("/api/notifications", json={"enabled": True}).status_code == 200
    assert notifications.load_settings()["enabled"] is True


def test_with_neither_phones_nor_email_it_is_refused(client, fresh_notifier):
    r = client.put("/api/notifications", json={"enabled": True})
    assert r.status_code == 400
    assert "phone" in r.json()["detail"] and "mail server" in r.json()["detail"]


def test_half_an_email_setup_is_refused_even_with_phones(client, fresh_notifier):
    _add(Phone())
    r = client.put("/api/notifications", json={"enabled": True,
                                               "email": {"to_addrs": ["me@example.com"]}})
    assert r.status_code == 400


# -- The service worker ------------------------------------------------------


def test_the_service_worker_is_served_from_the_root_behind_sign_in(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-cache"
    with TestClient(server.app) as anon:
        anon.cookies.clear()
        assert anon.get("/sw.js", follow_redirects=False).status_code in (302, 401)


def test_the_service_worker_always_shows_what_it_is_sent():
    # iOS withdraws push from a site whose worker receives a push and shows
    # nothing, so every path through the handler must end in a notification.
    assert "addEventListener('push'" in SW
    assert "showNotification" in SW
    assert "event.waitUntil" in SW


def test_the_service_worker_does_not_intercept_requests():
    # No fetch handler: it exists for alerts, not to cache a heating app.
    assert "'fetch'" not in SW and '"fetch"' not in SW


def test_a_tap_only_opens_this_site():
    assert "self.location.origin" in SW


# -- The interface -----------------------------------------------------------


def test_the_api_client_names_each_push_endpoint():
    for name in ("push:", "addPush:", "removePush:", "testPush:"):
        assert name in CORE


def test_permission_is_asked_before_anything_is_awaited():
    # Safari grants the prompt only inside the tap that asked for it; one
    # await first and it is refused without being shown.
    start = CABIN.index("function enablePushHere(")
    body = CABIN[start:CABIN.index("\n  }\n", start)]
    assert body.index("Notification.requestPermission()") < body.index("await")


def test_the_worker_is_registered_for_the_whole_site():
    assert "serviceWorker.register('/sw.js'" in CABIN


def test_the_backup_leaves_the_push_key_out():
    script = (ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
    assert 'rm -rf "$TMPDIR/backup/data/webpush"' in script


def test_cryptography_is_a_declared_dependency():
    assert re.search(r"^cryptography>=", (ROOT / "requirements.txt").read_text(), re.M)
