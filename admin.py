"""
Lazytype — admin-tool (owner-only).

Genereer en beheer abonnement-licentiesleutels. Deze tool bevat het geheim
(LAZYTYPE_LICENSE_SECRET) en hoort NIET in de gedistribueerde app/exe.

De *_text()-functies geven leesbare strings terug en worden hergebruikt door
het in-app Admin-menu van de tray.

Interactief menu:      python admin.py
Scriptbaar:
    python admin.py gen <email> [tier] [dagen]   # tier: personal|pro (default pro, 30d; personal=0/perpetueel)
    python admin.py list
    python admin.py verify <sleutel>
    python admin.py revoke <id>
    python admin.py tiers
    python admin.py server
"""

import json
import os
import sys
import time

import dictate          # load_env(), save_env_value(), ROOT (lichte import)
import license as lic

DATA = dictate.ROOT / "admin_data"
REG = DATA / "licenses.json"
REVOKED = DATA / "revoked.json"
PRICING = dictate.ROOT / "pricing.json"


# ── Opslag ──────────────────────────────────────────────────────────────
def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, obj):
    DATA.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def registry():
    return _load(REG, [])


def revoked_ids():
    return set(_load(REVOKED, []))


# ── Geheim ──────────────────────────────────────────────────────────────
def ensure_secret() -> str:
    sec = os.environ.get(lic.SECRET_ENV, "")
    if not sec:
        sec = lic.new_secret()
        dictate.save_env_value(lic.SECRET_ENV, sec)
    return sec


# ── Cores (geven tekst/objecten terug; herbruikbaar in de tray) ─────────
def gen(email: str, tier: str = "pro", days: int = 30):
    ensure_secret()
    key = lic.generate(email, tier=tier, days=int(days))
    payload = lic.decode(key)
    reg = registry()
    reg.append({**payload, "email": email, "key": key, "created": int(time.time())})
    _save(REG, reg)
    return key, payload


def list_text() -> str:
    reg = registry()
    rev = revoked_ids()
    if not reg:
        return "Nog geen sleutels uitgegeven."
    lines = [f"{'EMAIL':26} {'TIER':9} {'STATUS':11} {'VERVALT':11} ID", "-" * 74]
    for e in reg:
        ok, reason, _ = lic.verify(e["key"], revoked=rev)
        status = "geldig" if ok else reason
        exp = int(e.get("exp", 0) or 0)
        when = "altijd" if exp == 0 else time.strftime("%Y-%m-%d", time.localtime(exp))
        lines.append(f"{e.get('email','?'):26} {e.get('tier','?'):9} {status:11} {when:11} {e.get('id','?')}")
    return "\n".join(lines)


def verify_text(key: str) -> str:
    ok, reason, payload = lic.verify(key, revoked=revoked_ids())
    mark = "✅" if ok else "❌"
    extra = f" — {lic.describe(payload)} ({payload.get('email','?')})" if payload else ""
    return f"{mark} {reason}{extra}"


def revoke(key_id: str) -> str:
    rev = list(revoked_ids())
    if key_id in rev:
        return "Was al ingetrokken."
    rev.append(key_id)
    _save(REVOKED, rev)
    return (f"🚫 Ingetrokken: {key_id}\n"
            "Upload admin_data/revoked.json → server (public_html/api/revoked.json).")


def tiers_text() -> str:
    data = _load(PRICING, {})
    sym = data.get("symbol", "€")
    comp = data.get("competitor", {})
    out = [f"LAZYTYPE — PRIJZEN   (Wispr Flow ≈ {sym}{comp.get('price_month','?')}/mnd)", ""]
    for t in data.get("tiers", []):
        if "price_once" in t:
            price = "gratis" if t["price_once"] == 0 else f"{sym}{t['price_once']} eenmalig"
        elif t.get("price_month", 0) == 0:
            price = "gratis"
        else:
            yr = f" of {sym}{t['price_year']}/jaar" if t.get("price_year") else ""
            unit = t.get("price_unit", "/maand")
            price = f"{sym}{t['price_month']} {unit}{yr}"
        if t.get("price_placeholder"):
            price += " (placeholder)"
        star = " ⭐" if t.get("highlight") else ""
        out.append(f"  {t['name']:9} {price}{star}")
        out.append(f"            {t.get('tagline','')}")
        out += [f"              • {f}" for f in t.get("features", [])]
        out.append("")
    return "\n".join(out)


def server_text() -> str:
    sec = os.environ.get(lic.SECRET_ENV, "(nog niet ingesteld — genereer eerst een sleutel)")
    return (
        "SERVER-CONFIG (Hostinger, naast api/transcribe.php):\n\n"
        "Zet als env-var of in api/config.php:\n"
        f"  LAZYTYPE_LICENSE_SECRET = {sec}\n"
        "  GROQ_API_KEY            = <jouw Groq-key>   (server-side!)\n\n"
        "Upload via FTP:\n"
        "  • api/transcribe.php       → public_html/api/transcribe.php\n"
        "  • admin_data/revoked.json  → public_html/api/revoked.json (na intrekken)\n\n"
        "Client wijst standaard naar https://lazytype.com/api/transcribe.php\n"
        "(te overrulen met LAZYTYPE_API in .env)."
    )


# ── CLI / interactief menu ──────────────────────────────────────────────
MENU = """
══════════ Lazytype admin ══════════
  1. Sleutel genereren
  2. Sleutels tonen
  3. Sleutel verifiëren
  4. Sleutel intrekken
  5. Tiers & prijzen
  6. Server-config tonen
  0. Afsluiten
════════════════════════════════════"""


def menu():
    while True:
        print(MENU)
        choice = input("Keuze: ").strip()
        try:
            if choice == "1":
                email = input("  E-mail: ").strip()
                tier = (input("  Tier [personal/pro] (pro): ").strip() or "pro").lower()
                dflt = "0" if tier == "personal" else "30"
                days = int(input(f"  Geldig (dagen, 0=voor altijd) ({dflt}): ").strip() or dflt)
                key, payload = gen(email, tier, days)
                print(f"\n✅ {lic.describe(payload)} voor {email}:\n\n   {key}\n")
            elif choice == "2":
                print(list_text())
            elif choice == "3":
                print(verify_text(input("  Plak sleutel: ").strip()))
            elif choice == "4":
                print(revoke(input("  ID om in te trekken: ").strip()))
            elif choice == "5":
                print(tiers_text())
            elif choice == "6":
                print(server_text())
            elif choice in ("0", "q", "exit"):
                return
            else:
                print("  ?")
        except Exception as e:
            print(f"  ⚠ {e}")


def main():
    args = sys.argv[1:]
    if not args:
        menu()
        return
    cmd, *rest = args
    try:
        if cmd == "gen" and rest:
            key, payload = gen(*rest)
            print(f"✅ {lic.describe(payload)} voor {rest[0]}:\n\n   {key}\n")
        elif cmd == "list":
            print(list_text())
        elif cmd == "verify" and rest:
            print(verify_text(rest[0]))
        elif cmd == "revoke" and rest:
            print(revoke(rest[0]))
        elif cmd == "tiers":
            print(tiers_text())
        elif cmd == "server":
            print(server_text())
        else:
            print(__doc__)
    except Exception as e:
        print(f"⚠ {e}")


if __name__ == "__main__":
    main()
