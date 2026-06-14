"""
Lazytype — licentiesleutels voor abonnementen (HMAC-ondertekend).

Een sleutel ziet er zo uit:   LZT.<payload>.<sig>
    payload = base64url(JSON {id,email,tier,iat,exp})   exp=0 → lifetime
    sig     = base64url(HMAC-SHA256(secret, payload))

Verificatie gebeurt SERVER-side (api/transcribe.php) — daar zit het geheim en
daar wordt betaald gebruik gegate. Deze module is voor:
  • de admin-tool (admin.py) om sleutels te genereren/verifiëren;
  • de client, die alleen `decode()` gebruikt om tier/vervaldatum te tónen.

Het geheim (LAZYTYPE_LICENSE_SECRET) staat in .env en op de server, en wordt
NOOIT meegeleverd in de gedistribueerde app/exe.
"""

import base64
import hashlib
import hmac
import json
import os
import time

SECRET_ENV = "LAZYTYPE_LICENSE_SECRET"
PREFIX = "LZT"

# Sleutel-tiers (moet gelijklopen met pricing.json en transcribe.php).
# "trial"    = 14-daagse proefsleutel (server-managed, net als Pro maar met vervaldatum)
# "personal" = eenmalige aanschaf, eigen Groq-key (BYOK), geen vervaldatum
# "pro"      = abonnement, server-managed, geen vervaldatum
TIERS = {
    "trial":    {"name": "Proef (14d)", "managed": True},   # proefsleutel, server-managed
    "personal": {"name": "Personal",   "managed": False},   # eenmalig, eigen Groq-key
    "pro":      {"name": "Pro",        "managed": True},    # abonnement, wij hosten
}


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret(secret=None) -> bytes:
    if secret is not None:
        return secret if isinstance(secret, bytes) else secret.encode()
    return os.environ.get(SECRET_ENV, "").encode()


def _sign(payload_b64: str, secret: bytes) -> str:
    return _b64e(hmac.new(secret, payload_b64.encode(), hashlib.sha256).digest())


def new_secret() -> str:
    """Genereer een sterk geheim (hex). Eenmalig instellen, op server én in .env."""
    return os.urandom(32).hex()


def generate(email: str, tier: str = "pro", days: int = 30, secret=None) -> str:
    """Maak een ondertekende sleutel. days<=0 of tier=lifetime → geen vervaldatum."""
    sec = _secret(secret)
    if not sec:
        raise RuntimeError(f"{SECRET_ENV} ontbreekt (zet 'm in .env)")
    if tier not in TIERS:
        raise ValueError(f"onbekende tier: {tier}")
    now = int(time.time())
    exp = 0 if (tier == "lifetime" or days <= 0) else now + int(days) * 86400
    payload = {"id": _b64e(os.urandom(6)), "email": email, "tier": tier, "iat": now, "exp": exp}
    pb = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return f"{PREFIX}.{pb}.{_sign(pb, sec)}"


def decode(key: str):
    """Lees de payload ZONDER verificatie — uitsluitend voor weergave in de client."""
    try:
        prefix, pb, _sig = key.strip().split(".")
        if prefix != PREFIX:
            return None
        return json.loads(_b64d(pb))
    except Exception:
        return None


def verify(key: str, secret=None, revoked=None):
    """(ok, reden, payload). Echte poortwachter draait op de server; dit is de
    referentie-implementatie die admin.py gebruikt."""
    sec = _secret(secret)
    try:
        prefix, pb, sig = key.strip().split(".")
    except ValueError:
        return False, "ongeldig formaat", None
    if prefix != PREFIX:
        return False, "ongeldig formaat", None
    if not sec:
        return False, "geen geheim ingesteld", None
    if not hmac.compare_digest(sig, _sign(pb, sec)):
        return False, "handtekening klopt niet", None
    try:
        payload = json.loads(_b64d(pb))
    except Exception:
        return False, "payload onleesbaar", None
    if revoked and payload.get("id") in set(revoked):
        return False, "ingetrokken", payload
    exp = int(payload.get("exp", 0) or 0)
    if exp and time.time() > exp:
        return False, "verlopen", payload
    if payload.get("tier") not in TIERS:
        return False, "onbekende tier", payload
    return True, "geldig", payload


def describe(payload: dict) -> str:
    """Korte, leesbare omschrijving voor in een menu/CLI."""
    if not payload:
        return "geen geldige sleutel"
    tier = TIERS.get(payload.get("tier", ""), {}).get("name", payload.get("tier", "?"))
    exp = int(payload.get("exp", 0) or 0)
    when = "voor altijd" if exp == 0 else "tot " + time.strftime("%Y-%m-%d", time.localtime(exp))
    return f"{tier} · {when}"
