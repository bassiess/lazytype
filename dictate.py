"""
Lazytype — globale-sneltoets dicteertool.

Houd een toets ingedrukt (standaard: rechter Ctrl), spreek, laat los.
De tekst wordt razendsnel getranscribeerd (Groq whisper-large-v3-turbo)
en direct geplakt in de app waar je cursor staat.

Gebruik:
    python dictate.py                # start de dicteertool (daemon)
    python dictate.py --test x.wav   # test alleen de transcriptie op een bestand
    python dictate.py --devices      # toon microfoons
    python dictate.py --check        # test mic-opname (3s) + transcriptie

Config via .env (zie .env.example):
    GROQ_API_KEY=...                 # vereist voor de Groq-engine
    OPENAI_API_KEY=...               # alternatief
    DICTATE_ENGINE=groq              # groq | openai | local
    DICTATE_HOTKEY=ctrl_r            # welke toets je ingedrukt houdt
    DICTATE_LANGUAGE=nl              # nl | en | ... | auto
    DICTATE_POSTPROCESS=off          # off | clean | taalcode — AI-opschonen of -vertalen
"""

import io
import os
import re
import sys
import math
import time
import wave
import subprocess
import threading
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# Forceer UTF-8 op de uitvoer, anders crasht print() met emoji/pijltjes op een
# cp1252-console (standaard op Nederlandse Windows).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Persistente opslag: AppData op Windows, ~/.config/Lazytype op macOS.
# Bij frozen exe: migreer .env naast de exe naar AppData als die al bestaat.
def _win_appdata() -> str:
    """Echte %APPDATA% (Roaming) via de Windows-API — werkt óók als de APPDATA/
    USERPROFILE-env-vars ontbreken (bijv. na een update-relaunch met een schone
    omgeving). Gebruikt het token van de huidige gebruiker, dus altijd het juiste
    profiel → config (licentie!) blijft behouden over zo'n relaunch heen."""
    if not IS_WIN:
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        CSIDL_APPDATA = 0x001A
        SHGFP_TYPE_CURRENT = 0
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        fn = ctypes.windll.shell32.SHGetFolderPathW
        if fn(None, CSIDL_APPDATA, None, SHGFP_TYPE_CURRENT, buf) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return ""


def _user_home() -> Path:
    """Home-map robuust bepalen. Path.home() crasht ('Could not determine home
    directory') als HOME/USERPROFILE ontbreekt — wat gebeurt bij een 'schone'
    relaunch-omgeving direct na een update. Daarom met fallbacks."""
    try:
        return Path.home()
    except Exception:
        for _v in ("USERPROFILE", "HOME"):
            _p = os.environ.get(_v)
            if _p:
                return Path(_p)
        _hd, _hp = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
        if _hd and _hp:
            return Path(_hd + _hp)
        import tempfile
        return Path(tempfile.gettempdir())   # laatste redmiddel — nooit crashen


if getattr(sys, "frozen", False):
    if IS_WIN:
        # Echte AppData eerst via de Windows-API (overleeft een schone relaunch),
        # dan de env-var, dan home/Roaming.
        _appdata = _win_appdata() or os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        _base = Path(_appdata).expanduser() if _appdata else (_user_home() / "AppData" / "Roaming")
        ROOT = _base / "Lazytype"
    else:
        ROOT = _user_home() / ".config" / "Lazytype"
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        import tempfile
        ROOT = Path(tempfile.gettempdir()) / "Lazytype"   # onschrijfbare APPDATA → temp
        ROOT.mkdir(parents=True, exist_ok=True)
    _old_env = Path(sys.executable).resolve().parent / ".env"
    if _old_env.exists() and not (ROOT / ".env").exists():
        # Migreer alleen API-keys/device, nooit onboarding-staat of hotkey-prefs.
        _no_migrate = {"DICTATE_ONBOARDED", "DICTATE_HOTKEY", "DICTATE_COMMAND_HOTKEY",
                       "DICTATE_TRANSLATE_HOTKEY", "DICTATE_TRANSLATE_TARGET",
                       "DICTATE_LANGUAGE", "DICTATE_POSTPROCESS", "DICTATE_OVERLAY"}
        _lines = [l for l in _old_env.read_text(encoding="utf-8").splitlines()
                  if l.strip() and not l.strip().startswith("#")
                  and l.split("=", 1)[0].strip() not in _no_migrate]
        if _lines:
            (ROOT / ".env").write_text("\n".join(_lines) + "\n", encoding="utf-8")
else:
    ROOT = Path(__file__).resolve().parent


# ── Logging ─────────────────────────────────────────────────────────────
# De gebouwde app draait --windowed (geen console) → print() is onzichtbaar.
# Daarom een roterend logbestand in ROOT, plus stderr (zichtbaar in dev). Dit
# is dé manier om veldfouten te diagnosticeren zonder dat de gebruiker een
# crash-rapport hoeft te plakken.
LOG_FILE = ROOT / "lazytype.log"
log = logging.getLogger("lazytype")


def _setup_logging():
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass
    try:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    except Exception:
        pass
    return log


_setup_logging()


# ── .env laden (zonder externe dependency) ──────────────────────────────
def load_env():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


load_env()

ENGINE = os.environ.get("DICTATE_ENGINE", "groq").lower()

# Platform-specifieke standaard sneltoetsen.
# Windows: Ctrl+Win = dicteren, Ctrl = command, Win+Alt = vertalen (weinig conflicten).
# Mac: Rechter Option = dicteren, Rechter Shift = command, Ctrl+Option = vertalen.
#   BELANGRIJK: de meeste Mac-toetsenborden (MacBook, Magic Keyboard) hebben GEEN
#   rechter Ctrl-toets → een 'ctrl_r'-default is onindrukbaar en lijkt "doet niks".
#   Rechter Option (alt_r) bestaat wél overal en is veilig vast te houden (Option
#   alléén typt niets). De Cmd-toets wordt bewust niet gebruikt (botst met shortcuts).
_DEFAULT_HOTKEY           = "ctrl+win" if IS_WIN else "alt_r"
_DEFAULT_COMMAND_HOTKEY   = "ctrl_r"   if IS_WIN else "shift_r"
_DEFAULT_TRANSLATE_HOTKEY = "win+alt"  if IS_WIN else "ctrl+alt"

HOTKEY_NAME = os.environ.get("DICTATE_HOTKEY") or _DEFAULT_HOTKEY
LANGUAGE = os.environ.get("DICTATE_LANGUAGE") or "nl"
TRAILING_SPACE = os.environ.get("DICTATE_TRAILING_SPACE", "true").lower() in ("1", "true", "yes")
RESTORE_CLIPBOARD = os.environ.get("DICTATE_RESTORE_CLIPBOARD", "true").lower() in ("1", "true", "yes")
SAMPLE_RATE = 16000

GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")
OPENAI_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")

# LLM-nabewerking (opschonen / vertalen) via Groq chat-completions.
GROQ_CHAT_MODEL = os.environ.get("GROQ_CLEANUP_MODEL", "llama-3.3-70b-versatile")
# "off" = uit · "clean" = opschonen in dezelfde taal · taalcode (en/nl/de/…) = vertalen + opschonen
# Standaard "clean": dictaat wordt opgeschoond (haperingen/«eh» weg, interpunctie net).
POSTPROCESS = os.environ.get("DICTATE_POSTPROCESS", "clean").lower()

# Managed abonnement: transcriptie loopt via de Lazytype-proxy (server houdt de
# Groq-key server-side). Vereist een geldige licentiesleutel (LAZYTYPE_LICENSE).
LICENSE = os.environ.get("LAZYTYPE_LICENSE", "")
API_URL = os.environ.get("LAZYTYPE_API", "https://lazytype.com/api/transcribe.php")

# Eigen woordenboek: namen/jargon die Whisper anders verhaspelt. Eén term per
# regel in dictionary.txt; wordt als `prompt` aan Whisper meegegeven (bias).
DICTIONARY_FILE = ROOT / "dictionary.txt"
# Tweede sneltoets voor command mode (selecteer tekst → spreek instructie).
COMMAND_HOTKEY_NAME = os.environ.get("DICTATE_COMMAND_HOTKEY") or _DEFAULT_COMMAND_HOTKEY
TRANSLATE_HOTKEY_NAME = os.environ.get("DICTATE_TRANSLATE_HOTKEY") or _DEFAULT_TRANSLATE_HOTKEY
TRANSLATE_TARGET = os.environ.get("DICTATE_TRANSLATE_TARGET") or "en"


def load_dictionary() -> list[str]:
    try:
        lines = DICTIONARY_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def dictionary_prompt() -> str:
    """Bouw een Whisper-prompt uit de woordenlijst (max ~200 tokens → cap ~600 tekens)."""
    terms = load_dictionary()
    if not terms:
        return ""
    return ("Woordenlijst: " + ", ".join(terms))[:600]


# Snippets: spreek een trigger → plak een vast tekstblok. Eén per regel:
#   trigger = uit te vouwen tekst   (gebruik \n voor een nieuwe regel)
SNIPPETS_FILE = ROOT / "snippets.txt"


def load_snippets() -> dict:
    try:
        lines = SNIPPETS_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    snips = {}
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        trig, _, exp = s.partition("=")
        trig = trig.strip().lower()
        if trig:
            snips[trig] = exp.strip().replace("\\n", "\n")
    return snips


# AI-modi: opgeslagen instructies met een eigen sneltoets. Toets indrukken → de
# instructie wordt op je selectie/laatste dictaat toegepast (geen audio nodig).
MODES_FILE = ROOT / "modes.json"


def load_modes() -> list:
    """Lijst van {"name","instruction","hotkey"}. Lege lijst bij ontbreken/fout."""
    try:
        import json
        data = json.loads(MODES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [{"name": str(m.get("name", "")).strip(),
                     "instruction": str(m.get("instruction", "")).strip(),
                     "hotkey": str(m.get("hotkey", "")).strip()}
                    for m in data if isinstance(m, dict) and m.get("instruction")]
    except Exception:
        pass
    return []


def save_modes(modes: list):
    import json
    clean = [{"name": (m.get("name") or "").strip(),
              "instruction": (m.get("instruction") or "").strip(),
              "hotkey": (m.get("hotkey") or "").strip()}
             for m in modes if (m.get("instruction") or "").strip()]
    MODES_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Gebruiksstatistiek (woorden gedicteerd + bespaarde tijd) ────────────
# Elk geslaagd dictaat wordt als één regel JSON gelogd: {"t": epoch, "n": woorden, "k": soort}.
USAGE_FILE = ROOT / "usage.jsonl"
TYPING_WPM   = 40    # gemiddeld typtempo (woorden/min)
SPEAKING_WPM = 150   # effectief spreektempo met Lazytype (woorden/min)


def _count_words(text: str) -> int:
    return len([w for w in (text or "").split() if w.strip()])


def record_usage(text: str, kind: str = "dictaat"):
    """Log één dictaat-event (tijd, aantal woorden, soort) voor de statistieken."""
    try:
        import json
        n = _count_words(text)
        if n <= 0:
            return
        line = json.dumps({"t": int(time.time()), "n": n, "k": kind or "dictaat"},
                          ensure_ascii=False)
        with open(USAGE_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_usage() -> list:
    out = []
    try:
        import json
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                    if isinstance(d, dict) and "t" in d and "n" in d:
                        out.append(d)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return out


def usage_summary(now: int = 0) -> dict:
    """Aggregaten per periode (7d/30d/365d/all): totaal woorden, per soort, bespaarde tijd (sec)."""
    now = int(now or time.time())
    events = load_usage()
    spans = {"7d": 7 * 86400, "30d": 30 * 86400, "365d": 365 * 86400}
    res = {p: {"words": 0, "by_kind": {}} for p in spans}
    res["all"] = {"words": 0, "by_kind": {}}

    def _add(bucket, k, n):
        bucket["words"] += n
        bucket["by_kind"][k] = bucket["by_kind"].get(k, 0) + n

    for e in events:
        age = now - int(e.get("t", 0))
        n = int(e.get("n", 0))
        k = str(e.get("k", "dictaat")) or "dictaat"
        if n <= 0:
            continue
        _add(res["all"], k, n)
        for p, span in spans.items():
            if 0 <= age <= span:
                _add(res[p], k, n)

    for p in list(spans) + ["all"]:
        w = res[p]["words"]
        saved_min = w * (1.0 / TYPING_WPM - 1.0 / SPEAKING_WPM)   # typtijd − spreektijd
        res[p]["saved_sec"] = max(0, int(saved_min * 60))
    return res


# ── Abonnement & 14-daagse proef ────────────────────────────────────────
TRIAL_DAYS = 14


def _config_dir() -> Path:
    if IS_WIN:
        base = Path(_win_appdata() or os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or _user_home())
    elif IS_MAC:
        base = _user_home() / "Library" / "Application Support"
    else:
        base = _user_home() / ".config"
    return base / "Lazytype"


def _trial_file() -> Path:
    override = os.environ.get("LAZYTYPE_TRIAL_FILE")
    return Path(override) if override else _config_dir() / "trial"


def _trial_start():
    """Geeft de vroegste bekende starttijd terug (Registry + bestand — beide moeten verwijderd worden)."""
    timestamps = []
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Lazytype") as k:
                val, _ = winreg.QueryValueEx(k, "TrialStart")
                timestamps.append(float(val))
        except Exception:
            pass
    try:
        timestamps.append(float(_trial_file().read_text(encoding="utf-8").strip()))
    except Exception:
        pass
    return min(timestamps) if timestamps else None


def start_trial_if_needed():
    if _trial_start() is None:
        ts = str(int(time.time()))
        if IS_WIN:
            try:
                import winreg
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Lazytype") as k:
                    winreg.SetValueEx(k, "TrialStart", 0, winreg.REG_SZ, ts)
            except Exception:
                pass
        try:
            f = _trial_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(ts, encoding="utf-8")
        except Exception:
            pass


def trial_days_left() -> int:
    ts = _trial_start()
    if ts is None:
        return TRIAL_DAYS
    secs_left = ts + TRIAL_DAYS * 86400 - time.time()
    if secs_left <= 0:
        return 0
    return int((secs_left + 86399) // 86400)   # ceil naar hele dagen


_license_server_ok = None  # None=ongecontroleerd, True=ok, False=afgewezen door server


def _verify_cache_ok(key: str) -> bool:
    """True als er een recente (<7 dagen) server-verificatie gecached is voor deze sleutel."""
    try:
        import hashlib
        kh = hashlib.sha256(key.encode()).hexdigest()[:16]
        parts = (_config_dir() / "verify_cache").read_text(encoding="utf-8").strip().split(":")
        return len(parts) == 2 and parts[0] == kh and time.time() - float(parts[1]) < 7 * 86400
    except Exception:
        return False


def _verify_cache_save(key: str):
    try:
        import hashlib
        kh = hashlib.sha256(key.encode()).hexdigest()[:16]
        f = _config_dir() / "verify_cache"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"{kh}:{int(time.time())}", encoding="utf-8")
    except Exception:
        pass


def verify_personal_key():
    """Achtergrond: HMAC-verificeer + device-binding bij de server (verify.php).
    Bij netwerk fout: vertrouw cache als die ≤7 dagen oud is (offline-vriendelijk).
    Bij expliciete afwijzing (verkeerd device, verlopen): cache wissen + blokkeren."""
    global _license_server_ok
    key = (os.environ.get("LAZYTYPE_LICENSE") or LICENSE).strip()
    if not key:
        return
    p = license_payload()
    if not p or p.get("tier") not in ("personal", "pro", "trial"):
        return
    try:
        import requests
        base = API_URL.rsplit("/", 1)[0]
        r = requests.post(f"{base}/verify.php", data={
            "license": key,
            "device":  ensure_device_id(),
        }, timeout=10)
        ok = r.status_code == 200 and bool(r.json().get("ok"))
        _license_server_ok = ok
        if ok:
            _verify_cache_save(key)
        else:
            try:
                (_config_dir() / "verify_cache").unlink()
            except Exception:
                pass
    except Exception:
        _license_server_ok = _verify_cache_ok(key)


def license_payload():
    """Decodeer de huidige sleutel (alleen weergave; de server verifieert echt)."""
    key = (os.environ.get("LAZYTYPE_LICENSE") or LICENSE).strip()
    if not key:
        return None
    try:
        import base64
        import json as _json
        _prefix, pb, _sig = key.split(".")
        return _json.loads(base64.urlsafe_b64decode(pb + "=" * (-len(pb) % 4)))
    except Exception:
        return None


def license_state() -> dict:
    """Effectieve toegang: {tier, valid, managed, days_left, label}.
    Owner (geheim aanwezig) = onbeperkt. Geldige Personal/Pro-sleutel telt; anders
    een lokale 14-daagse proef (BYOK), daarna geblokkeerd tot aankoop."""
    p = license_payload()
    if p and p.get("tier") in ("personal", "pro", "trial", "lifetime"):
        exp = int(p.get("exp", 0) or 0)
        if (not exp or time.time() <= exp) and _license_server_ok is not False:
            tier = p["tier"]
            managed = tier in ("pro", "trial", "lifetime")
            when = "" if not exp else " tot " + time.strftime("%Y-%m-%d", time.localtime(exp))
            labels = {"pro": "Pro", "personal": "Personal", "trial": "Proef", "lifetime": "Lifetime"}
            return {"tier": tier, "valid": True, "managed": managed,
                    "days_left": None, "label": labels.get(tier, tier) + when}
    # Fallback: lokale proef zonder sleutel (BYOK, eigen Groq-key vereist)
    start_trial_if_needed()
    left = trial_days_left()
    if left > 0:
        return {"tier": "trial", "valid": True, "managed": False, "days_left": left,
                "label": f"Proef — nog {left} dag" + ("en" if left != 1 else "")}
    return {"tier": "trial", "valid": False, "managed": False, "days_left": 0,
            "label": "Proef verlopen — koop een abonnement op lazytype.com"}


def check_access(engine: str = None):
    """Poortwachter vóór elke dictatie. Raise → de tray toont de melding."""
    st = license_state()
    if not st["valid"]:
        raise RuntimeError("Proef verlopen — koop Personal of Pro op lazytype.com")
    if (engine or ENGINE) == "managed" and not st["managed"]:
        raise RuntimeError("Managed transcriptie vereist een Pro-abonnement.")
    return st


# ── Geschiedenis van dictaten (lokaal, voor terug-kopiëren) ─────────────
HISTORY_MAX = 25
HISTORY_ENABLED = os.environ.get("DICTATE_HISTORY", "1").lower() in ("1", "true", "yes", "on")


def _history_file() -> Path:
    override = os.environ.get("LAZYTYPE_HISTORY_FILE")
    return Path(override) if override else _config_dir() / "history.jsonl"


def add_history(text: str):
    """Voeg een dictaat toe (lokaal, max HISTORY_MAX). Stilletjes falen mag."""
    text = (text or "").strip()
    if not text:
        return
    try:
        import json as _json
        f = _history_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
        lines.append(_json.dumps({"t": int(time.time()), "text": text}, ensure_ascii=False))
        f.write_text("\n".join(lines[-HISTORY_MAX:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_history() -> list:
    """Lijst dicteer-items, meest recente eerst."""
    try:
        import json as _json
        out = []
        for ln in _history_file().read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln:
                try:
                    out.append(_json.loads(ln))
                except Exception:
                    pass
        return list(reversed(out))
    except Exception:
        return []


def clear_history():
    try:
        _history_file().unlink()
    except Exception:
        pass


# ── Hulp: int16-frames → WAV-bytes ──────────────────────────────────────
def frames_to_wav(frames: list[bytes], sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()


# ── Transcriptie-engines ────────────────────────────────────────────────
def transcribe_groq(wav_bytes: bytes, language: str) -> str:
    import requests

    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY ontbreekt in .env")
    data = {"model": GROQ_MODEL, "response_format": "text", "temperature": "0"}
    if language and language != "auto":
        data["language"] = language
    prompt = dictionary_prompt()
    if prompt:
        data["prompt"] = prompt
    r = requests.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        data=data,
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"Groq-fout {r.status_code}: {r.text[:300]}")
    return r.text.strip()


def transcribe_openai(wav_bytes: bytes, language: str) -> str:
    import requests

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY ontbreekt in .env")
    data = {"model": OPENAI_MODEL, "response_format": "text"}
    if language and language != "auto":
        data["language"] = language
    prompt = dictionary_prompt()
    if prompt:
        data["prompt"] = prompt
    r = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        data=data,
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"OpenAI-fout {r.status_code}: {r.text[:300]}")
    return r.text.strip()


def transcribe_local(wav_bytes: bytes, language: str) -> str:
    """Offline fallback via de al-draaiende whisper-server (zie server.js)."""
    import requests

    port = os.environ.get("WHISPER_SERVER_PORT", "8178")
    data = {"response_format": "json", "language": language or "auto"}
    prompt = dictionary_prompt()
    if prompt:
        data["prompt"] = prompt
    r = requests.post(
        f"http://127.0.0.1:{port}/inference",
        files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        data=data,
        timeout=120,
    )
    if not r.ok:
        raise RuntimeError(f"Lokale engine-fout {r.status_code}: {r.text[:200]}")
    return (r.json().get("text") or "").strip()


def _hardware_id() -> str:
    """Stabiele, anonieme machine-id afgeleid van hardware/OS — gelijk ná een
    herinstallatie. Voorkomt dat elke herinstallatie een NIEUW device-slot verbruikt
    (de oude os.urandom-aanpak putte de 2-apparaten-limiet uit). Lege string als het
    niet lukt → dan valt ensure_device_id terug op een willekeurige id."""
    raw = ""
    try:
        if IS_WIN:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as k:
                raw, _ = winreg.QueryValueEx(k, "MachineGuid")
        elif IS_MAC:
            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], text=True)
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            raw = m.group(1) if m else ""
    except Exception:
        raw = ""
    if not raw:
        return ""
    import hashlib
    return hashlib.sha256(("lazytype:" + raw.strip()).encode()).hexdigest()[:16]


def ensure_device_id() -> str:
    """Stabiele, anonieme device-id voor device-binding. Bij voorkeur afgeleid van
    de hardware (overleeft herinstallaties); anders eenmalig willekeurig."""
    did = os.environ.get("LAZYTYPE_DEVICE", "").strip()
    stable = _hardware_id()
    if stable:
        # Migreer bestaande (willekeurige) id's naar de stabiele hardware-id, zodat
        # een herinstallatie hetzelfde slot hergebruikt i.p.v. een nieuw te claimen.
        if did != stable:
            try:
                save_env_value("LAZYTYPE_DEVICE", stable)
            except Exception:
                pass
            os.environ["LAZYTYPE_DEVICE"] = stable
        return stable
    if not did:
        did = os.urandom(8).hex()
        try:
            save_env_value("LAZYTYPE_DEVICE", did)
        except Exception:
            pass
        os.environ["LAZYTYPE_DEVICE"] = did
    return did


def transcribe_managed(wav_bytes: bytes, language: str = "auto", postprocess: str = "off",
                       command: str = "", context: str = "") -> str:
    """Abonnement: stuur audio naar de Lazytype-proxy, die met de server-side
    Groq-key transcribeert én (optioneel) vertaalt/opschoont/command toepast."""
    import requests

    key = (os.environ.get("LAZYTYPE_LICENSE") or LICENSE).strip()
    if not key:
        raise RuntimeError("Geen licentiesleutel — vul je abonnement in (menu: Abonnement).")
    data = {"license": key, "language": language or "auto",
            "postprocess": postprocess or "off", "device": ensure_device_id()}
    prompt = dictionary_prompt()
    if prompt:
        data["prompt"] = prompt
    if command:
        data["command"] = command
    if context:
        data["context"] = context
    r = requests.post(
        os.environ.get("LAZYTYPE_API", API_URL),
        files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        data=data,
        timeout=90,
    )
    if r.status_code in (402, 403, 413, 429):
        try:
            msg = r.json().get("error", "abonnement ongeldig")
        except Exception:
            msg = "abonnement ongeldig of verlopen"
        raise RuntimeError(msg)
    if not r.ok:
        raise RuntimeError(f"Managed-fout {r.status_code}: {r.text[:200]}")
    return (r.json().get("text") or "").strip()


ENGINES = {"groq": transcribe_groq, "openai": transcribe_openai, "local": transcribe_local}


def transcribe(wav_bytes: bytes, engine: str = ENGINE, language: str = LANGUAGE) -> str:
    fn = ENGINES.get(engine)
    if not fn:
        raise RuntimeError(f"Onbekende engine: {engine}")
    return fn(wav_bytes, language).strip()


# Gesproken commando's die het hele dictaat vervangen (handig bij hold-to-talk).
NEWLINE_COMMANDS = {
    "nieuwe regel": "\n",
    "volgende regel": "\n",
    "new line": "\n",
    "enter": "\n",
    "nieuwe alinea": "\n\n",
    "nieuwe paragraaf": "\n\n",
    "new paragraph": "\n\n",
}

# Gesproken ACTIES (geen tekst): wis het vorige dictaat.
UNDO_COMMANDS = {"scratch that", "verwijder dat", "wis dat", "schrap dat",
                 "delete that", "wis dit"}


def voice_action(text: str):
    """Herken een gesproken actie. Geeft 'undo' om het vorige dictaat te wissen, anders None."""
    key = re.sub(r"[.,!?]+$", "", (text or "").strip().lower()).strip()
    return "undo" if key in UNDO_COMMANDS else None


def delete_last(n: int):
    """Stuur n keer Backspace — wist het zojuist geplakte dictaat ('scratch that')."""
    if n <= 0:
        return
    n = min(int(n), 2000)   # veiligheidslimiet
    if IS_WIN:
        import ctypes
        u32 = ctypes.windll.user32
        VK_BACK, KEYEVENTF_UP = 0x08, 0x0002
        for _ in range(n):
            u32.keybd_event(VK_BACK, 0, 0, 0)
            u32.keybd_event(VK_BACK, 0, KEYEVENTF_UP, 0)
            time.sleep(0.003)
    else:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        for _ in range(n):
            kb.press(Key.backspace); kb.release(Key.backspace)
            time.sleep(0.003)


def _literal_expansion(text: str):
    """Geeft de letterlijke vervanging als de tekst exact een opmaakcommando of
    snippet-trigger is (interpunctie die Whisper toevoegt wordt genegeerd); anders None.
    Zulke uitvoer is letterlijk en mag NIET door de AI-nabewerking."""
    key = re.sub(r"[.,!?]+$", "", (text or "").strip().lower()).strip()
    if not key:
        return None
    if key in NEWLINE_COMMANDS:
        return NEWLINE_COMMANDS[key]
    snippets = load_snippets()
    if key in snippets:
        return snippets[key]
    return None


def finalize_text(text: str) -> str:
    """Slimme afronding: opmaakcommando's + snippets + whitespace opschonen."""
    t = (text or "").strip()
    if not t:
        return ""
    lit = _literal_expansion(t)
    if lit is not None:
        return lit
    # dubbele spaties/whitespace netjes maken
    t = re.sub(r"[ \t]+", " ", t).strip()
    return t


# ── LLM-nabewerking: opschonen en/of vertalen via Groq chat ─────────────
LANG_NAMES = {
    "nl": "Dutch", "en": "English", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pt": "Portuguese",
}


def postprocess_text(text: str, mode: str = None) -> str:
    """Optionele nabewerking met een Groq-LLM.

    mode:
      "off"/""  → tekst ongewijzigd teruggeven
      "clean"   → stopwoorden/haperingen weg + interpunctie fixen, ZELFDE taal
      taalcode  → vertalen naar die taal (en meteen opschonen), bv. "en"

    Faalt stil: bij ontbrekende key, netwerk- of API-fout komt de
    oorspronkelijke tekst terug, zodat dicteren nooit blokkeert op deze extra stap.
    """
    mode = (mode if mode is not None else POSTPROCESS or "off").lower()
    text = text or ""
    if mode in ("", "off") or not text.strip():
        return text
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return text

    if mode == "clean":
        system = (
            "You polish raw speech-to-text dictation. Remove filler words, false "
            "starts, repetitions and stutters; fix punctuation, capitalization and "
            "obvious mis-transcriptions. Keep the EXACT same language as the input — "
            "never translate. Preserve the original meaning and tone. Do not add, "
            "summarize, explain or answer anything. Output ONLY the cleaned text."
        )
    else:
        target = LANG_NAMES.get(mode, mode)
        system = (
            f"You post-process raw speech-to-text dictation. Translate it into {target}. "
            "Also clean it up: drop filler words, false starts and stutters, and fix "
            "punctuation and capitalization so it reads naturally for a native speaker. "
            "Preserve the original meaning and tone. Do not add, summarize, explain or "
            f"answer anything. Output ONLY the final {target} text, nothing else."
        )

    try:
        import requests

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": GROQ_CHAT_MODEL,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            },
            timeout=30,
        )
        if not r.ok:
            return text
        out = (r.json()["choices"][0]["message"]["content"] or "").strip()
        return out or text
    except Exception:
        return text


# ── Volledige pijplijn: transcriberen → afronden → nabewerken ───────────
def run_pipeline(wav_bytes: bytes, engine: str = None, language: str = None,
                 postprocess: str = None, context: str = "") -> str:
    """Eén ingang voor alle aanroepers (daemon, CLI, tray). Bij de managed-engine
    doet de server het nabewerken; anders gebeurt dat lokaal.
    context = categorie van de actieve app (email/chat/code) voor toon-aanpassing."""
    engine = engine or ENGINE
    check_access(engine)
    language = language if language is not None else LANGUAGE
    mode = (postprocess if postprocess is not None else POSTPROCESS) or "off"
    if engine == "managed":
        return finalize_text(transcribe_managed(wav_bytes, language, mode, context=context))
    raw = transcribe(wav_bytes, engine, language)
    if _literal_expansion(raw) is not None:
        return finalize_text(raw)          # commando/snippet → letterlijk, geen nabewerking
    text = finalize_text(raw)
    if text.strip():
        text = postprocess_text(text, mode)
    return text


# ── Command mode: selecteer tekst → spreek instructie → herschrijf ──────
def apply_command(instruction: str, text: str) -> str:
    """Pas een gesproken instructie toe op tekst via Groq chat (eigen key)."""
    instruction = (instruction or "").strip()
    if not instruction:
        return text
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("Command mode vereist een Groq-key of een abonnement (managed).")
    system = ("You edit text according to a spoken instruction. Apply the instruction to the "
              "user's text and output ONLY the resulting text — no preamble, no quotes, no "
              "explanation. Keep the original language unless the instruction says otherwise.")
    try:
        import requests

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": GROQ_CHAT_MODEL, "temperature": 0,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": f"Instruction: {instruction}\n\nText:\n{text}"}]},
            timeout=30,
        )
        if not r.ok:
            return text
        out = (r.json()["choices"][0]["message"]["content"] or "").strip()
        return out or text
    except Exception:
        return text


def copy_selection() -> str:
    """Kopieer de huidige selectie (Ctrl/Cmd+C) en lees 'm van het klembord."""
    from pynput.keyboard import Controller, Key

    kb = Controller()
    prev = _clipboard_get()
    _clipboard_set("\x00")           # sentinel om 'niets geselecteerd' te detecteren
    time.sleep(0.05)
    mod = Key.cmd if IS_MAC else Key.ctrl
    with kb.pressed(mod):
        kb.press("c")
        kb.release("c")
    time.sleep(0.15)
    sel = _clipboard_get() or ""
    if sel == "\x00":                # er was niets geselecteerd
        if prev is not None:
            _clipboard_set(prev)
        return ""
    return sel.strip()


def transform_command(wav_bytes: bytes, selected_text: str, language: str = None,
                      engine: str = None) -> str:
    """Transcribeer de gesproken instructie en pas 'm toe op de geselecteerde tekst."""
    engine = engine or ENGINE
    check_access(engine)
    language = language if language is not None else LANGUAGE
    if engine == "managed":
        return transcribe_managed(wav_bytes, language, "off", command=selected_text)
    instruction = finalize_text(transcribe(wav_bytes, engine, language))
    return apply_command(instruction, selected_text)


def _command_text_managed(instruction: str, text: str) -> str:
    """AI-mode (sneltoets): pas een TEKST-instructie toe op tekst via de server — geen audio."""
    import requests
    key = (os.environ.get("LAZYTYPE_LICENSE") or LICENSE).strip()
    if not key:
        raise RuntimeError("Geen licentiesleutel — vul je abonnement in (menu: Abonnement).")
    r = requests.post(
        os.environ.get("LAZYTYPE_API", API_URL),
        data={"license": key, "device": ensure_device_id(),
              "instruction": instruction, "command": text},
        timeout=60,
    )
    if r.status_code in (402, 403, 413, 429):
        try:
            raise RuntimeError(r.json().get("error", "abonnement ongeldig"))
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("abonnement ongeldig of verlopen")
    if not r.ok:
        raise RuntimeError(f"Mode-fout {r.status_code}: {r.text[:200]}")
    return (r.json().get("text") or "").strip()


def run_command_text(instruction: str, text: str, engine: str = None) -> str:
    """Pas een opgeslagen AI-mode-instructie toe op tekst (zonder audio)."""
    engine = engine or ENGINE
    check_access(engine)
    if engine == "managed":
        return _command_text_managed(instruction, text)
    return apply_command(instruction, text)   # eigen Groq-key


# ── API-key opslaan in .env ─────────────────────────────────────────────
def save_env_value(name: str, value: str):
    env_path = ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{name}="
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{name}={value}"
            found = True
            break
    if not found:
        lines.insert(0, f"{name}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[name] = value


# ── Tekst in de actieve app plakken ─────────────────────────────────────
def _clipboard_get():
    """Lees klembord met retries; None als het niet lukt."""
    import pyperclip

    for _ in range(8):
        try:
            return pyperclip.paste()
        except Exception:
            time.sleep(0.05)
    return None


def _clipboard_set(text: str) -> bool:
    """Zet klembord met retries (Windows OpenClipboard faalt soms door contention)."""
    import pyperclip

    for _ in range(12):
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            time.sleep(0.05)
    return False


def _send_ctrl_v():
    """Stuur Ctrl+V ATOMAIR via SendInput. Losse keybd_event-calls met korte sleeps
    laten de 'V' soms vóór de Ctrl aankomen → er verschijnt een losse letter 'v'
    achter de geplakte tekst. SendInput plaatst Ctrl↓ V↓ V↑ Ctrl↑ als één blok in
    de invoerwachtrij, in volgorde, zonder dat fysieke input ertussen kan komen.
    Valt terug op keybd_event (met ruimere sleeps) als SendInput faalt."""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    ULONG_PTR = wintypes.WPARAM           # pointer-groot op 32- én 64-bit
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL, VK_V = 0x11, 0x56

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR))

    class MOUSEINPUT(ctypes.Structure):   # alleen aanwezig voor de juiste union-grootte
        _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR))

    class _U(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT))

    class INPUT(ctypes.Structure):
        _fields_ = (("type", wintypes.DWORD), ("u", _U))

    def _ev(vk, up):
        e = INPUT(); e.type = 1            # INPUT_KEYBOARD
        e.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
        return e

    try:
        seq = (INPUT * 4)(_ev(VK_CONTROL, False), _ev(VK_V, False),
                          _ev(VK_V, True), _ev(VK_CONTROL, True))
        u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u32.SendInput.restype = wintypes.UINT
        if u32.SendInput(4, seq, ctypes.sizeof(INPUT)) == 4:
            return
    except Exception:
        pass
    # Fallback: losse calls met ruimere sleeps (Ctrl zeker eerst geregistreerd).
    u32.keybd_event(VK_CONTROL, 0, 0, 0);            time.sleep(0.04)
    u32.keybd_event(VK_V,       0, 0, 0);            time.sleep(0.02)
    u32.keybd_event(VK_V,       0, KEYEVENTF_KEYUP, 0); time.sleep(0.04)
    u32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def _paste_win32(text: str):
    """Plak via directe Win32-calls (betrouwbaarder dan pyperclip + pynput in frozen exe)."""
    import ctypes
    from ctypes import wintypes, c_void_p, c_size_t
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32

    # KRITIEK op 64-bit Windows: zonder expliciete restype kapt ctypes de
    # teruggegeven HANDLE/pointer af tot 32-bit → corrupt geheugenadres → crash
    # of falende paste. Declareer daarom alle pointer-/handle-types expliciet.
    k32.GlobalAlloc.restype  = c_void_p
    k32.GlobalAlloc.argtypes = [wintypes.UINT, c_size_t]
    k32.GlobalLock.restype   = c_void_p
    k32.GlobalLock.argtypes  = [c_void_p]
    k32.GlobalUnlock.argtypes = [c_void_p]
    u32.SetClipboardData.restype  = c_void_p
    u32.SetClipboardData.argtypes = [wintypes.UINT, c_void_p]
    u32.OpenClipboard.argtypes    = [c_void_p]

    previous = _clipboard_get() if RESTORE_CLIPBOARD else None

    # Klembord instellen via Win32 (CF_UNICODETEXT)
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE  = 0x0002
    data = text.encode("utf-16-le") + b"\x00\x00"
    ok = False
    for _ in range(12):
        try:
            if u32.OpenClipboard(None):
                try:
                    u32.EmptyClipboard()
                    h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                    if h:
                        ptr = k32.GlobalLock(h)
                        if ptr:
                            ctypes.memmove(ptr, data, len(data))
                            k32.GlobalUnlock(h)
                            # Eigenaarschap van h gaat over naar het systeem; NIET GlobalFree'en.
                            if u32.SetClipboardData(CF_UNICODETEXT, h):
                                ok = True
                finally:
                    u32.CloseClipboard()
                if ok:
                    break
        except Exception:
            pass
        time.sleep(0.05)

    if not ok:
        print("  (klembord mislukt — tekst wordt getypt)")
        try:
            from pynput.keyboard import Controller
            Controller().type(text)
        except Exception as e:
            print(f"  (type ook mislukt: {e})")
        return

    time.sleep(0.05)

    # Ctrl+V atomair (voorkomt de losse 'v' achter de tekst — zie _send_ctrl_v).
    _send_ctrl_v()

    if RESTORE_CLIPBOARD and previous is not None and previous != text:
        def _restore():
            time.sleep(0.6)
            _clipboard_set(previous)
        threading.Thread(target=_restore, daemon=True).start()


def paste_text(text: str):
    if not text:
        return
    if TRAILING_SPACE and not text.endswith(" "):
        text += " "

    if IS_WIN:
        _paste_win32(text)
        return

    # macOS: pyperclip + pynput Cmd+V
    from pynput.keyboard import Controller, Key
    kb = Controller()
    previous = _clipboard_get() if RESTORE_CLIPBOARD else None
    if _clipboard_set(text):
        time.sleep(0.05)
        with kb.pressed(Key.cmd):
            kb.press("v")
            kb.release("v")
        if RESTORE_CLIPBOARD and previous is not None:
            def restore():
                time.sleep(0.6)
                _clipboard_set(previous)
            threading.Thread(target=restore, daemon=True).start()
    else:
        print("  (klembord bezet — tekst wordt getypt)")
        kb.type(text)


# ── Geluidsfeedback (cross-platform) ────────────────────────────────────
_WIN_BEEP = {"start": (1050, 120), "stop": (540, 110), "done": (880, 120), "error": (330, 280)}
_MAC_SOUND = {"start": "Tink", "stop": "Pop", "done": "Pop", "error": "Basso"}


_BEEP_RATE = 22050


def _make_tone_pcm(freq: int, dur_ms: int, volume: float = 0.5, rate: int = _BEEP_RATE) -> bytes:
    """Bouw een kort sine-toontje als ruwe 16-bit mono PCM, met fade-in/out tegen klikken."""
    import struct
    n = max(1, int(rate * dur_ms / 1000))
    fade = max(1, int(n * 0.08))
    out = bytearray()
    for i in range(n):
        amp = volume
        if i < fade:
            amp *= i / fade
        elif i > n - fade:
            amp *= (n - i) / fade
        out += struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)))
    return bytes(out)


def _pcm_to_wav(pcm: bytes, rate: int = _BEEP_RATE) -> bytes:
    import struct
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


_TONE_PCM_CACHE: dict = {}


def beep(kind: str):
    """Speel een korte feedbacktoon.

    Primair via sounddevice (PortAudio) — exact hetzelfde audiopad als de
    mic-opname, dus betrouwbaar hoorbaar. winsound.Beep() (PC-speaker) en
    PlaySound bleken op moderne laptops vaak onhoorbaar; sounddevice niet.
    """
    if IS_MAC:
        try:
            name = _MAC_SOUND.get(kind, "Tink")
            subprocess.Popen(
                ["afplay", f"/System/Library/Sounds/{name}.aiff"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        return

    freq, dur = _WIN_BEEP.get(kind, (880, 80))
    pcm = _TONE_PCM_CACHE.get(kind)
    if pcm is None:
        pcm = _make_tone_pcm(freq, dur)
        _TONE_PCM_CACHE[kind] = pcm

    # 1) sounddevice — zelfde uitvoer-subsysteem als de opname (betrouwbaar)
    try:
        import sounddevice as sd
        s = sd.RawOutputStream(samplerate=_BEEP_RATE, channels=1, dtype="int16")
        s.start()
        s.write(pcm)
        s.stop()
        s.close()
        return
    except Exception:
        pass

    # 2) winsound-fallback (Windows) als sounddevice-output niet beschikbaar is
    try:
        import winsound
        winsound.PlaySound(_pcm_to_wav(pcm), winsound.SND_MEMORY | winsound.SND_ASYNC)
    except Exception:
        pass


# ── Microfoonopname ─────────────────────────────────────────────────────
class Recorder:
    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.frames: list[bytes] = []
        self.stream = None
        self.last_level = 0.0   # live mic-niveau 0..1 (voor de overlay-waveform)

    def start(self):
        import sounddevice as sd
        import array

        self.frames = []
        self.last_level = 0.0

        def cb(indata, frames, time_info, status):
            b = bytes(indata)
            self.frames.append(b)
            a = array.array("h")
            a.frombytes(b)
            peak = 0
            for i in range(0, len(a), 16):   # gestapt samplen → goedkoop
                v = a[i] if a[i] >= 0 else -a[i]
                if v > peak:
                    peak = v
            self.last_level = peak / 32768.0

        self.stream = sd.RawInputStream(
            samplerate=self.sample_rate, channels=1, dtype="int16", callback=cb
        )
        self.stream.start()

    def stop(self) -> bytes:
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.last_level = 0.0
        return frames_to_wav(self.frames, self.sample_rate)

    def snapshot(self) -> bytes:
        """WAV-bytes van de audio tot nu toe, ZONDER de opname te stoppen
        (voor de realtime preview)."""
        return frames_to_wav(list(self.frames), self.sample_rate)

    def duration(self) -> float:
        total = sum(len(f) for f in self.frames) / 2
        return total / self.sample_rate


# ── Hotkey naar pynput-key ──────────────────────────────────────────────
def resolve_hotkey(name: str):
    from pynput.keyboard import Key, KeyCode

    name = name.strip().lower()
    if hasattr(Key, name):
        return getattr(Key, name)
    if len(name) == 1:
        return KeyCode.from_char(name)
    raise RuntimeError(f"Onbekende hotkey '{name}'. Probeer bijv. ctrl_r, f9, alt_r.")


# ── De dicteer-daemon ───────────────────────────────────────────────────
def run_daemon():
    from pynput import keyboard

    hotkey = resolve_hotkey(HOTKEY_NAME)
    recorder = Recorder()
    state = {"recording": False, "working": False}

    def banner():
        print("=" * 58)
        print("  🎙️  Lazytype is actief")
        print(f"  Engine   : {ENGINE}  ({GROQ_MODEL if ENGINE=='groq' else OPENAI_MODEL if ENGINE=='openai' else 'lokaal'})")
        print(f"  Taal     : {LANGUAGE}")
        print(f"  Sneltoets: HOUD '{HOTKEY_NAME}' ingedrukt, spreek, laat los")
        print(f"  Stoppen  : Ctrl+C in dit venster")
        print("=" * 58)

    def handle_release():
        """Loopt in een thread: opname stoppen, transcriberen, plakken."""
        try:
            wav = recorder.stop()
            secs = recorder.duration()
            beep("stop")
            if secs < 0.3:
                print("  (te kort, genegeerd)")
                return
            print(f"  ⏳ {secs:.1f}s opgenomen — transcriberen…")
            t0 = time.time()
            text = run_pipeline(wav)
            dt = time.time() - t0
            if not text:
                print("  (geen spraak herkend)")
                beep("error")
                return
            print(f"  ✅ ({dt:.2f}s) → {text!r}")
            paste_text(text)
            beep("done")
            if HISTORY_ENABLED:
                add_history(text)
        except Exception as e:
            print(f"  ⚠️  {e}")
            beep("error")
        finally:
            state["working"] = False

    def on_press(key):
        if key == hotkey and not state["recording"] and not state["working"]:
            state["recording"] = True
            recorder.start()
            beep("start")
            print("  🔴 Opname… (laat de toets los om te stoppen)")

    def on_release(key):
        if key == hotkey and state["recording"]:
            state["recording"] = False
            state["working"] = True
            threading.Thread(target=handle_release, daemon=True).start()

    banner()
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


# ── CLI ─────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]

    if args and args[0] == "--devices":
        import sounddevice as sd

        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(i, d["name"])
        return

    if args and args[0] == "--test":
        if len(args) < 2:
            print("Gebruik: python dictate.py --test pad/naar/audio.wav")
            return
        wav_bytes = Path(args[1]).read_bytes()
        t0 = time.time()
        text = run_pipeline(wav_bytes)
        print(f"[{ENGINE} · {time.time()-t0:.2f}s] {text}")
        return

    if args and args[0] == "--check":
        import sounddevice as sd

        print("Opname van 3 seconden… spreek nu.")
        rec = Recorder()
        rec.start()
        time.sleep(3)
        wav = rec.stop()
        print(f"Opgenomen: {rec.duration():.1f}s, {len(wav)} bytes")
        t0 = time.time()
        text = run_pipeline(wav)
        print(f"[{ENGINE} · {time.time()-t0:.2f}s] {text!r}")
        return

    run_daemon()


if __name__ == "__main__":
    main()
