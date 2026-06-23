#!/usr/bin/env python3
"""Smoke-tests voor de Lazytype-API (lazytype.com/api).

- Tests 1-4: sturen FOUTE/lege input → moeten een nette HTTP-code geven (géén 500).
  Dit vangt crashes op edge-input (zoals de b64-padding-bug die ALLE managed
  transcriptie brak). Geen bijwerkingen (input wordt vóór DB/Groq/e-mail afgewezen).
- Test 5 (optioneel): met een geminte test-sleutel een AI-mode draaien → 200 + tekst.
  Draait alleen als de repo-secret LAZYTYPE_LICENSE_SECRET is gezet.
"""
import os, sys, json, time, hmac, hashlib, base64
import urllib.request, urllib.parse, urllib.error

BASE = os.environ.get("LAZYTYPE_API_BASE", "https://lazytype.com/api")
fails = []


def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f"{BASE}/{path}", data=body)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


# 1) geen licentie → 402, en sowieso geen 500
s, b = post("transcribe.php", {})
check("transcribe zonder licentie geen 500", s != 500, f"status={s}")
check("transcribe zonder licentie = 402", s == 402, f"status={s}")

# 2) onzin-formaat → geen 500
s, b = post("transcribe.php", {"license": "nonsense"})
check("transcribe ongeldig format geen 500", s != 500, f"status={s}")

# 3) geldig formaat maar foute handtekening → 403 (en geen 500)
s, b = post("transcribe.php", {"license": "LZT.YWJj.ZGVm"})
check("transcribe foute handtekening geen 500", s != 500, f"status={s}")
check("transcribe foute handtekening = 403", s == 403, f"status={s}")

# 4) trial met ongeldig e-mail → 400 (en geen 500)
s, b = post("trial.php", {"email": "geen-email"})
check("trial ongeldig e-mail geen 500", s != 500, f"status={s}")
check("trial ongeldig e-mail = 400", s == 400, f"status={s}")

# 5) optioneel: echte managed AI-mode met geminte test-sleutel
secret = os.environ.get("LAZYTYPE_LICENSE_SECRET", "").strip()
if secret:
    def b64(x): return base64.urlsafe_b64encode(x).rstrip(b"=").decode()
    payload = {"id": b64(b"citest"), "email": "ci@lazytype.com",
               "tier": "lifetime", "iat": int(time.time()), "exp": 0}
    pb = b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = b64(hmac.new(secret.encode(), pb.encode(), hashlib.sha256).digest())
    key = f"LZT.{pb}.{sig}"
    s, b = post("transcribe.php", {"license": key, "device": "ci-smoke",
                                   "instruction": "maak hoofdletters", "command": "hallo ci"})
    check("managed AI-mode = 200", s == 200, f"status={s} body={b[:120]}")
    try:
        check("managed AI-mode geeft tekst", "HALLO CI" in json.loads(b).get("text", "").upper(), b[:120])
    except Exception:
        check("managed AI-mode geldige JSON", False, b[:120])
else:
    print("SKIP managed-test — zet repo-secret LAZYTYPE_LICENSE_SECRET om dit te activeren")

if fails:
    print(f"\n{len(fails)} CHECK(S) GEFAALD: {fails}")
    sys.exit(1)
print("\nAlle API-smoke-checks geslaagd.")
