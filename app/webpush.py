"""
Web Push: alerts on a phone, delivered to the Cabin app on its home screen.

``docs/PUSH.md`` is the design and the setup guide. In short:

* A phone that has Cabin on its home screen asks for permission once, and its
  browser hands back a **subscription**: an address at Apple's (or Google's,
  Mozilla's, Microsoft's) push service, plus two keys that belong to the phone.
* To alert it, the Pi encrypts the message to the phone's key (RFC 8291) and
  POSTs it to that address, signed with this installation's own key (VAPID,
  RFC 8292). The push service cannot read the message; it only carries it.
* Nothing here needs the phone to be on the cabin network, or the Pi to be
  reachable from the Internet. The Pi only ever calls *out*.

What this module deliberately does not do:

* **No third-party push library.** The protocol is two RFCs and a JWT, and the
  ``cryptography`` package already does the hard parts. A dependency that
  speaks HTTP for us is a dependency that could be talked into speaking it to
  somewhere else.
* **Nowhere but a push service.** ``check_allowed`` permits only the push
  services' own hosts, over https, on the default port, and redirects are
  refused. A subscription is supplied by a browser, which means it is
  supplied by whoever is signed in; without this, "subscribe" would be a way
  to make the Pi POST to any address on the LAN.
* **No critical alerts.** Web Push has no equivalent of iOS's critical or
  time-sensitive levels. ``Urgency: high`` is the most it can ask for, and a
  phone in Focus or Do Not Disturb will hold the alert like any other.

The private key and the subscriptions live in ``data/webpush/`` (0700, files
0600) and are left out of backups, like the alarm and weather secrets: a
restored Pi has a new key, and each phone turns notifications on again.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("nobo-control.webpush")

DATA_DIR = Path(__file__).resolve().parent / "data"

# Apple rejects a VAPID subject of mailto:…@localhost, and this installation
# has no address of its own that it could honestly give. The project's home
# page is a real, stable contact for whoever runs the push service.
VAPID_SUBJECT = "https://github.com/aba1975/Nobo_Raspberry_PI_Zigbee"
# Apple refuses a token valid for more than an hour or so beyond a day.
VAPID_LIFETIME_SECONDS = 12 * 3600

SEND_TIMEOUT_SECONDS = 10
# A day: an alert about a door left open is still worth seeing after a phone
# has been switched off overnight, but not after a week.
TTL_SECONDS = 24 * 3600
RECORD_SIZE = 4096
# Apple's limit is 4096 bytes of encrypted payload. RFC 8291 adds 86 bytes of
# header, a 16-byte tag and a delimiter, so this leaves plenty in hand.
MAX_PAYLOAD_BYTES = 3000
MAX_BODY_CHARS = 600

MAX_SUBSCRIPTIONS = 50
MAX_ENDPOINT_LENGTH = 1024
MAX_LABEL_LENGTH = 40

URGENCY = {"info": "low", "warning": "normal", "critical": "high"}

# The push services of the browsers that can install Cabin. Exact host names,
# or a suffix where the service hands out per-region hosts.
ALLOWED_HOSTS = frozenset({
    "web.push.apple.com",
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
})
ALLOWED_HOST_SUFFIXES = (".push.apple.com", ".notify.windows.com")


class ForbiddenEndpoint(ValueError):
    """An address this integration has no business posting to."""


class InvalidSubscription(ValueError):
    """What the browser sent cannot be a working push subscription."""


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    text = "".join(str(text).split())
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def endpoint_hash(endpoint: str) -> str:
    """How the interface recognises its own subscription without seeing others'."""
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )


# ---------------------------------------------------------------------------
# Where a subscription may point
# ---------------------------------------------------------------------------

def check_allowed(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        raise ForbiddenEndpoint("That is not a push service address")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not (host in ALLOWED_HOSTS or host.endswith(ALLOWED_HOST_SUFFIXES))
    ):
        raise ForbiddenEndpoint("Only a browser push service may be used")


def validate_subscription(endpoint: Any, p256dh: Any, auth_secret: Any) -> Tuple[str, str, str]:
    """The three things a browser hands over, checked; raises InvalidSubscription."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH:
        raise InvalidSubscription("The push address is missing or too long")
    try:
        check_allowed(endpoint)
    except ForbiddenEndpoint as exc:
        raise InvalidSubscription(str(exc)) from None
    if not isinstance(p256dh, str) or not isinstance(auth_secret, str):
        raise InvalidSubscription("The subscription keys are missing")
    try:
        raw_key = b64url_decode(p256dh)
        raw_auth = b64url_decode(auth_secret)
    except (ValueError, TypeError):
        raise InvalidSubscription("The subscription keys are not valid base64") from None
    if len(raw_auth) != 16:
        raise InvalidSubscription("The subscription's auth secret is the wrong length")
    if len(raw_key) != 65 or raw_key[0] != 0x04:
        raise InvalidSubscription("The subscription's key is not a P-256 public key")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw_key)
    except ValueError:
        raise InvalidSubscription("The subscription's key is not on the curve") from None
    return endpoint, b64url_encode(raw_key), b64url_encode(raw_auth)


# ---------------------------------------------------------------------------
# RFC 8291: message encryption
# ---------------------------------------------------------------------------

def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *,
            salt: Optional[bytes] = None,
            as_private: Optional[ec.EllipticCurvePrivateKey] = None,
            record_size: int = RECORD_SIZE) -> bytes:
    """Encrypt one message to one subscription, as a single aes128gcm record.

    ``salt`` and ``as_private`` are only ever passed by the tests, to reproduce
    the worked example in RFC 8291 Appendix A. In use both are fresh per
    message, which is what makes each message's key unique.
    """
    salt = salt if salt is not None else secrets.token_bytes(16)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(as_private.public_key())
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), peer)

    prk_key = _hmac(auth_secret, ecdh_secret)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hmac(prk_key, key_info + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]

    header = salt + struct.pack("!IB", record_size, len(as_public)) + as_public
    # 0x02 marks the last (here the only) record; no further padding.
    return header + AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)


# ---------------------------------------------------------------------------
# RFC 8292: VAPID, this installation's signature
# ---------------------------------------------------------------------------

def _push_dir() -> Path:
    return DATA_DIR / "webpush"


def _key_file() -> Path:
    return _push_dir() / "vapid_private.pem"


def _subscriptions_file() -> Path:
    return _push_dir() / "subscriptions.json"


def _secure_dir() -> Path:
    directory = _push_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _write_private(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_key_lock = threading.Lock()


def load_or_create_key() -> ec.EllipticCurvePrivateKey:
    """This installation's VAPID key, made the first time anybody needs it."""
    with _key_lock:
        path = _key_file()
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
                return key
            # Never silently replace a key: every phone subscribed with it
            # would stop receiving alerts without anything saying why.
            raise RuntimeError(f"{path} is not a P-256 key; move it aside to make a new one")
        _secure_dir()
        key = ec.generate_private_key(ec.SECP256R1())
        _write_private(path, key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        logger.info("Created this installation's Web Push key")
        return key


def public_key() -> str:
    """What the browser passes to ``pushManager.subscribe`` as applicationServerKey."""
    return b64url_encode(_public_bytes(load_or_create_key().public_key()))


def vapid_authorization(endpoint: str, key: ec.EllipticCurvePrivateKey,
                        now: Optional[float] = None) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    claims = {
        "aud": f"{parsed.scheme}://{parsed.netloc}",
        "exp": int((now if now is not None else time.time()) + VAPID_LIFETIME_SECONDS),
        "sub": VAPID_SUBJECT,
    }
    header = {"typ": "JWT", "alg": "ES256"}
    signing_input = (
        b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    )
    r, s = decode_dss_signature(key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256())))
    token = signing_input + "." + b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token},k={b64url_encode(_public_bytes(key.public_key()))}"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A push service answers; it never sends us elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ForbiddenEndpoint(f"The push service redirected to {newurl!r}; not followed")


_opener = urllib.request.build_opener(_NoRedirect())

# (status, body). Raises OSError when nothing came back at all.
Transport = Callable[[str, bytes, Mapping[str, str]], Tuple[int, bytes]]


def http_post(url: str, body: bytes, headers: Mapping[str, str]) -> Tuple[int, bytes]:
    check_allowed(url)
    request = urllib.request.Request(url, data=body, method="POST", headers=dict(headers))
    try:
        with _opener.open(request, timeout=SEND_TIMEOUT_SECONDS) as response:
            return response.status, response.read(512)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(512)


# ---------------------------------------------------------------------------
# The subscriptions
# ---------------------------------------------------------------------------

_store_lock = threading.RLock()


def label_from_user_agent(user_agent: str) -> str:
    ua = user_agent or ""
    for needle, label in (
        ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
        ("Macintosh", "Mac"), ("Windows", "Windows PC"), ("CrOS", "Chromebook"),
        ("Linux", "Linux"),
    ):
        if needle in ua:
            return label
    return "Browser"


def _load() -> List[Dict[str, Any]]:
    path = _subscriptions_file()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.error("Could not read %s: %s", path, exc)
        return []
    subs = doc.get("subscriptions") if isinstance(doc, dict) else None
    if not isinstance(subs, list):
        return []
    out = []
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        try:
            endpoint, p256dh, auth_secret = validate_subscription(
                sub.get("endpoint"), sub.get("p256dh"), sub.get("auth"),
            )
        except InvalidSubscription:
            continue
        if not isinstance(sub.get("id"), str) or not isinstance(sub.get("username"), str):
            continue
        out.append({**sub, "endpoint": endpoint, "p256dh": p256dh, "auth": auth_secret})
    return out


def _save(subs: List[Dict[str, Any]]) -> None:
    _secure_dir()
    data = json.dumps({"schema_version": 1, "subscriptions": subs}, indent=2).encode("utf-8")
    _write_private(_subscriptions_file(), data)


def subscriptions() -> List[Dict[str, Any]]:
    with _store_lock:
        return _load()


def _iso(epoch: Any) -> Optional[str]:
    if not isinstance(epoch, (int, float)):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def public_device(sub: Mapping[str, Any]) -> Dict[str, Any]:
    """A subscription as the interface may see it: never the address or keys."""
    error = sub.get("last_error")
    return {
        "id": sub["id"],
        "label": sub.get("label") or "Browser",
        "username": sub["username"],
        "created": _iso(sub.get("created")),
        "last_success": _iso(sub.get("last_success")),
        "last_error": (
            {"at": _iso(error.get("at")), "detail": str(error.get("detail") or "")}
            if isinstance(error, dict) else None
        ),
        "endpoint_hash": endpoint_hash(sub["endpoint"]),
    }


def add_subscription(username: str, endpoint: Any, p256dh: Any, auth_secret: Any,
                     label: str = "") -> Dict[str, Any]:
    """Save a phone's subscription, or refresh it if this address is known."""
    endpoint, p256dh, auth_secret = validate_subscription(endpoint, p256dh, auth_secret)
    label = str(label or "Browser").strip()[:MAX_LABEL_LENGTH] or "Browser"
    with _store_lock:
        subs = _load()
        for sub in subs:
            if sub["endpoint"] == endpoint:
                # The same browser, signed in as whoever is signed in now.
                sub.update(username=username, p256dh=p256dh, auth=auth_secret,
                           label=label, last_error=None)
                _save(subs)
                return sub
        if len(subs) >= MAX_SUBSCRIPTIONS:
            raise InvalidSubscription(
                f"There are already {MAX_SUBSCRIPTIONS} devices; remove one first",
            )
        sub = {
            "id": secrets.token_hex(8),
            "username": username,
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth_secret,
            "label": label,
            "created": time.time(),
            "last_success": None,
            "last_error": None,
        }
        subs.append(sub)
        _save(subs)
        return sub


def get_subscription(sub_id: str) -> Optional[Dict[str, Any]]:
    with _store_lock:
        return next((s for s in _load() if s["id"] == sub_id), None)


def remove_subscription(sub_id: str) -> bool:
    with _store_lock:
        subs = _load()
        kept = [s for s in subs if s["id"] != sub_id]
        if len(kept) == len(subs):
            return False
        _save(kept)
        return True


def remove_user(username: str) -> int:
    """A deleted user's phones stop receiving alerts at once."""
    with _store_lock:
        subs = _load()
        kept = [s for s in subs if s["username"] != username]
        if len(kept) != len(subs):
            _save(kept)
        return len(subs) - len(kept)


def rename_user(old: str, new: str) -> None:
    with _store_lock:
        subs = _load()
        changed = False
        for sub in subs:
            if sub["username"] == old:
                sub["username"] = new
                changed = True
        if changed:
            _save(subs)


def _record(sub_id: str, *, success: bool, error: Optional[str] = None) -> None:
    with _store_lock:
        subs = _load()
        for sub in subs:
            if sub["id"] == sub_id:
                if success:
                    sub["last_success"] = time.time()
                    sub["last_error"] = None
                else:
                    sub["last_error"] = {"at": time.time(), "detail": (error or "")[:200]}
                _save(subs)
                return


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def build_payload(title: str, body: str, severity: str = "info",
                  tag: Optional[str] = None, url: str = "/") -> bytes:
    """The JSON the service worker turns into a notification, kept under the limit."""
    body = body.strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[: MAX_BODY_CHARS - 1].rstrip() + "…"
    message = {"title": title[:120], "body": body, "severity": severity, "url": url}
    if tag:
        message["tag"] = tag[:64]
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    while len(data) > MAX_PAYLOAD_BYTES and message["body"]:
        message["body"] = message["body"][: len(message["body"]) // 2] + "…"
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return data


def topic_for(tag: str) -> str:
    """A Topic header: a later message with the same topic replaces one not yet delivered."""
    return b64url_encode(hashlib.sha256(tag.encode("utf-8")).digest())[:22]


def send(sub: Mapping[str, Any], payload: bytes, *, severity: str = "info",
         tag: Optional[str] = None, key: Optional[ec.EllipticCurvePrivateKey] = None,
         transport: Optional[Transport] = None) -> str:
    """Deliver one message to one subscription.

    Returns ``"sent"``, ``"gone"`` (the phone has unsubscribed, or the app was
    removed; the subscription has been deleted) or ``"failed"``. Never raises
    for a delivery problem, because an alert channel being down must never
    become a heating fault.
    """
    key = key or load_or_create_key()
    transport = transport or http_post
    endpoint = sub["endpoint"]
    try:
        body = encrypt(payload, b64url_decode(sub["p256dh"]), b64url_decode(sub["auth"]))
        headers = {
            "Authorization": vapid_authorization(endpoint, key),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(TTL_SECONDS),
            "Urgency": URGENCY.get(severity, "normal"),
        }
        if tag:
            headers["Topic"] = topic_for(tag)
        status, answer = transport(endpoint, body, headers)
    except (OSError, ForbiddenEndpoint, ValueError) as exc:
        logger.warning("Push to %s (%s) failed: %s", sub.get("label"), sub["id"], exc)
        _record(sub["id"], success=False, error=str(exc))
        return "failed"

    if 200 <= status < 300:
        _record(sub["id"], success=True)
        return "sent"
    if status in (404, 410):
        logger.info("Push subscription %s (%s) has gone; removing it", sub["id"], sub.get("label"))
        remove_subscription(sub["id"])
        return "gone"
    detail = answer.decode("utf-8", "replace").strip()[:160]
    # Apple answers {"reason": "BadJwtToken"}; the reason alone is what helps.
    try:
        reason = json.loads(detail).get("reason")
    except (ValueError, AttributeError):
        reason = None
    if isinstance(reason, str) and reason:
        detail = reason[:160]
    logger.warning("Push to %s (%s) refused: %s %s", sub.get("label"), sub["id"], status, detail)
    _record(sub["id"], success=False, error=f"{status} {detail}".strip())
    return "failed"


def deliver_all(title: str, body: str, severity: str = "info",
                tag: Optional[str] = None) -> Dict[str, int]:
    """Send one alert to every subscribed device. Called on the notifier's thread."""
    subs = subscriptions()
    counts = {"sent": 0, "gone": 0, "failed": 0}
    if not subs:
        return counts
    payload = build_payload(title, body, severity, tag)
    key = load_or_create_key()
    for sub in subs:
        counts[send(sub, payload, severity=severity, tag=tag, key=key)] += 1
    return counts
