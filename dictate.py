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
import time
import wave
import subprocess
import threading
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

# Als 'frozen' (PyInstaller .exe) staat .env naast de exe, niet in de temp-map.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent


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
# Windows: Ctrl+Win = dicteren, Win+Alt = vertalen (comfortabel, weinig conflicten).
# Mac: Rechter Ctrl = dicteren, Rechter Option = vertalen (minst bezet op macOS).
_DEFAULT_HOTKEY           = "ctrl+win" if IS_WIN else "ctrl_r"
_DEFAULT_COMMAND_HOTKEY   = "ctrl_r"   if IS_WIN else "alt_r"
_DEFAULT_TRANSLATE_HOTKEY = "win+alt"  if IS_WIN else "alt_r"

HOTKEY_NAME = os.environ.get("DICTATE_HOTKEY", _DEFAULT_HOTKEY)
LANGUAGE = os.environ.get("DICTATE_LANGUAGE", "nl")
TRAILING_SPACE = os.environ.get("DICTATE_TRAILING_SPACE", "true").lower() in ("1", "true", "yes")
RESTORE_CLIPBOARD = os.environ.get("DICTATE_RESTORE_CLIPBOARD", "true").lower() in ("1", "true", "yes")
SAMPLE_RATE = 16000

GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")
OPENAI_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")

# LLM-nabewerking (opschonen / vertalen) via Groq chat-completions.
GROQ_CHAT_MODEL = os.environ.get("GROQ_CLEANUP_MODEL", "llama-3.3-70b-versatile")
# "off" = uit · "clean" = opschonen in dezelfde taal · taalcode (en/nl/de/…) = vertalen + opschonen
POSTPROCESS = os.environ.get("DICTATE_POSTPROCESS", "off").lower()

# Managed abonnement: transcriptie loopt via de Lazytype-proxy (server houdt de
# Groq-key server-side). Vereist een geldige licentiesleutel (LAZYTYPE_LICENSE).
LICENSE = os.environ.get("LAZYTYPE_LICENSE", "")
API_URL = os.environ.get("LAZYTYPE_API", "https://lazytype.com/api/transcribe.php")

# Eigen woordenboek: namen/jargon die Whisper anders verhaspelt. Eén term per
# regel in dictionary.txt; wordt als `prompt` aan Whisper meegegeven (bias).
DICTIONARY_FILE = ROOT / "dictionary.txt"
# Tweede sneltoets voor command mode (selecteer tekst → spreek instructie).
COMMAND_HOTKEY_NAME = os.environ.get("DICTATE_COMMAND_HOTKEY", _DEFAULT_COMMAND_HOTKEY)
TRANSLATE_HOTKEY_NAME = os.environ.get("DICTATE_TRANSLATE_HOTKEY", _DEFAULT_TRANSLATE_HOTKEY)
TRANSLATE_TARGET = os.environ.get("DICTATE_TRANSLATE_TARGET", "en")


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


# ── Abonnement & 14-daagse proef ────────────────────────────────────────
TRIAL_DAYS = 14


def _config_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    return base / "Lazytype"


def _trial_file() -> Path:
    override = os.environ.get("LAZYTYPE_TRIAL_FILE")
    return Path(override) if override else _config_dir() / "trial"


def _trial_start():
    try:
        return float(_trial_file().read_text(encoding="utf-8").strip())
    except Exception:
        return None


def start_trial_if_needed():
    if _trial_start() is None:
        try:
            f = _trial_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(str(int(time.time())), encoding="utf-8")
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

def verify_personal_key():
    """Achtergrond: HMAC-verificeer Personal/Pro-sleutel bij de server (verify.php).
    Zo is de lokale decode-zonder-HMAC-check (honor-system) niet de enige barrière."""
    global _license_server_ok
    key = (os.environ.get("LAZYTYPE_LICENSE") or LICENSE).strip()
    if not key:
        return
    p = license_payload()
    if not p or p.get("tier") not in ("personal", "pro"):
        return
    try:
        import requests
        base = API_URL.rsplit("/", 1)[0]
        r = requests.post(f"{base}/verify.php", data={"license": key}, timeout=10)
        _license_server_ok = r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        _license_server_ok = True  # netwerk fout → fail-open, vertrouw lokale check


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
    if os.environ.get("LAZYTYPE_LICENSE_SECRET"):
        return {"tier": "owner", "valid": True, "managed": True,
                "days_left": None, "label": "Owner (onbeperkt)"}
    p = license_payload()
    if p and p.get("tier") in ("personal", "pro"):
        exp = int(p.get("exp", 0) or 0)
        if (not exp or time.time() <= exp) and _license_server_ok is not False:
            managed = p["tier"] == "pro"
            when = "" if not exp else " tot " + time.strftime("%Y-%m-%d", time.localtime(exp))
            return {"tier": p["tier"], "valid": True, "managed": managed,
                    "days_left": None, "label": ("Pro" if managed else "Personal") + when}
    start_trial_if_needed()
    left = trial_days_left()
    if left > 0:
        return {"tier": "trial", "valid": True, "managed": False, "days_left": left,
                "label": f"Proef — nog {left} dag" + ("en" if left != 1 else "")}
    return {"tier": "trial", "valid": False, "managed": False, "days_left": 0,
            "label": "Proef verlopen — koop Personal of Pro"}


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


def ensure_device_id() -> str:
    """Stabiele, anonieme device-id per installatie (voor device-binding van een
    abonnement). Eenmalig gegenereerd en in .env bewaard."""
    did = os.environ.get("LAZYTYPE_DEVICE", "").strip()
    if not did:
        did = os.urandom(8).hex()
        try:
            save_env_value("LAZYTYPE_DEVICE", did)
        except Exception:
            pass
        os.environ["LAZYTYPE_DEVICE"] = did
    return did


def transcribe_managed(wav_bytes: bytes, language: str = "auto", postprocess: str = "off",
                       command: str = "") -> str:
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
    "enter": "\n",
    "nieuwe alinea": "\n\n",
    "nieuwe paragraaf": "\n\n",
}


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
                 postprocess: str = None) -> str:
    """Eén ingang voor alle aanroepers (daemon, CLI, tray). Bij de managed-engine
    doet de server het nabewerken; anders gebeurt dat lokaal."""
    engine = engine or ENGINE
    check_access(engine)
    language = language if language is not None else LANGUAGE
    mode = (postprocess if postprocess is not None else POSTPROCESS) or "off"
    if engine == "managed":
        return finalize_text(transcribe_managed(wav_bytes, language, mode))
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


def paste_text(text: str):
    from pynput.keyboard import Controller, Key

    if not text:
        return
    if TRAILING_SPACE and not text.endswith(" "):
        text += " "

    kb = Controller()
    previous = _clipboard_get() if RESTORE_CLIPBOARD else None
    paste_mod = Key.cmd if IS_MAC else Key.ctrl  # macOS plakt met Cmd+V

    if _clipboard_set(text):
        time.sleep(0.05)
        with kb.pressed(paste_mod):
            kb.press("v")
            kb.release("v")
        if RESTORE_CLIPBOARD and previous is not None:
            def restore():
                time.sleep(0.6)
                _clipboard_set(previous)
            threading.Thread(target=restore, daemon=True).start()
    else:
        # Klembord onbereikbaar → typ de tekst rechtstreeks (trager, maar werkt altijd)
        print("  (klembord bezet — tekst wordt getypt)")
        kb.type(text)


# ── Geluidsfeedback (cross-platform) ────────────────────────────────────
_WIN_BEEP = {"start": (1050, 120), "stop": (540, 110), "done": (880, 120), "error": (330, 280)}
_MAC_SOUND = {"start": "Tink", "stop": "Pop", "done": "Pop", "error": "Basso"}


def beep(kind: str):
    try:
        if IS_WIN:
            import winsound
            freq, dur = _WIN_BEEP.get(kind, (880, 80))
            winsound.Beep(freq, dur)
        elif IS_MAC:
            name = _MAC_SOUND.get(kind, "Tink")
            # niet-blokkerend afspelen
            subprocess.Popen(
                ["afplay", f"/System/Library/Sounds/{name}.aiff"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            print("\a", end="", flush=True)  # Linux: terminal-bel
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
