"""
Lazytype — systeemvak-versie.

Zelfde dicteren als dictate.py (houd een toets ingedrukt, spreek, laat los),
maar nu met een icoontje in het systeemvak:

  • Statuskleur:  blauw = klaar · rood = opname · oranje = transcriberen · grijs = gepauzeerd
  • Rechtsklik-menu: engine wisselen, taal wisselen, pauzeren, afsluiten

Starten:  python dictate_tray.py   (of dubbelklik Lazytype.bat)
Test:     python dictate_tray.py --selftest   (bouwt icoon+menu, schrijft preview, sluit af)
"""

import os
import sys
import math
import time
import subprocess
import threading
from pathlib import Path

# Forceer UTF-8 op de uitvoer; in een 'windowed' .exe is stdout None → devnull,
# anders crasht print() (zie ook dictate.py).
for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    if _s is None:
        try:
            setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
        except Exception:
            pass
    else:
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import pystray
from PIL import Image, ImageDraw

import dictate  # hergebruikt: Recorder, transcribe, finalize_text, paste_text, beep, config
from pynput import keyboard
from pynput.keyboard import Key

IS_WIN = dictate.IS_WIN
IS_MAC = dictate.IS_MAC

APP_VERSION = "1.8.13"
_update_info = None  # None = geen update beschikbaar / niet gecontroleerd; str = nieuwere versie

# Live-ticker: rapporteer woordtelling na elke transcriptie (fire-and-forget).
_STATS_URL = "https://lazytype.com/api/stats.php"
_STATS_KEY  = "lt_stats_K9mQ2wX4bR8t2026"


def _report_words(n: int) -> None:
    """Stuur woordtelling naar de live-ticker — silently, op achtergrondthread."""
    if n < 1:
        return
    try:
        import json as _j, urllib.request as _ur
        payload = _j.dumps({"key": _STATS_KEY, "words": n}).encode()
        req = _ur.Request(_STATS_URL, data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        _ur.urlopen(req, timeout=5)
    except Exception:
        pass


def _check_update():
    """Achtergrond-check of er een nieuwe versie beschikbaar is op lazytype.com."""
    global _update_info
    try:
        import requests
        r = requests.get("https://lazytype.com/version.json", timeout=8)
        latest = r.json().get("version", "")
        if latest and latest != APP_VERSION:
            _update_info = latest
            refresh()
    except Exception:
        pass


def _cleanup_stale_mei():
    """Ruim verweesde PyInstaller _MEI-mappen op die na een update achterblijven.

    Veilig: slaat de HUIDIGE map (sys._MEIPASS), recente mappen (<10 min, mogelijk
    een instantie die net (af)start) en nog-vergrendelde mappen over. Een map is
    'in gebruik' als zijn python*.dll niet exclusief te openen is — dan overslaan.
    Dit vervangt de gevaarlijke rmdir-sweep die tijdens het afsluiten een nog
    gebruikte map verwijderde ('Failed to load Python DLL')."""
    if not getattr(sys, "frozen", False) or not IS_WIN:
        return
    try:
        import tempfile, shutil
        tmp = Path(tempfile.gettempdir())
        try:
            current = str(Path(sys._MEIPASS).resolve())
        except Exception:
            current = ""
        now = time.time()
        for d in tmp.glob("_MEI*"):
            try:
                if not d.is_dir() or str(d.resolve()) == current:
                    continue
                if now - d.stat().st_mtime < 600:          # recent → mogelijk actief
                    continue
                dlls = list(d.glob("python*.dll"))
                if dlls:
                    try:
                        with open(dlls[0], "r+b"):          # exclusief openen
                            pass                            # lukt → niet geladen → verweesd
                    except OSError:
                        continue                            # vergrendeld → in gebruik → overslaan
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass

# Sneltoetsen die óók een modifier zijn (ctrl/alt/shift). Hierbij gebruiken we een
# "arming"-mechanisme: kort tikken of een snelkoppeling (bv. Ctrl+C) start géén dictaat.
MODIFIER_KEYS = {
    Key.ctrl, Key.ctrl_l, Key.ctrl_r,
    Key.alt, Key.alt_l, Key.alt_r, getattr(Key, "alt_gr", None),
    Key.shift, Key.shift_l, Key.shift_r,
    getattr(Key, "cmd", None), getattr(Key, "cmd_l", None), getattr(Key, "cmd_r", None),
}
MODIFIER_KEYS.discard(None)
MIN_HOLD_SEC = 0.35  # zo lang schoon ingedrukt houden voordat een modifier als dictaat telt


# ── Gedeelde, muteerbare status ─────────────────────────────────────────
_state_lock = threading.Lock()

state = {
    "engine": dictate.ENGINE,
    "language": dictate.LANGUAGE,
    "postprocess": dictate.POSTPROCESS,  # off | clean | taalcode (en/nl/de/…)
    "hotkey_name": dictate.HOTKEY_NAME,                    # dicteren
    "hotkey_command": dictate.COMMAND_HOTKEY_NAME,         # command mode
    "hotkey_translate": dictate.TRANSLATE_HOTKEY_NAME,     # dicteren + vertalen
    "translate_target": dictate.TRANSLATE_TARGET,          # doeltaal van de vertaal-toets
    "phase": "idle",     # idle | recording | working
    "enabled": True,
    "last": "",
    "last_dictation": "",   # laatste GESLAAGDE dictaat-tekst (voor command-op-laatste)
    "last_paste_len": 0,    # lengte van de laatste paste (voor "scratch that"-undo)
    "busy": False,
    "history": dictate.HISTORY_ENABLED,   # dicteer-geschiedenis bewaren
    "context": os.environ.get("DICTATE_CONTEXT", "1").lower() in ("1", "true", "yes", "on"),  # toon per app
    "realtime": os.environ.get("DICTATE_REALTIME", "0").lower() in ("1", "true", "yes", "on"),  # live preview
    "overlay": os.environ.get("DICTATE_OVERLAY", "1").lower() in ("1", "true", "yes", "on"),
    "active_mode": "dictate",       # dictate | command | translate (welke flow loopt nu)
    "active_matchers": None,        # welke sneltoets(-combo) de huidige opname startte
    "target_hwnd": 0,               # venster dat focus had bij indrukken → herstel vóór paste
}

PHASE_LABEL = {
    "idle": "Klaar — houd de toets ingedrukt",
    "recording": "🔴 Opname…",
    "working": "⏳ Transcriberen…",
    "disabled": "Gepauzeerd",
}

# Merk-icoon in lijn met de website: near-black squircle, witte waveform-balken,
# violette I-beam (tekstcursor). De STATUS zit in de kleur van de I-beam.
INK = (26, 27, 31)        # squircle-achtergrond (#1a1b1f), zoals site --ink
BAR = (245, 246, 250)     # witte balken
ACCENT = {
    "idle":      (90, 70, 224),    # violet #5a46e0 (merkkleur van de site)
    "recording": (226, 74, 74),    # rood = opname
    "working":   (240, 168, 60),   # amber = transcriberen
    "disabled":  (122, 126, 137),  # grijs = gepauzeerd
}
GRADIENTS = ACCENT        # alias voor compat
COLORS = ACCENT

# UI-thema voor de dialogen, in lijn met de website (warm-wit, ink, violet accent)
UI_PAPER, UI_INK, UI_ACCENT, UI_ACCENT2 = "#f7f6f3", "#191a1e", "#5a46e0", "#4a37c8"
UI_SUB, UI_LINE = "#6b6f7b", "#e8e6e0"
UI_FONT, UI_MONO = ("Segoe UI", 10), ("Consolas", 11)

# Keuzelijsten voor het instellingen-venster + onboarding
HOTKEY_CHOICES = [("Right Ctrl", "ctrl_r"), ("Left Ctrl", "ctrl_l"),
                  ("Right Alt", "alt_r"), ("Left Alt", "alt_l"), ("Right Shift", "shift_r"),
                  ("Ctrl + Alt", "ctrl+alt"), ("Ctrl + Shift", "ctrl+shift"),
                  ("Ctrl + Win", "ctrl+win"), ("Win + Alt", "win+alt"),
                  ("Win + Shift", "win+shift"), ("Alt + Shift", "alt+shift"),
                  ("F8", "f8"), ("F9", "f9"), ("F10", "f10"), ("Off", "uit")]


def _hk_display(spec):
    return next((l for l, v in HOTKEY_CHOICES if v == spec), spec)
# Alle door Whisper ondersteunde talen (ISO-code → naam). Eén bron voor alle
# taal-keuzelijsten (spreektaal, vertaaldoel, nabewerking). ~99 talen.
LANGUAGES = [
    ("Afrikaans", "af"), ("Albanian", "sq"), ("Amharic", "am"), ("Arabic", "ar"),
    ("Armenian", "hy"), ("Assamese", "as"), ("Azerbaijani", "az"), ("Bashkir", "ba"),
    ("Basque", "eu"), ("Belarusian", "be"), ("Bengali", "bn"), ("Bosnian", "bs"),
    ("Breton", "br"), ("Bulgarian", "bg"), ("Cantonese", "yue"), ("Catalan", "ca"),
    ("Chinese", "zh"), ("Croatian", "hr"), ("Czech", "cs"), ("Danish", "da"),
    ("Dutch", "nl"), ("English", "en"), ("Estonian", "et"), ("Faroese", "fo"),
    ("Finnish", "fi"), ("French", "fr"), ("Galician", "gl"), ("Georgian", "ka"),
    ("German", "de"), ("Greek", "el"), ("Gujarati", "gu"), ("Haitian Creole", "ht"),
    ("Hausa", "ha"), ("Hawaiian", "haw"), ("Hebrew", "he"), ("Hindi", "hi"),
    ("Hungarian", "hu"), ("Icelandic", "is"), ("Indonesian", "id"), ("Italian", "it"),
    ("Japanese", "ja"), ("Javanese", "jw"), ("Kannada", "kn"), ("Kazakh", "kk"),
    ("Khmer", "km"), ("Korean", "ko"), ("Lao", "lo"), ("Latin", "la"),
    ("Latvian", "lv"), ("Lingala", "ln"), ("Lithuanian", "lt"), ("Luxembourgish", "lb"),
    ("Macedonian", "mk"), ("Malagasy", "mg"), ("Malay", "ms"), ("Malayalam", "ml"),
    ("Maltese", "mt"), ("Maori", "mi"), ("Marathi", "mr"), ("Mongolian", "mn"),
    ("Myanmar", "my"), ("Nepali", "ne"), ("Norwegian", "no"), ("Nynorsk", "nn"),
    ("Occitan", "oc"), ("Pashto", "ps"), ("Persian", "fa"), ("Polish", "pl"),
    ("Portuguese", "pt"), ("Punjabi", "pa"), ("Romanian", "ro"), ("Russian", "ru"),
    ("Sanskrit", "sa"), ("Serbian", "sr"), ("Shona", "sn"), ("Sindhi", "sd"),
    ("Sinhala", "si"), ("Slovak", "sk"), ("Slovenian", "sl"), ("Somali", "so"),
    ("Spanish", "es"), ("Sundanese", "su"), ("Swahili", "sw"), ("Swedish", "sv"),
    ("Tagalog", "tl"), ("Tajik", "tg"), ("Tamil", "ta"), ("Tatar", "tt"),
    ("Telugu", "te"), ("Thai", "th"), ("Tibetan", "bo"), ("Turkish", "tr"),
    ("Turkmen", "tk"), ("Ukrainian", "uk"), ("Urdu", "ur"), ("Uzbek", "uz"),
    ("Vietnamese", "vi"), ("Welsh", "cy"), ("Yiddish", "yi"), ("Yoruba", "yo"),
]

# Afgeleide keuzelijsten — overal dezelfde ~99 talen.
LANG_CHOICES = list(LANGUAGES)                                    # vertaaldoel
TRANSLATE_TO_CHOICES = list(LANGUAGES)                            # idem (onboarding)
SPOKEN_CHOICES = [("Auto-detect", "auto")] + list(LANGUAGES)     # spreektaal (+ auto)
ENGINE_CHOICES = [("Managed (Pro)", "managed"), ("Groq (own key)", "groq"),
                  ("OpenAI", "openai"), ("Local", "local")]

LANG_DISPLAY = {code: name for name, code in LANGUAGES}
LANG_DISPLAY["auto"] = "Auto-detect"

# AI-nabewerking voor het normale dictaat: uit / opschonen / vertaal naar taal.
POSTPROC_CHOICES = [("Uit", "off"), ("Opschonen (zelfde taal)", "clean"),
                    ("Vertaal → Engels", "en"), ("Vertaal → Nederlands", "nl"),
                    ("Vertaal → Duits", "de"), ("Vertaal → Frans", "fr"),
                    ("Vertaal → Spaans", "es")]


def render_icon(accent, size=64, shape="squircle", bar=BAR, bg=INK):
    """Wave-cursor merkmark (4× supersampling, antialiased): near-black squircle,
    witte waveform-balken, I-beam-cursor in de accent/statuskleur."""
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = int(S * 0.035)
    if shape == "circle":
        d.ellipse((pad, pad, S - pad, S - pad), fill=bg + (255,))
    else:
        d.rounded_rectangle((pad, pad, S - pad, S - pad), radius=int(S * 0.235), fill=bg + (255,))

    cx = cy = S // 2
    bw = int(S * 0.08)
    sp = int(S * 0.155)

    def vbar(x, h, color):
        d.rounded_rectangle((x - bw // 2, cy - h // 2, x + bw // 2, cy + h // 2),
                            radius=bw // 2, fill=color + (255,))

    vbar(cx - 2 * sp, int(S * 0.20), bar)   # buiten: kort
    vbar(cx - sp,     int(S * 0.40), bar)   # binnen: lang
    vbar(cx + sp,     int(S * 0.40), bar)
    vbar(cx + 2 * sp, int(S * 0.20), bar)

    # I-beam (tekstcursor) in de accent/statuskleur
    a = accent + (255,)
    H = int(S * 0.50)
    top, bot = cy - H // 2, cy + H // 2
    sw, ct, cw = int(S * 0.085), int(S * 0.055), int(S * 0.10)
    d.rounded_rectangle((cx - sw // 2, top, cx + sw // 2, bot), radius=sw // 2, fill=a)
    d.rounded_rectangle((cx - cw, top, cx + cw, top + ct), radius=ct // 2, fill=a)
    d.rounded_rectangle((cx - cw, bot - ct, cx + cw, bot), radius=ct // 2, fill=a)

    return img.resize((size, size), Image.LANCZOS)


def make_icon(state_or_color, size=64, shape="squircle"):
    if isinstance(state_or_color, str):
        accent = ACCENT.get(state_or_color, ACCENT["idle"])
        bar = (150, 154, 165) if state_or_color == "disabled" else BAR
    elif isinstance(state_or_color, tuple):
        accent, bar = state_or_color, BAR
    else:
        accent, bar = ACCENT["idle"], BAR
    return render_icon(accent, size=size, shape=shape, bar=bar)


ICONS = {k: make_icon(k) for k in ACCENT}

icon = None
recorder = dictate.Recorder()
arm = {"timer": None, "aborted": False}
pressed = set()       # toetsen die nu ingedrukt zijn
HOTKEYS_LIST = []     # lijst van (matchers, mode, all_modifiers); langste combo eerst
_keypicking = False   # True tijdens keypicker-capture → pynput-listener doet niets

# Generieke modifier-groepen — bij een combo maakt links/rechts niet uit.
MOD_GROUPS = {
    "ctrl":  {Key.ctrl, Key.ctrl_l, Key.ctrl_r},
    "alt":   {Key.alt, Key.alt_l, Key.alt_r, getattr(Key, "alt_gr", None)},
    "shift": {Key.shift, Key.shift_l, Key.shift_r},
    "win":   {getattr(Key, "cmd", None), getattr(Key, "cmd_l", None), getattr(Key, "cmd_r", None)},
    "cmd":   {getattr(Key, "cmd", None), getattr(Key, "cmd_l", None), getattr(Key, "cmd_r", None)},
}


def _matcher(token):
    """Eén deel van een sneltoets → set van toetsen die ervoor tellen."""
    token = token.strip().lower()
    if token in MOD_GROUPS:
        return {k for k in MOD_GROUPS[token] if k is not None}
    try:
        return {dictate.resolve_hotkey(token)}
    except Exception:
        return set()


def _parse_hotkey(spec):
    """'ctrl+alt' / 'ctrl_l' / 'win shift' → lijst van matchers (1 = enkel, 2 = combo)."""
    parts = [p for p in spec.strip().lower().replace("+", " ").split() if p]
    return [m for m in (_matcher(p) for p in parts) if m]


def _all_modifiers(matchers):
    return all(all(k in MODIFIER_KEYS for k in m) for m in matchers)


def _satisfied(matchers):
    return bool(matchers) and all(any(k in pressed for k in m) for m in matchers)


def rebuild_hotkeys():
    """(Her)bouw de sneltoets-lijst uit de instellingen. De listener leest 'm live,
    dus losse toetsen én combo's wijzigen werkt zonder de app te herstarten."""
    global HOTKEYS_LIST
    lst = []
    triggers = [(state["hotkey_name"], "dictate"),
                (state["hotkey_command"], "command"),
                (state["hotkey_translate"], "translate")]
    # AI-modi met een eigen sneltoets → mode:<index>
    for i, m in enumerate(dictate.load_modes()):
        if m.get("hotkey"):
            triggers.append((m["hotkey"], f"mode:{i}"))
    for spec, mode in triggers:
        spec = (spec or "").strip()
        if not spec or spec.lower() in ("uit", "none", "geen", ""):
            continue
        matchers = _parse_hotkey(spec)
        if matchers:
            lst.append((matchers, mode, _all_modifiers(matchers)))
    lst.sort(key=lambda e: -len(e[0]))   # langste combo eerst → 'ctrl+alt' wint van losse 'ctrl'
    HOTKEYS_LIST = lst


rebuild_hotkeys()


def current_phase():
    return "disabled" if not state["enabled"] else state["phase"]


def refresh():
    if not icon:
        return
    ph = current_phase()
    icon.icon = ICONS[ph]
    pp = state.get("postprocess", "off")
    pp_tag = "" if pp in ("off", "") else (" · opschonen" if pp == "clean" else f" · →{pp}")
    icon.title = f"Lazytype · {PHASE_LABEL[ph]} · {state['engine']}/{state['language']}{pp_tag}"
    try:
        icon.menu = build_menu()   # herbouw zodat o.a. de geschiedenis live meeloopt
        icon.update_menu()
    except Exception:
        pass


def set_phase(p):
    state["phase"] = p
    refresh()


# ── Opname → transcriptie → plakken ─────────────────────────────────────
def handle_release():
    try:
        wav = recorder.stop()
        secs = recorder.duration()
        overlay_ui.hide_preview()   # realtime preview-balk weg
        threading.Thread(target=lambda: dictate.beep("stop"), daemon=True).start()
        if secs < 0.3:
            return
        set_phase("working")
        t0 = time.time()
        mode = state.get("active_mode")
        ctx = active_app_context(state.get("target_hwnd", 0))   # toon-aanpassing per app
        if mode == "command":
            # Geselecteerde tekst heeft voorrang; is er niets geselecteerd, dan passen
            # we de gesproken instructie toe op het LAATSTE dictaat (command-op-laatste).
            sel = dictate.copy_selection() or state.get("last_dictation", "")
            if not sel:
                state["last"] = "Command: selecteer tekst of dicteer eerst iets"
                threading.Thread(target=lambda: dictate.beep("error"), daemon=True).start()
                return
            state["last"] = "Commando verwerken…"
            text = dictate.transform_command(wav, sel, language=state["language"], engine=state["engine"])
        elif mode == "translate":
            state["last"] = "Transcriberen…"
            text = dictate.run_pipeline(wav, engine=state["engine"], language=state["language"],
                                        postprocess=state["translate_target"], context=ctx)
        else:
            eng = state["engine"]
            pp  = state["postprocess"]
            if eng == "managed" or pp in ("off", ""):
                text = dictate.run_pipeline(wav, engine=eng,
                                            language=state["language"], postprocess=pp, context=ctx)
            else:
                state["last"] = "Transcriberen…"
                dictate.check_access(eng)
                raw = dictate.transcribe(wav, eng, state["language"])
                raw = dictate.finalize_text(raw)
                if raw.strip():
                    state["last"] = "Nabewerken…"
                    text = dictate.postprocess_text(raw, pp)
                else:
                    text = raw
        dt = time.time() - t0
        if not text:
            state["last"] = "(geen spraak herkend)"
            threading.Thread(target=lambda: dictate.beep("error"), daemon=True).start()
            return
        print(f"  ✅ ({dt:.2f}s) → {text}")
        hwnd = state.get("target_hwnd", 0)

        # Stem-actie: "scratch that" → wis het vorige dictaat i.p.v. plakken.
        is_undo = mode not in ("command", "translate") and dictate.voice_action(text) == "undo"

        # Output (plakken of wissen) op de overlay-thread (Win32 message queue aanwezig →
        # SetForegroundWindow werkt hier; anders faalt focus-herstel silently).
        _done = threading.Event()

        def _output_on_tk():
            try:
                _restore_focus(hwnd)
                if is_undo:
                    dictate.delete_last(state.get("last_paste_len", 0))
                else:
                    dictate.paste_text(text)
            except Exception as _e:
                print(f"  ⚠️ output: {_e}")
            finally:
                _done.set()

        if dictate.IS_WIN and overlay_ui.root:
            overlay_ui.root.after(0, _output_on_tk)
            _done.wait(timeout=8.0)
        else:
            _restore_focus(hwnd)
            dictate.delete_last(state.get("last_paste_len", 0)) if is_undo else dictate.paste_text(text)

        if is_undo:
            state["last"] = "↶ ongedaan gemaakt"
            state["last_dictation"] = ""
            state["last_paste_len"] = 0
        else:
            state["last"] = text
            state["last_dictation"] = text   # voor command-op-laatste (ook ketenen van edits)
            state["last_paste_len"] = len(text) + (1 if dictate.TRAILING_SPACE and not text.endswith(" ") else 0)
            _kind = "command" if mode == "command" else "vertalen" if mode == "translate" else "dictaat"
            dictate.record_usage(text, _kind)   # statistiek bijwerken
            threading.Thread(target=lambda t=text: _report_words(len(t.split())), daemon=True).start()

        threading.Thread(target=lambda: dictate.beep("done"), daemon=True).start()
        if not is_undo and state.get("history"):
            dictate.add_history(text)
    except Exception as e:
        print(f"  ⚠️  {e}")
        state["last"] = f"Fout: {e}"
        threading.Thread(target=lambda: dictate.beep("error"), daemon=True).start()
    finally:
        state["busy"] = False
        set_phase("idle")


def apply_mode(idx: int, matchers):
    """Pas een opgeslagen AI-mode toe op de selectie (of het laatste dictaat) — geen audio."""
    try:
        modes = dictate.load_modes()
        if idx >= len(modes):
            return
        mode = modes[idx]
        # Wacht tot de sneltoets is losgelaten, anders verstoren de modifiers de Ctrl/Cmd+C.
        for _ in range(60):
            if not _satisfied(matchers):
                break
            time.sleep(0.05)
        time.sleep(0.05)
        set_phase("working")
        hwnd = state.get("target_hwnd", 0)
        text = dictate.copy_selection() or state.get("last_dictation", "")
        if not text:
            state["last"] = f"{mode.get('name') or 'Mode'}: selecteer tekst of dicteer eerst"
            threading.Thread(target=lambda: dictate.beep("error"), daemon=True).start()
            return
        threading.Thread(target=lambda: dictate.beep("start"), daemon=True).start()
        result = dictate.run_command_text(mode["instruction"], text, engine=state["engine"])
        if not result:
            state["last"] = "(geen resultaat)"
            threading.Thread(target=lambda: dictate.beep("error"), daemon=True).start()
            return
        _done = threading.Event()

        def _out():
            try:
                _restore_focus(hwnd)
                dictate.paste_text(result)
            except Exception as _e:
                print(f"  ⚠️ mode paste: {_e}")
            finally:
                _done.set()

        if dictate.IS_WIN and overlay_ui.root:
            overlay_ui.root.after(0, _out)
            _done.wait(timeout=8.0)
        else:
            _restore_focus(hwnd)
            dictate.paste_text(result)
        state["last"] = result
        state["last_dictation"] = result
        state["last_paste_len"] = len(result) + (1 if dictate.TRAILING_SPACE and not result.endswith(" ") else 0)
        dictate.record_usage(result, f"mode:{(mode.get('name') or 'Mode').strip()}")   # statistiek
        threading.Thread(target=lambda: dictate.beep("done"), daemon=True).start()
        if state.get("history"):
            dictate.add_history(result)
    except Exception as e:
        print(f"  ⚠️ mode: {e}")
        state["last"] = f"Fout: {e}"
        threading.Thread(target=lambda: dictate.beep("error"), daemon=True).start()
    finally:
        state["busy"] = False
        set_phase("idle")


def _confirm_arming():
    """Loopt MIN_HOLD_SEC na het indrukken: promoot 'arming' → echte opname."""
    if state["phase"] == "arming" and not arm["aborted"]:
        dictate.beep("start")
        set_phase("recording")


def _get_foreground_hwnd() -> int:
    if dictate.IS_WIN:
        try:
            import ctypes
            return ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            pass
    return 0


# App-naam (lowercase) → context-categorie voor toon-aanpassing.
_APP_CONTEXT = {
    "outlook": "email", "thunderbird": "email", "mailspring": "email", "mail": "email",
    "em client": "email", "spark": "email", "postbox": "email",
    "slack": "chat", "discord": "chat", "whatsapp": "chat", "telegram": "chat",
    "teams": "chat", "signal": "chat", "messages": "chat", "messenger": "chat",
    "code": "code", "devenv": "code", "cursor": "code", "windsurf": "code",
    "sublime_text": "code", "sublime text": "code", "pycharm": "code", "idea": "code",
    "rider": "code", "webstorm": "code", "goland": "code", "clion": "code",
    "xcode": "code", "terminal": "code", "iterm": "code", "powershell": "code",
}


def _proc_name_from_hwnd(hwnd: int) -> str:
    """exe-basename (lowercase, zonder .exe) van het venster-proces (Windows)."""
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                   wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1].lower().replace(".exe", "")
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _mac_frontmost_app() -> str:
    try:
        out = subprocess.check_output(
            ["osascript", "-e",
             'tell application "System Events" to name of first process whose frontmost is true'],
            text=True, timeout=2)
        return out.strip().lower()
    except Exception:
        return ""


def active_app_context(hwnd: int = 0) -> str:
    """Context-categorie van de actieve app: 'email' | 'chat' | 'code' | '' (algemeen).
    Stuurt niets als de toon-aanpassing uitstaat."""
    if not state.get("context", True):
        return ""
    name = _proc_name_from_hwnd(hwnd) if dictate.IS_WIN else (_mac_frontmost_app() if dictate.IS_MAC else "")
    if not name:
        return ""
    for token, cat in _APP_CONTEXT.items():
        if token in name:
            return cat
    return ""


def _restore_focus(hwnd: int):
    if not hwnd or not dictate.IS_WIN:
        return
    try:
        import ctypes
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        fg = u32.GetForegroundWindow()
        if fg == hwnd:
            return
        fg_tid  = u32.GetWindowThreadProcessId(fg,   None)
        tgt_tid = u32.GetWindowThreadProcessId(hwnd, None)
        our_tid = k32.GetCurrentThreadId()
        u32.AttachThreadInput(our_tid, fg_tid, True)
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        u32.AttachThreadInput(our_tid, fg_tid, False)
        import time as _t; _t.sleep(0.08)
    except Exception:
        pass


def _start_realtime_preview():
    """Loop tijdens het opnemen: transcribeer de audio-tot-nu en toon 'm in de
    preview-balk. Alleen bij realtime-toggle + dictate-modus + managed-engine."""
    if not state.get("realtime") or state.get("active_mode") != "dictate" or state.get("engine") != "managed":
        return

    def _loop():
        last = ""
        while state["phase"] in ("recording", "arming"):
            time.sleep(2.2)
            if state["phase"] != "recording":
                continue
            try:
                if recorder.duration() < 1.0:
                    continue
                txt = dictate.transcribe_managed(recorder.snapshot(), state["language"], "off")
                if txt and txt != last:
                    last = txt
                    overlay_ui.show_preview(txt)
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True).start()


def _begin(matchers, mode, needs_arming):
    state["target_hwnd"] = _get_foreground_hwnd()
    recorder.start()
    arm["aborted"] = False
    state["active_mode"] = mode
    state["active_matchers"] = matchers
    if needs_arming:
        # nog niet zeker of dit een dictaat/command of een snelkoppeling wordt
        state["phase"] = "arming"
        arm["timer"] = threading.Timer(MIN_HOLD_SEC, _confirm_arming)
        arm["timer"].start()
    else:
        threading.Thread(target=lambda: dictate.beep("start"), daemon=True).start()
        set_phase("recording")
    _start_realtime_preview()


def on_press(key):
    try:
        pressed.add(key)
        if _keypicking or not state["enabled"] or state["busy"]:
            return
        hit = next(((mt, md, am) for mt, md, am in HOTKEYS_LIST if _satisfied(mt)), None)
        if state["phase"] == "idle":
            if hit:
                if hit[1].startswith("mode:"):
                    # AI-mode: instant uitvoeren (geen opname). busy blokkeert key-repeat.
                    state["busy"] = True
                    state["target_hwnd"] = _get_foreground_hwnd()
                    idx = int(hit[1].split(":", 1)[1])
                    mt = hit[0]
                    threading.Thread(target=lambda: apply_mode(idx, mt), daemon=True).start()
                else:
                    _begin(hit[0], hit[1], hit[2])
            return
        if state["phase"] == "arming":
            active = state.get("active_matchers") or []
            if hit and hit[0] != active and len(hit[0]) >= len(active):
                # tweede toets erbij → schakel over naar de specifiekere combo
                if arm["timer"]:
                    arm["timer"].cancel()
                arm["aborted"] = False
                state["active_matchers"] = hit[0]
                state["active_mode"] = hit[1]
                if hit[2]:
                    arm["timer"] = threading.Timer(MIN_HOLD_SEC, _confirm_arming)
                    arm["timer"].start()
                else:
                    threading.Thread(target=lambda: dictate.beep("start"), daemon=True).start()
                    set_phase("recording")
                return
            # toets die bij geen actieve hotkey hoort → het was een snelkoppeling
            if not any(key in m for m in active):
                arm["aborted"] = True
    except Exception as e:
        print(f"on_press error: {e}")


def on_release(key):
    try:
        pressed.discard(key)
        if state["phase"] not in ("arming", "recording"):
            return
        active = state.get("active_matchers")
        if not active or _satisfied(active):
            return                       # combo nog volledig ingedrukt → niks doen
        if state["phase"] == "arming":
            # te snel losgelaten of afgebroken → stil weggooien
            if arm["timer"]:
                arm["timer"].cancel()
            recorder.stop()
            set_phase("idle")
            return
        state["busy"] = True
        set_phase("working")
        threading.Thread(target=handle_release, daemon=True).start()
    except Exception as e:
        print(f"on_release error: {e}")


# ── Menu-acties ─────────────────────────────────────────────────────────
def choose_engine(e):
    def action(icon_, item):
        state["engine"] = e
        refresh()
    return action


def choose_language(code):
    def action(icon_, item):
        state["language"] = code
        refresh()
    return action


def choose_postprocess(mode):
    def action(icon_, item):
        state["postprocess"] = mode
        dictate.save_env_value("DICTATE_POSTPROCESS", mode)  # bewaar keuze (bleef voorheen niet behouden)
        refresh()
    return action


def toggle_enabled(icon_, item):
    state["enabled"] = not state["enabled"]
    refresh()


def history_copy(text):
    def action(icon_, item):
        dictate._clipboard_set(text)
        state["last"] = "Gekopieerd ✓"
        refresh()
    return action


def toggle_history(icon_, item):
    state["history"] = not state["history"]
    dictate.save_env_value("DICTATE_HISTORY", "1" if state["history"] else "0")
    refresh()


def clear_history_action(icon_, item):
    dictate.clear_history()
    state["last"] = "Geschiedenis gewist"
    refresh()


def _hist_label(text):
    t = (text or "").replace("\n", "⏎").strip()
    return (t[:42] + "…") if len(t) > 42 else (t or "—")


def do_quit(icon_, item):
    overlay_ui.stop()          # eerst de overlay-Tk netjes sluiten (op zijn eigen thread)
    icon_.visible = False
    icon_.stop()


def short_last():
    t = state["last"]
    if not t:
        return "Laatste: —"
    t = t.replace("\n", "⏎")
    return "Laatste: " + (t[:38] + "…" if len(t) > 38 else t)


# ── Automatisch starten bij inloggen (Windows: register · macOS: LaunchAgent) ──
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "Lazytype"
LAUNCH_AGENT = dictate._user_home() / "Library" / "LaunchAgents" / "com.lazytype.plist"


def autostart_supported():
    return IS_WIN or IS_MAC


def _program_args():
    """Het commando om de tool bij login te starten — gebouwde app of python-script."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        # macOS: start de .app via LaunchServices ('open <app>') i.p.v. de losse
        # Mach-O. De binary rechtstreeks starten wordt door Gatekeeper geweigerd bij
        # een niet-interactieve login-start en verliest de app-context (TCC-rechten
        # mic/invoerbewaking). 'open' respecteert de eerdere goedkeuring + rechten.
        if IS_MAC and exe.parent.name == "MacOS" and exe.parents[2].suffix == ".app":
            return ["/usr/bin/open", str(exe.parents[2])]
        return [str(exe)]
    if IS_WIN:
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        runner = str(pyw if pyw.exists() else exe)
    else:
        runner = sys.executable
    return [runner, str(Path(__file__).resolve())]


def is_autostart():
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                val, _ = winreg.QueryValueEx(k, APP_NAME)
                return bool(val)
        except OSError:
            return False
    if IS_MAC:
        return LAUNCH_AGENT.exists()
    return False


def set_autostart(on: bool):
    if IS_WIN:
        import winreg
        cmd = " ".join(f'"{a}"' for a in _program_args())
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
    elif IS_MAC:
        if on:
            args = "".join(f"      <string>{a}</string>\n" for a in _program_args())
            plist = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '  <key>Label</key><string>com.lazytype</string>\n'
                '  <key>ProgramArguments</key><array>\n' + args +
                '  </array>\n'
                '  <key>RunAtLoad</key><true/>\n'
                '</dict></plist>\n'
            )
            LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
            LAUNCH_AGENT.write_text(plist, encoding="utf-8")
            subprocess.run(["launchctl", "load", str(LAUNCH_AGENT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif LAUNCH_AGENT.exists():
            subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            LAUNCH_AGENT.unlink()


def toggle_autostart(icon_, item):
    new = not is_autostart()
    set_autostart(new)
    dictate.save_env_value("DICTATE_AUTOSTART_OPTOUT", "0" if new else "1")  # keuze onthouden
    refresh()


# ── Windows: als 'echt' programma registreren (Start-menu + Geïnstalleerde apps) ──
def _win_make_shortcut(lnk_path: str, target: str) -> bool:
    """Maak een .lnk via WScript.Shell (PowerShell) — geen extra dependency."""
    t = target.replace("'", "''"); l = lnk_path.replace("'", "''")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{l}');"
          f"$s.TargetPath='{t}';$s.IconLocation='{t},0';"
          f"$s.Description='Lazytype — spraak naar tekst';$s.Save()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                       creationflags=0x08000000, timeout=25,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return Path(lnk_path).exists()
    except Exception:
        return False


def _win_start_menu_lnk() -> Path:
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
            "Start Menu" / "Programs" / "Lazytype.lnk")


def _win_install_integration():
    """Eenmalig (frozen Windows): Start-menu-snelkoppeling + vermelding in
    Geïnstalleerde apps, zodat Lazytype tussen je programma's staat i.p.v. alleen
    een tray-icoon. Respecteert een eerdere registratie via DICTATE_REGISTERED."""
    if not (IS_WIN and getattr(sys, "frozen", False)):
        return
    if os.environ.get("DICTATE_REGISTERED", "").lower() in ("1", "true", "yes"):
        return
    exe = str(Path(sys.executable).resolve())
    try:
        lnk = _win_start_menu_lnk()
        lnk.parent.mkdir(parents=True, exist_ok=True)
        _win_make_shortcut(str(lnk), exe)
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Lazytype"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "Lazytype")
            winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
            winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "Lazytype")
            winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, exe)
            winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, str(Path(exe).parent))
            winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, f'"{exe}" --uninstall')
            winreg.SetValueEx(k, "URLInfoAbout", 0, winreg.REG_SZ, "https://lazytype.com")
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        dictate.save_env_value("DICTATE_REGISTERED", "1")
        print("  Windows-integratie: Start-menu-snelkoppeling + Geïnstalleerde apps")
    except Exception as e:
        print(f"  (Windows-integratie overgeslagen: {e})")


def _win_uninstall():
    """Verwijder Lazytype netjes (aangeroepen vanuit Geïnstalleerde apps → Verwijderen)."""
    if not IS_WIN:
        return
    try:
        import tkinter as tk, tkinter.messagebox as mb
        r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)
        ok = mb.askyesno("Lazytype verwijderen",
                         "Weet je zeker dat je Lazytype wilt verwijderen?\n\n"
                         "Dit verwijdert de app, je instellingen en de autostart.")
        r.destroy()
    except Exception:
        ok = True
    if not ok:
        return
    import winreg, tempfile, shutil
    exe = Path(sys.executable).resolve()
    try:
        set_autostart(False)
    except Exception:
        pass
    try:
        _win_start_menu_lnk().unlink(missing_ok=True)
    except Exception:
        pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Lazytype")
    except Exception:
        pass
    try:
        appdir = Path(os.environ.get("APPDATA", "")) / "Lazytype"
        if appdir.exists():
            shutil.rmtree(appdir, ignore_errors=True)
    except Exception:
        pass
    try:
        bat = Path(tempfile.gettempdir()) / "lazytype_uninstall.bat"
        bat.write_text("@echo off\r\nping -n 3 127.0.0.1 >nul\r\n"
                       f'del /f /q "{exe}" >nul 2>&1\r\n'
                       'del "%~f0" >nul 2>&1\r\n', encoding="ascii")
        subprocess.Popen(["cmd", "/c", str(bat)], cwd=tempfile.gettempdir(),
                         creationflags=0x08000000 | 0x00000008, close_fds=True)
    except Exception:
        pass


# ── Eigen invoer-dialoog in de site-stijl (vervangt de grijze simpledialog) ──
def _dlg_subprocess(kind, **spec):
    """macOS: toon een Tk-dialoog in een APART proces. Tk/Cocoa mag niet op een
    pystray/worker-thread draaien (→ native abort). Geeft de ingevoerde tekst
    terug (input-dialoog), of None."""
    import subprocess, json, base64
    payload = base64.b64encode(json.dumps({"kind": kind, **spec}).encode()).decode()
    try:
        r = subprocess.run([sys.executable, "--dlg", payload], capture_output=True, timeout=1800)
        for ln in r.stdout.decode("utf-8", "replace").splitlines():
            if ln.startswith("RESULT:"):
                return base64.b64decode(ln[7:]).decode("utf-8", "replace")
    except Exception:
        pass
    return None


def _themed_input(title, prompt, initial="", secret=False):
    if IS_MAC:   # niet op een worker/pystray-thread → apart proces
        return _dlg_subprocess("input", title=title, prompt=prompt,
                               initial=initial or "", secret=bool(secret))
    return _themed_input_tk(title, prompt, initial, secret)


def _themed_input_tk(title, prompt, initial="", secret=False):
    import tkinter as tk
    result = {"value": None}
    root = tk.Tk()
    root.title(title)
    root.configure(bg=UI_PAPER)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text=prompt, bg=UI_PAPER, fg=UI_INK, font=UI_FONT,
             wraplength=400, justify="left").pack(fill="x", padx=18, pady=(18, 8))
    var = tk.StringVar(value=initial)
    entry = tk.Entry(root, textvariable=var, width=46, font=UI_FONT,
                     show="•" if secret else "", relief="flat", bg="#ffffff",
                     fg=UI_INK, insertbackground=UI_INK, highlightthickness=1,
                     highlightbackground="#dedbd3", highlightcolor=UI_ACCENT)
    entry.pack(padx=18, ipady=6, fill="x")
    entry.focus_set(); entry.icursor("end")

    def ok(_e=None):
        result["value"] = var.get(); root.destroy()

    def cancel(_e=None):
        result["value"] = None; root.destroy()

    bar = tk.Frame(root, bg=UI_PAPER)
    bar.pack(fill="x", padx=18, pady=16)
    tk.Button(bar, text="Annuleer", command=cancel, bg=UI_PAPER, fg=UI_INK,
              relief="flat", font=UI_FONT, cursor="hand2", borderwidth=0,
              activebackground=UI_PAPER, activeforeground=UI_ACCENT).pack(side="right", padx=(8, 0))
    tk.Button(bar, text="OK", command=ok, bg=UI_ACCENT, fg="white", relief="flat",
              activebackground=UI_ACCENT2, activeforeground="white",
              font=("Segoe UI", 10, "bold"), padx=22, pady=6, cursor="hand2",
              borderwidth=0).pack(side="right")
    root.bind("<Return>", ok)
    root.bind("<Escape>", cancel)
    root.mainloop()
    return result["value"]


# ── API-key instellen (eigen dialoog) ───────────────────────────────────
def ask_groq_key(initial: str = "") -> str | None:
    return _themed_input("Lazytype — Groq API-key",
                         "Plak je Groq API-key (gratis via console.groq.com/keys):", initial)


def set_key_action(icon_, item):
    def worker():
        key = ask_groq_key(initial=os.environ.get("GROQ_API_KEY", ""))
        if key:
            dictate.save_env_value("GROQ_API_KEY", key.strip())
            state["last"] = "API-key opgeslagen ✓"
            refresh()
    threading.Thread(target=worker, daemon=True).start()


def _request_trial_code(email: str) -> None:
    """STAP 1: vraag een 6-cijferige verificatiecode aan (server mailt die).
    Niets terug bij succes; raise RuntimeError bij fout (bv. cooldown/al gebruikt)."""
    try:
        import requests
        base = dictate.API_URL.rsplit("/", 1)[0]
        r = requests.post(f"{base}/trial.php", data={
            "email": email.strip(),
            "device": dictate.ensure_device_id(),
        }, timeout=15)
        data = r.json()
        if r.ok and data.get("ok") and data.get("code_sent"):
            return
        raise RuntimeError(data.get("error", f"Serverfout {r.status_code}"))
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Geen verbinding: {e}")


def _verify_trial_code(email: str, code: str) -> str:
    """STAP 2: verifieer de code → geeft de proefsleutel terug of raise RuntimeError."""
    try:
        import requests
        base = dictate.API_URL.rsplit("/", 1)[0]
        r = requests.post(f"{base}/trial.php", data={
            "email": email.strip(),
            "code": code.strip(),
            "device": dictate.ensure_device_id(),
        }, timeout=15)
        data = r.json()
        if r.ok and data.get("ok") and data.get("key"):
            return data["key"]
        raise RuntimeError(data.get("error", f"Serverfout {r.status_code}"))
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Geen verbinding: {e}")


# ── Abonnement (licentiesleutel) ────────────────────────────────────────
def license_status() -> str:
    return "Status: " + dictate.license_state()["label"]


def set_license_action(icon_, item):
    def worker():
        import license as lic
        val = _ask("Lazytype — Abonnement", "Plak je licentiesleutel (begint met LZT.):",
                   os.environ.get("LAZYTYPE_LICENSE", ""))
        if val:
            val = val.strip()
            dictate.save_env_value("LAZYTYPE_LICENSE", val)
            os.environ["LAZYTYPE_LICENSE"] = val
            p = lic.decode(val)
            state["last"] = "Abonnement: " + (lic.describe(p) if p else "ongeldige sleutel")
            if p and lic.TIERS.get(p.get("tier"), {}).get("managed"):
                state["engine"] = "managed"
                dictate.save_env_value("DICTATE_ENGINE", "managed")
            refresh()
    threading.Thread(target=worker, daemon=True).start()


# ── Admin (owner-only: alleen zichtbaar als het geheim lokaal aanwezig is) ──
def is_owner() -> bool:
    return bool(os.environ.get("LAZYTYPE_LICENSE_SECRET")) and not getattr(sys, "frozen", False)


def _ask(title: str, prompt: str, initial: str = ""):
    return _themed_input(title, prompt, initial)


def _show_text(title: str, text: str):
    if IS_MAC:   # apart proces (Tk niet op een worker-thread)
        _dlg_subprocess("text", title=title, text=text)
        return
    threading.Thread(target=lambda: _show_text_tk(title, text), daemon=True).start()


def _show_text_tk(title: str, text: str):
    import tkinter as tk
    from tkinter.scrolledtext import ScrolledText
    root = tk.Tk(); root.title(title); root.attributes("-topmost", True)
    root.configure(bg=UI_PAPER)
    box = ScrolledText(root, width=80, height=22, font=UI_MONO, bg="#ffffff",
                       fg=UI_INK, insertbackground=UI_INK, relief="flat",
                       borderwidth=0, padx=12, pady=10)
    box.pack(fill="both", expand=True, padx=12, pady=12)
    box.insert("1.0", text); box.configure(state="disabled")
    root.mainloop()


def admin_gen_action(icon_, item):
    def worker():
        import admin, license as lic
        email = _ask("Admin — nieuwe sleutel", "E-mail van de klant:")
        if not email:
            return
        tier = (_ask("Admin — tier", "Tier (personal / pro):", "pro") or "pro").strip().lower()
        days = (_ask("Admin — geldigheid", "Geldig in dagen (0 = lifetime):", "30") or "30").strip()
        try:
            key, payload = admin.gen(email.strip(), tier, int(days or 0))
        except Exception as e:
            _show_text("Admin — fout", str(e)); return
        dictate._clipboard_set(key)
        _show_text("Admin — sleutel aangemaakt",
                   f"{lic.describe(payload)} voor {email}\n(gekopieerd naar klembord)\n\n{key}")
    threading.Thread(target=worker, daemon=True).start()


def _admin_show(fn_name, title):
    def action(icon_, item):
        def worker():
            import admin
            _show_text(title, getattr(admin, fn_name)())
        threading.Thread(target=worker, daemon=True).start()
    return action


def admin_verify_action(icon_, item):
    def worker():
        import admin
        key = _ask("Admin — verifiëren", "Plak een sleutel:")
        if key:
            _show_text("Admin — verificatie", admin.verify_text(key.strip()))
    threading.Thread(target=worker, daemon=True).start()


def admin_revoke_action(icon_, item):
    def worker():
        import admin
        kid = _ask("Admin — intrekken", "ID om in te trekken (zie 'Sleutels tonen'):")
        if kid:
            _show_text("Admin — intrekken", admin.revoke(kid.strip()))
    threading.Thread(target=worker, daemon=True).start()


def _edit_file_tk(path, title, label, default=""):
    import tkinter as tk
    from tkinter.scrolledtext import ScrolledText
    try:
        content = Path(path).read_text(encoding="utf-8")
    except Exception:
        content = default
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.configure(bg=UI_PAPER)
    tk.Label(root, text=label, anchor="w", justify="left", bg=UI_PAPER,
             fg=UI_INK, font=UI_FONT).pack(fill="x", padx=14, pady=(14, 6))
    box = ScrolledText(root, width=58, height=18, font=UI_MONO, bg="#ffffff",
                       fg=UI_INK, insertbackground=UI_INK, relief="flat",
                       borderwidth=0, padx=12, pady=10)
    box.pack(fill="both", expand=True, padx=14)
    box.insert("1.0", content)

    def save():
        try:
            Path(path).write_text(box.get("1.0", "end-1c"), encoding="utf-8")
            state["last"] = "Opgeslagen ✓"
        except Exception as e:
            state["last"] = f"Opslaan mislukt: {e}"
        root.destroy()
        refresh()

    tk.Button(root, text="Opslaan", command=save, bg=UI_ACCENT, fg="white",
              activebackground=UI_ACCENT2, activeforeground="white", relief="flat",
              font=("Segoe UI", 10, "bold"), padx=20, pady=7, cursor="hand2",
              borderwidth=0).pack(pady=12)
    root.mainloop()


def _edit_file_action(path, title, label, default=""):
    """Factory: maakt een menu-actie die een tekstbestand in een editor opent."""
    def action(icon_, item):
        if IS_MAC:   # apart proces (Tk niet op een worker-thread)
            _dlg_subprocess("edit", path=str(path), title=title, label=label, default=default)
            refresh()
            return
        threading.Thread(target=lambda: _edit_file_tk(path, title, label, default), daemon=True).start()
    return action


# ── Instellingen-venster + onboarding (in de site-stijl) ────────────────
def _dropdown(tk, parent, label, choices, current):
    cur = next((l for l, v in choices if v == current), choices[0][0])
    tk.Label(parent, text=label, bg=UI_PAPER, fg=UI_INK, font=UI_FONT, anchor="w").pack(fill="x", padx=18, pady=(10, 2))
    var = tk.StringVar(master=parent, value=cur)

    # Lange lijsten (talen, ~99) → scrollbare combobox; typ een letter om te springen.
    if len(choices) > 12:
        from tkinter import ttk
        cb = ttk.Combobox(parent, textvariable=var, values=[l for l, _ in choices],
                          state="readonly", font=UI_FONT, height=18)
        cb.pack(fill="x", padx=18, ipady=2)
        return lambda: next((v for l, v in choices if l == var.get()), current)

    # Korte lijsten → klassieke OptionMenu in de site-stijl.
    om = tk.OptionMenu(parent, var, *[l for l, _ in choices])
    om.configure(bg="#ffffff", fg=UI_INK, font=UI_FONT, relief="flat", anchor="w",
                 highlightthickness=1, highlightbackground="#dedbd3", activebackground="#efecfd")
    om["menu"].configure(bg="#ffffff", fg=UI_INK, font=UI_FONT, activebackground=UI_ACCENT, activeforeground="white")
    om.pack(fill="x", padx=18)
    return lambda: next((v for l, v in choices if l == var.get()), choices[0][1])


def _checkbox(tk, parent, label, initial):
    """Aan/uit-vinkje in de site-stijl. Geeft een getter terug (bool)."""
    var = tk.BooleanVar(master=parent, value=bool(initial))
    cb = tk.Checkbutton(parent, text=label, variable=var, bg=UI_PAPER, fg=UI_INK,
                        font=UI_FONT, anchor="w", activebackground=UI_PAPER,
                        activeforeground=UI_INK, selectcolor="#ffffff",
                        highlightthickness=0, bd=0, cursor="hand2")
    cb.pack(fill="x", padx=16, pady=(6, 0))
    return lambda: bool(var.get())


def _accent_btn(tk, parent, text, cmd):
    return tk.Button(parent, text=text, command=cmd, bg=UI_ACCENT, fg="white", relief="flat",
                     activebackground=UI_ACCENT2, activeforeground="white", borderwidth=0,
                     font=("Segoe UI", 10, "bold"), padx=22, pady=7, cursor="hand2")


def _ghost_btn(tk, parent, text, cmd):
    return tk.Button(parent, text=text, command=cmd, bg=UI_PAPER, fg=UI_INK, relief="flat",
                     activebackground=UI_PAPER, activeforeground=UI_ACCENT, borderwidth=0,
                     font=UI_FONT, cursor="hand2")


_TK_KEY_TO_PART = {
    "Control_R": ("ctrl", "ctrl_r"), "Control_L": ("ctrl", "ctrl_l"),
    "Alt_R":     ("alt",  "alt_r"),  "Alt_L":     ("alt",  "alt_l"),
    "Shift_R":   ("shift","shift_r"),
    "Super_L":   ("win",  "win"),    "Super_R":   ("win",  "win"),
    "Meta_L":    ("win",  "win"),    "Meta_R":    ("win",  "win"),
    "F8": ("f8","f8"), "F9": ("f9","f9"), "F10": ("f10","f10"),
}


def _tk_syms_to_spec(syms):
    """Zet een set Tkinter keysyms om naar een hotkey-spec ('ctrl+win', 'ctrl_r', …)."""
    groups = {}
    for s in syms:
        if s in _TK_KEY_TO_PART:
            g, exact = _TK_KEY_TO_PART[s]
            if g not in groups:
                groups[g] = exact
    if not groups:
        return None
    if len(groups) == 1:
        return list(groups.values())[0]
    order = ["ctrl", "win", "alt", "shift", "f8", "f9", "f10"]
    return "+".join(g for g in order if g in groups) or None


def _keypicker(tk, parent, label, initial):
    """Druk-op-een-toets widget. Klik 'Wijzig…' → druk combo → sla op.
    Op macOS: gebruik dropdown (AppKit-conflict met Tkinter event-capture → crash)."""
    initial = initial or HOTKEY_CHOICES[0][1]
    if IS_MAC:
        return _dropdown(tk, parent, label, HOTKEY_CHOICES, initial)
    val = {"v": initial}
    tk.Label(parent, text=label, bg=UI_PAPER, fg=UI_INK, font=UI_FONT,
             anchor="w").pack(fill="x", padx=18, pady=(10, 2))
    row = tk.Frame(parent, bg=UI_PAPER)
    row.pack(fill="x", padx=18)
    disp_var = tk.StringVar(master=parent, value=_hk_display(initial))
    tk.Label(row, textvariable=disp_var, bg="#ffffff", fg=UI_INK, font=UI_FONT,
             anchor="w", padx=10, pady=5, relief="flat",
             highlightthickness=1, highlightbackground="#dedbd3").pack(side="left", fill="x", expand=True)
    btn = tk.Button(row, text="Change…", bg=UI_PAPER, fg=UI_ACCENT, relief="flat",
                    font=UI_FONT, cursor="hand2", padx=8)
    btn.pack(side="left", padx=(6, 0))
    tk.Button(row, text="Off", bg=UI_PAPER, fg=UI_SUB, relief="flat",
              font=UI_FONT, cursor="hand2", padx=4,
              command=lambda: (val.update(v="uit"), disp_var.set(_hk_display("uit")))).pack(side="left", padx=(2, 0))

    _valid = {v for _, v in HOTKEY_CHOICES}

    def _pynput_to_spec(keys):
        """Zet een set pynput-keys om naar een hotkey-spec ('ctrl+win', 'ctrl_r', …)."""
        from pynput.keyboard import Key as K
        exact_map = {
            K.ctrl_r:  "ctrl_r",  K.ctrl_l:  "ctrl_l",
            K.alt_r:   "alt_r",   K.alt_l:   "alt_l",
            K.shift_r: "shift_r",
        }
        order = ["ctrl", "win", "alt", "shift"]
        group_hit = {}
        for k in keys:
            for g, members in MOD_GROUPS.items():
                if k in members:
                    canonical = "win" if g == "cmd" else g
                    group_hit[canonical] = k
                    break
            else:
                for fn in ("f8", "f9", "f10"):
                    if k == getattr(K, fn, None):
                        group_hit[fn] = k
        unique = [g for g in order + ["f8", "f9", "f10"] if g in group_hit]
        if not unique:
            return None
        if len(unique) == 1:
            g = unique[0]
            if g in ("f8", "f9", "f10"):
                return g
            for k in keys:
                if k in exact_map:
                    return exact_map[k]
        return "+".join(g for g in order if g in group_hit) or None

    def _finish(spec):
        global _keypicking
        _keypicking = False
        pressed.clear()
        btn.config(text="Wijzig…", state="normal")
        if spec and spec in _valid:
            val["v"] = spec
        disp_var.set(_hk_display(val["v"]))

    def _start():
        global _keypicking
        _keypicking = True
        pressed.clear()
        btn.config(text="Druk nu…", state="disabled")
        disp_var.set("—")

        if IS_MAC:
            # Op macOS: gebruik Tkinter-bindings (pynput vereist Accessibility-rechten
            # en conflicteert met de Tk event-loop → crash).
            toplevel = parent.winfo_toplevel()
            _held = set()
            _bid_p = [None]; _bid_r = [None]

            def _cleanup():
                try:
                    if _bid_p[0]: toplevel.unbind("<KeyPress>", _bid_p[0])
                    if _bid_r[0]: toplevel.unbind("<KeyRelease>", _bid_r[0])
                except Exception:
                    pass

            def _on_tk_press(event):
                _held.add(event.keysym)

            def _on_tk_release(event):
                syms = set(_held) | {event.keysym}
                _held.clear()
                _cleanup()
                spec = _tk_syms_to_spec(syms)
                try:
                    toplevel.after(0, lambda: _finish(spec))
                except Exception:
                    _finish(spec)

            _bid_p[0] = toplevel.bind("<KeyPress>", _on_tk_press, add=True)
            _bid_r[0] = toplevel.bind("<KeyRelease>", _on_tk_release, add=True)
            toplevel.focus_force()
        else:
            _captured = set()
            _done = [False]

            def _on_press(key):
                if not _done[0]:
                    _captured.add(key)

            def _on_release(key):
                if _done[0]:
                    return False
                _done[0] = True
                spec = _pynput_to_spec(set(_captured) | {key})
                try:
                    parent.winfo_toplevel().after(0, lambda: _finish(spec))
                except Exception:
                    _finish(spec)
                return False

            keyboard.Listener(on_press=_on_press, on_release=_on_release).start()

    btn.config(command=_start)
    return lambda: val["v"]


def open_dashboard(start_tab="instellingen"):
    """Volledig instellingen-dashboard (tabbladen). Draait op de HUIDIGE thread."""
    import tkinter as tk

    root = tk.Tk()
    root.title("Lazytype")
    root.configure(bg=UI_PAPER)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    W = 620   # breed genoeg voor de 7 tabs (tabbalk vroeg ~558px → 'Over' viel net buiten beeld)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    H = min(880, sh - 80)   # ruim: scrollen is meestal niet meer nodig (blijft als vangnet)
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")

    # ── Tabbalk ──────────────────────────────────────────────────────────
    tab_bar = tk.Frame(root, bg=UI_PAPER)
    tab_bar.pack(fill="x")
    tk.Frame(tab_bar, bg=UI_LINE, height=1).pack(side="bottom", fill="x")

    content = tk.Frame(root, bg=UI_PAPER)
    content.pack(fill="both", expand=True)

    tab_frames = {}    # tid -> binnenframe (hierin wordt de inhoud gebouwd)
    tab_pages  = {}    # tid -> holder (canvas + scrollbar; dit toont/verbergt show_tab)
    tab_canvas = {}    # tid -> canvas (voor muiswiel-scroll)
    tab_btns   = {}
    active     = {"tab": None}

    def show_tab(name):
        for p in tab_pages.values():
            p.pack_forget()
        tab_pages[name].pack(fill="both", expand=True)
        active["tab"] = name
        # scrollregio herberekenen nu de tab zichtbaar/gemeten is
        cv = tab_canvas[name]
        cv.update_idletasks()
        cv.configure(scrollregion=cv.bbox("all"))
        cv.yview_moveto(0.0)
        for n, b in tab_btns.items():
            if n == name:
                b.config(fg=UI_ACCENT, font=("Segoe UI", 10, "bold"),
                         relief="flat", bd=0, cursor="arrow")
            else:
                b.config(fg=UI_SUB, font=UI_FONT,
                         relief="flat", bd=0, cursor="hand2")

    def _bind_wheel(widget, cv):
        """Bind muiswiel-scroll op widget + alle kinderen (Text-velden scrollen zelf)."""
        def _w(e):
            if e.delta:
                cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
            return "break"
        stack = [widget]
        while stack:
            w = stack.pop()
            if w.winfo_class() == "Text":      # ScrolledText: eigen scroll behouden
                continue
            try: w.bind("<MouseWheel>", _w)
            except Exception: pass
            stack.extend(w.winfo_children())

    for tid, tlabel in [("statistieken", "Statistieken"),
                         ("abonnement", "Abonnement"),
                         ("instellingen", "Instellingen"),
                         ("woorden", "Woorden"),
                         ("modi", "Modi"),
                         ("status", "Status"),
                         ("over", "Over")]:
        holder = tk.Frame(content, bg=UI_PAPER)
        cv = tk.Canvas(holder, bg=UI_PAPER, highlightthickness=0)
        vb = tk.Scrollbar(holder, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=vb.set)
        vb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        f = tk.Frame(cv, bg=UI_PAPER)
        wid = cv.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>", lambda e, c=cv: c.configure(scrollregion=c.bbox("all")))
        cv.bind("<Configure>", lambda e, c=cv, w=wid: c.itemconfigure(w, width=e.width))
        tab_frames[tid] = f
        tab_pages[tid]  = holder
        tab_canvas[tid] = cv
        b = tk.Button(tab_bar, text=tlabel, bg=UI_PAPER, relief="flat", bd=0,
                      padx=12, pady=10, activebackground=UI_PAPER,
                      activeforeground=UI_ACCENT,
                      command=lambda t=tid: show_tab(t))
        b.pack(side="left")
        tab_btns[tid] = b

    def section(parent, text):
        tk.Label(parent, text=text, bg=UI_PAPER, fg=UI_INK,
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(
                 fill="x", padx=20, pady=(18, 4))

    def divider(parent):
        tk.Frame(parent, bg=UI_LINE, height=1).pack(fill="x", padx=20, pady=(10, 2))

    # ── TAB: Statistieken ─────────────────────────────────────────────────
    sta = tab_frames["statistieken"]
    _usage = dictate.usage_summary()

    def _fmt_words(n):
        return f"{int(n):,}".replace(",", ".")

    def _fmt_dur(sec):
        sec = int(sec); h, m = sec // 3600, (sec % 3600) // 60
        if h >= 1: return f"{h} uur {m} min"
        if m >= 1: return f"{m} min"
        return f"{sec} sec"

    def _kind_label(k):
        return {"dictaat": "Dictaat", "command": "Command", "vertalen": "Vertalen"}.get(
            k, (k[5:] + " (mode)") if k.startswith("mode:") else k)

    section(sta, "Jouw cijfers")
    cards = tk.Frame(sta, bg=UI_PAPER)
    cards.pack(fill="x", padx=16, pady=(4, 0))
    for _i in range(3):
        cards.columnconfigure(_i, weight=1)
    for _col, (_pk, _plabel) in enumerate([("7d", "Afgelopen 7 dagen"),
                                           ("30d", "Afgelopen maand"),
                                           ("365d", "Afgelopen jaar")]):
        _d = _usage[_pk]
        card = tk.Frame(cards, bg="#f0efe9")
        card.grid(row=0, column=_col, padx=4, sticky="nsew")
        tk.Label(card, text=_plabel, bg="#f0efe9", fg=UI_SUB, font=("Segoe UI", 9),
                 anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(card, text=_fmt_words(_d["words"]), bg="#f0efe9", fg=UI_ACCENT,
                 font=("Segoe UI", 20, "bold"), anchor="w").pack(fill="x", padx=10)
        tk.Label(card, text="woorden", bg="#f0efe9", fg=UI_SUB, font=("Segoe UI", 9),
                 anchor="w").pack(fill="x", padx=10)
        tk.Label(card, text="⏱ " + _fmt_dur(_d["saved_sec"]) + " bespaard", bg="#f0efe9",
                 fg=UI_INK, font=("Segoe UI", 9, "bold"), anchor="w").pack(
                 fill="x", padx=10, pady=(6, 8))

    tk.Label(sta, text=f"Bespaarde tijd: typen (~{dictate.TYPING_WPM} wpm) vs. spreken "
                       f"(~{dictate.SPEAKING_WPM} wpm).",
             bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 8), anchor="w").pack(
             fill="x", padx=20, pady=(8, 0))

    divider(sta)
    section(sta, "Per functie  (woorden)")
    if _usage["all"]["words"] <= 0:
        tk.Label(sta, text="Nog geen dictaten — begin met praten en je cijfers verschijnen hier.",
                 bg=UI_PAPER, fg=UI_SUB, font=UI_FONT, anchor="w").pack(fill="x", padx=20, pady=(6, 0))
    else:
        _hr = tk.Frame(sta, bg=UI_PAPER); _hr.pack(fill="x", padx=20, pady=(2, 2))
        tk.Label(_hr, text="", bg=UI_PAPER, width=20, anchor="w").pack(side="left")
        for _ct in ("7 dgn", "maand", "jaar"):
            tk.Label(_hr, text=_ct, bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 8, "bold"),
                     width=9, anchor="e").pack(side="left")
        _kinds = sorted(_usage["all"]["by_kind"], key=lambda k: -_usage["all"]["by_kind"][k])
        for _k in _kinds:
            _r = tk.Frame(sta, bg=UI_PAPER); _r.pack(fill="x", padx=20, pady=1)
            tk.Label(_r, text=_kind_label(_k), bg=UI_PAPER, fg=UI_INK, font=("Segoe UI", 9),
                     width=20, anchor="w").pack(side="left")
            for _pk in ("7d", "30d", "365d"):
                tk.Label(_r, text=_fmt_words(_usage[_pk]["by_kind"].get(_k, 0)), bg=UI_PAPER,
                         fg=UI_INK, font=("Segoe UI", 9), width=9, anchor="e").pack(side="left")

    # ── TAB: Abonnement ───────────────────────────────────────────────────
    ab = tab_frames["abonnement"]
    section(ab, "Abonnement")

    lst = dictate.license_state()
    tier_color = {"pro": "#1f9d57", "trial": UI_ACCENT, "personal": "#1f9d57",
                  "owner": "#1f9d57"}.get(lst["tier"], UI_SUB)
    tier_dot = "●"
    tk.Label(ab, text=f"{tier_dot}  {lst['label']}", bg=UI_PAPER,
             fg=tier_color, font=("Segoe UI", 13, "bold"), anchor="w").pack(
             fill="x", padx=20, pady=(4, 0))

    if lst["tier"] == "trial" and lst.get("days_left") is not None:
        tk.Label(ab, text=f"Nog {lst['days_left']} dag{'en' if lst['days_left'] != 1 else ''} resterend",
                 bg=UI_PAPER, fg=UI_SUB, font=UI_FONT, anchor="w").pack(fill="x", padx=20)

    if not lst["valid"] or lst["tier"] in ("trial",):
        divider(ab)
        section(ab, "Upgraden")
        tk.Label(ab, text="Upgrade naar Personal (eenmalig) of Pro (abonnement)\nvoor onbeperkt gebruik.",
                 bg=UI_PAPER, fg=UI_SUB, font=UI_FONT, justify="left", anchor="w").pack(
                 fill="x", padx=20)
        _accent_btn(tk, ab, "Bekijk abonnementen →",
                    lambda: __import__("webbrowser").open("https://lazytype.com/#pricing")).pack(
                    padx=20, pady=(10, 0), anchor="w")

    divider(ab)
    section(ab, "Licentiesleutel")
    key_val = os.environ.get("LAZYTYPE_LICENSE", "")
    key_disp = (key_val[:28] + "…") if len(key_val) > 30 else (key_val or "—")
    tk.Label(ab, text=key_disp, bg=UI_PAPER, fg=UI_INK,
             font=UI_MONO, anchor="w").pack(fill="x", padx=20)
    _ghost_btn(tk, ab, "Sleutel wijzigen…",
               lambda: set_license_action(None, None)).pack(padx=20, pady=(6, 0), anchor="w")

    if lst["tier"] == "personal":
        divider(ab)
        section(ab, "Groq API-key")
        groq_val = os.environ.get("GROQ_API_KEY", "")
        groq_disp = (groq_val[:20] + "…") if len(groq_val) > 22 else (groq_val or "—")
        tk.Label(ab, text=groq_disp, bg=UI_PAPER, fg=UI_INK,
                 font=UI_MONO, anchor="w").pack(fill="x", padx=20)
        _ghost_btn(tk, ab, "API-key wijzigen…",
                   lambda: set_key_action(None, None)).pack(padx=20, pady=(6, 0), anchor="w")

    # ── TAB: Instellingen ─────────────────────────────────────────────────
    inst = tab_frames["instellingen"]

    def save_settings():
        global _keypicking
        _keypicking = False
        pressed.clear()
        hk_d, hk_c, hk_t = g_dict(), g_cmd(), g_tr()
        actieve = [v for v in (hk_d, hk_c, hk_t) if v and v != "uit"]
        if len(actieve) != len(set(actieve)):
            import tkinter.messagebox as mb
            mb.showwarning("Dubbele sneltoets",
                           "Twee sneltoetsen zijn hetzelfde. Kies unieke toetsen voor dicteren, command en vertalen.",
                           parent=root)
            return
        state["hotkey_name"]      = hk_d
        state["hotkey_command"]   = hk_c
        state["hotkey_translate"] = hk_t
        state["language"]         = g_lang()
        state["translate_target"] = g_tgt()
        state["engine"]           = g_eng()
        for k, envk in (("hotkey_name",      "DICTATE_HOTKEY"),
                        ("hotkey_command",    "DICTATE_COMMAND_HOTKEY"),
                        ("hotkey_translate",  "DICTATE_TRANSLATE_HOTKEY"),
                        ("translate_target",  "DICTATE_TRANSLATE_TARGET"),
                        ("language",          "DICTATE_LANGUAGE"),
                        ("engine",            "DICTATE_ENGINE")):
            dictate.save_env_value(envk, state[k])
        # Nabewerking & gedrag
        state["postprocess"] = g_pp()
        dictate.save_env_value("DICTATE_POSTPROCESS", state["postprocess"])
        state["context"] = g_context()
        dictate.save_env_value("DICTATE_CONTEXT", "1" if state["context"] else "0")
        state["realtime"] = g_realtime()
        dictate.save_env_value("DICTATE_REALTIME", "1" if state["realtime"] else "0")
        new_overlay = g_overlay()
        if new_overlay != state["overlay"]:
            state["overlay"] = new_overlay
            dictate.save_env_value("DICTATE_OVERLAY", "1" if new_overlay else "0")
            overlay_ui.start() if new_overlay else overlay_ui.stop()
        state["history"] = g_history()
        dictate.save_env_value("DICTATE_HISTORY", "1" if state["history"] else "0")
        if autostart_supported():
            _want = g_autostart()
            if _want is not None:
                dictate.save_env_value("DICTATE_AUTOSTART_OPTOUT", "0" if _want else "1")  # keuze onthouden
                if _want != is_autostart():
                    set_autostart(_want)
        rebuild_hotkeys()
        if icon:
            state["last"] = "Instellingen opgeslagen ✓"
            refresh()
        root.destroy()

    # Save-balk altijd onderaan vastgepind (buiten de scroll-content)
    btn_bar = tk.Frame(inst, bg=UI_PAPER)
    btn_bar.pack(side="bottom", fill="x", padx=20, pady=12)
    tk.Frame(inst, bg=UI_LINE, height=1).pack(side="bottom", fill="x")
    _accent_btn(tk, btn_bar, "Opslaan", save_settings).pack(side="right")
    _ghost_btn(tk, btn_bar, "Annuleren", root.destroy).pack(side="right", padx=(0, 8))

    # Scrollbare inhoud
    scr_frame = tk.Frame(inst, bg=UI_PAPER)
    scr_frame.pack(fill="both", expand=True)

    section(scr_frame, "Sneltoetsen")
    g_dict = _keypicker(tk, scr_frame, "Dicteren — houd in en spreek", state["hotkey_name"])
    g_cmd  = _keypicker(tk, scr_frame, "Command — selecteer tekst + spreek instructie", state["hotkey_command"])
    g_tr   = _keypicker(tk, scr_frame, "Vertalen — dicteren + meteen vertalen", state["hotkey_translate"])
    divider(scr_frame)
    section(scr_frame, "Taal & Engine")
    g_lang = _dropdown(tk, scr_frame, "Spreektaal", SPOKEN_CHOICES, state["language"])
    g_tgt  = _dropdown(tk, scr_frame, "Vertaal naar", LANG_CHOICES, state["translate_target"])
    g_eng  = _dropdown(tk, scr_frame, "Engine", ENGINE_CHOICES, state["engine"])
    divider(scr_frame)
    section(scr_frame, "Nabewerking & gedrag")
    g_pp = _dropdown(tk, scr_frame, "AI-nabewerking (normaal dictaat)", POSTPROC_CHOICES, state["postprocess"])
    g_context = _checkbox(tk, scr_frame, "Context-bewuste toon (zakelijk in e-mail, casual in chat)", state["context"])
    g_realtime = _checkbox(tk, scr_frame, "Realtime preview-balk (live tekst tijdens spreken — meer API-calls)", state["realtime"])
    g_overlay = _checkbox(tk, scr_frame, "Overlay-balkje tonen", state["overlay"])
    g_history = _checkbox(tk, scr_frame, "Dicteer-geschiedenis bewaren", state["history"])
    g_autostart = (_checkbox(tk, scr_frame, "Automatisch starten bij inloggen", is_autostart())
                   if autostart_supported() else (lambda: None))

    # ── TAB: Woorden (woordenboek & snippets) ─────────────────────────────
    wd = tab_frames["woorden"]
    from tkinter.scrolledtext import ScrolledText

    def _file_editor(parent, path, title, hint, default=""):
        section(parent, title)
        tk.Label(parent, text=hint, bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=W - 48).pack(fill="x", padx=20)
        box = ScrolledText(parent, height=7, font=UI_MONO, bg="#ffffff", fg=UI_INK,
                           insertbackground=UI_INK, relief="flat", borderwidth=0,
                           highlightthickness=1, highlightbackground="#dedbd3", padx=8, pady=6)
        box.pack(fill="x", padx=20, pady=(4, 0))
        try:
            box.insert("1.0", path.read_text(encoding="utf-8"))
        except Exception:
            box.insert("1.0", default)

        def _save():
            try:
                path.write_text(box.get("1.0", "end-1c"), encoding="utf-8")
                msg = "Opgeslagen ✓"
            except Exception as e:
                msg = f"Opslaan mislukt: {e}"
            if icon:
                state["last"] = msg
                refresh()
        _accent_btn(tk, parent, "Opslaan", _save).pack(padx=20, pady=(6, 0), anchor="w")

    _file_editor(wd, dictate.DICTIONARY_FILE, "Woordenboek",
                 "Eén term per regel (namen/jargon). Verbetert de herkenning.",
                 "# Eén term per regel: namen, jargon, afkortingen.\n")
    divider(wd)
    _file_editor(wd, dictate.SNIPPETS_FILE, "Snippets",
                 "Per regel:  trigger = tekst   (\\n voor een nieuwe regel).",
                 "# trigger = uit te vouwen tekst\n# bijv:  mijn agenda = https://calendly.com/bas\n")

    # ── TAB: Modi (AI-modi met eigen sneltoets) ───────────────────────────
    def _ph_entry(parent, placeholder, value="", **kw):
        """tk.Entry met grijze voorbeeldtekst (placeholder) zolang 'ie leeg is.
        Geeft (widget, getter) terug; de getter levert "" als alleen de
        voorbeeldtekst nog staat."""
        e = tk.Entry(parent, fg=UI_INK, **kw)
        st = {"ph": False}
        def _show():
            e.delete(0, "end"); e.insert(0, placeholder); e.config(fg="#9a978f"); st["ph"] = True
        def _clear():
            if st["ph"]:
                e.delete(0, "end"); e.config(fg=UI_INK); st["ph"] = False
        e.bind("<FocusIn>", lambda _e: _clear())
        e.bind("<FocusOut>", lambda _e: (None if e.get() else _show()))
        if value:
            e.insert(0, value)
        else:
            _show()
        return e, (lambda: "" if st["ph"] else e.get().strip())

    md2 = tab_frames["modi"]
    section(md2, "AI-modi")
    tk.Label(md2, text="Een AI-modus is een opgeslagen instructie met een eigen sneltoets. "
                       "Selecteer wat tekst (of gebruik je laatste dictaat), druk de sneltoets, "
                       "en de AI bewerkt die tekst volgens jouw instructie — zónder te praten.",
             bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9), justify="left",
             anchor="w", wraplength=W - 48).pack(fill="x", padx=20, pady=(0, 2))
    tk.Label(md2, text="Voorbeelden: “maak deze tekst formeel” · “vat samen in 3 bullets” · "
                       "“vertaal naar het Engels” · “corrigeer spelling en grammatica”.",
             bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 8, "italic"), justify="left",
             anchor="w", wraplength=W - 48).pack(fill="x", padx=20, pady=(0, 2))

    modes_rows = []
    modes_container = tk.Frame(md2, bg=UI_PAPER)
    modes_container.pack(fill="both", expand=True, pady=(4, 0))

    def _add_mode_row(name="", instruction="", hotkey="uit"):
        rowf = tk.Frame(modes_container, bg="#f0efe9")
        rowf.pack(fill="x", padx=20, pady=(8, 0))
        top = tk.Frame(rowf, bg="#f0efe9")
        top.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(top, text="Naam", bg="#f0efe9", fg=UI_SUB,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 6))
        nm_e, nm_get = _ph_entry(top, "Formele e-mail", value=name,
                                 font=("Segoe UI", 10, "bold"), relief="flat", bg="#ffffff",
                                 insertbackground=UI_INK, highlightthickness=1,
                                 highlightbackground="#dedbd3")
        nm_e.pack(side="left", fill="x", expand=True, ipady=3)
        rec = {"name": nm_get}

        def _rm():
            rowf.destroy()
            if rec in modes_rows:
                modes_rows.remove(rec)
        tk.Button(top, text="✕", command=_rm, bg="#f0efe9", fg=UI_SUB, relief="flat",
                  bd=0, cursor="hand2", font=("Segoe UI", 10)).pack(side="left", padx=(6, 0))

        tk.Label(rowf, text="Instructie — wat moet de AI met je tekst doen?",
                 bg="#f0efe9", fg=UI_SUB, font=("Segoe UI", 8), anchor="w").pack(
                 fill="x", padx=8, pady=(6, 0))
        instr_e, instr_get = _ph_entry(rowf, "Herschrijf deze tekst professioneel, foutloos en beknopt",
                                       value=instruction, font=UI_FONT, relief="flat", bg="#ffffff",
                                       insertbackground=UI_INK, highlightthickness=1,
                                       highlightbackground="#dedbd3")
        instr_e.pack(fill="x", ipady=3, padx=8, pady=(2, 0))
        rec["instr"] = instr_get
        rec["hk"] = _dropdown(tk, rowf, "Sneltoets (de toets die deze modus activeert)",
                              HOTKEY_CHOICES, hotkey or "uit")
        tk.Frame(rowf, bg="#f0efe9", height=6).pack()
        modes_rows.append(rec)
        _bind_wheel(rowf, tab_canvas["modi"])   # muiswiel ook op nieuwe rij

    for _m in dictate.load_modes():
        _add_mode_row(_m.get("name", ""), _m.get("instruction", ""), _m.get("hotkey", "uit") or "uit")

    mbtn = tk.Frame(md2, bg=UI_PAPER)
    mbtn.pack(side="bottom", fill="x", padx=20, pady=12)

    def _save_modes():
        out = []
        for r in modes_rows:
            instr = r["instr"]().strip()
            if not instr:
                continue
            out.append({"name": r["name"]().strip(), "instruction": instr, "hotkey": r["hk"]()})
        # Sneltoetsen mogen niet botsen met dicteren/command/vertalen of elkaar.
        hks = [m["hotkey"] for m in out if m["hotkey"] and m["hotkey"] != "uit"]
        base = [state["hotkey_name"], state["hotkey_command"], state["hotkey_translate"]]
        allhk = hks + [h for h in base if h and h != "uit"]
        if len(allhk) != len(set(allhk)):
            import tkinter.messagebox as mb
            mb.showwarning("Dubbele sneltoets", "Een mode-sneltoets botst met een andere toets. "
                           "Kies unieke toetsen.", parent=root)
            return
        dictate.save_modes(out)
        rebuild_hotkeys()
        if icon:
            state["last"] = "Modi opgeslagen ✓"
            refresh()
        root.destroy()
    _accent_btn(tk, mbtn, "Opslaan", _save_modes).pack(side="right")
    _ghost_btn(tk, mbtn, "+ Mode toevoegen", lambda: _add_mode_row()).pack(side="left")

    # ── TAB: Status (read-only diagnostiek) ───────────────────────────────
    stt = tab_frames["status"]
    section(stt, "Status & diagnostiek")
    _ls = dictate.license_state()
    _pp = state["postprocess"]
    _pp_disp = ("Uit" if _pp in ("off", "") else
                "Opschonen" if _pp == "clean" else "Vertaal → " + LANG_DISPLAY.get(_pp, _pp))
    _rows = [
        ("Versie", APP_VERSION),
        ("Licentie", _ls.get("label", "—")),
        ("Tier", _ls.get("tier", "—")),
        ("Dagen resterend", str(_ls.get("days_left")) if _ls.get("days_left") is not None else "—"),
        ("Engine", state["engine"]),
        ("Spreektaal", LANG_DISPLAY.get(state["language"], state["language"])),
        ("Vertaaldoel", LANG_DISPLAY.get(state["translate_target"], state["translate_target"])),
        ("AI-nabewerking", _pp_disp),
        ("Context-toon", "aan" if state["context"] else "uit"),
        ("Realtime preview", "aan" if state["realtime"] else "uit"),
        ("Dicteer-toets", _hk_display(state["hotkey_name"])),
        ("Command-toets", _hk_display(state["hotkey_command"])),
        ("Vertaal-toets", _hk_display(state["hotkey_translate"])),
        ("Overlay", "aan" if state["overlay"] else "uit"),
        ("Geschiedenis", "aan" if state["history"] else "uit"),
        ("Autostart", ("aan" if is_autostart() else "uit") if autostart_supported() else "n.v.t."),
        ("Apparaat-id", dictate.ensure_device_id()),
        ("Config-map", str(dictate.ROOT)),
        ("Laatste dictaat", (state.get("last_dictation") or "—")),
    ]

    def _statrow(parent, k, v):
        row = tk.Frame(parent, bg=UI_PAPER)
        row.pack(fill="x", padx=20, pady=1)
        tk.Label(row, text=k, bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9),
                 width=16, anchor="w").pack(side="left")
        tk.Label(row, text=str(v), bg=UI_PAPER, fg=UI_INK, font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=W - 190).pack(side="left", fill="x", expand=True)

    for _k, _v in _rows:
        _statrow(stt, _k, _v)

    def _copy_diag():
        try:
            dictate._clipboard_set("\n".join(f"{k}: {v}" for k, v in _rows))
            if icon:
                state["last"] = "Diagnostiek gekopieerd ✓"
                refresh()
        except Exception:
            pass
    _ghost_btn(tk, stt, "Kopieer diagnostiek", _copy_diag).pack(padx=20, pady=(14, 0), anchor="w")

    # ── TAB: Over & Update ────────────────────────────────────────────────
    ov = tab_frames["over"]
    section(ov, "Over Lazytype")
    tk.Label(ov, text=f"Versie {APP_VERSION}", bg=UI_PAPER, fg=UI_INK,
             font=("Segoe UI", 12, "bold"), anchor="w").pack(fill="x", padx=20)
    tk.Label(ov, text="Dicteersoftware voor Windows en macOS",
             bg=UI_PAPER, fg=UI_SUB, font=UI_FONT, anchor="w").pack(fill="x", padx=20)

    divider(ov)
    section(ov, "Update")
    upd_var = tk.StringVar()
    upd_lbl = tk.Label(ov, textvariable=upd_var, bg=UI_PAPER, fg=UI_SUB,
                       font=UI_FONT, anchor="w")
    upd_lbl.pack(fill="x", padx=20)
    upd_btn_frame = tk.Frame(ov, bg=UI_PAPER)
    upd_btn_frame.pack(fill="x", padx=20, pady=(8, 0))

    def check_now():
        for w in upd_btn_frame.winfo_children():   # wis vorige knoppen → geen dubbele
            w.destroy()
        upd_var.set("Controleren…")
        root.update()
        try:
            import requests
            r = requests.get("https://lazytype.com/version.json", timeout=8)
            latest = r.json().get("version", "")
            if latest and latest != APP_VERSION:
                upd_var.set(f"Nieuwe versie beschikbaar: v{latest}")
                upd_lbl.config(fg="#1f9d57")
                if IS_WIN and getattr(sys, "frozen", False):
                    _accent_btn(tk, upd_btn_frame, f"Installeer v{latest}",
                                lambda v=latest: _install_update(v, root)).pack(side="left")
                else:
                    _ghost_btn(tk, upd_btn_frame, "Download →",
                               lambda: __import__("webbrowser").open("https://lazytype.com")).pack(side="left")
            else:
                upd_var.set("Je hebt de nieuwste versie.")
        except Exception:
            upd_var.set("Kan niet controleren — check je internetverbinding.")

    if _update_info:
        upd_var.set(f"Nieuwe versie beschikbaar: v{_update_info}")
        upd_lbl.config(fg="#1f9d57")
        if IS_WIN and getattr(sys, "frozen", False):
            _accent_btn(tk, upd_btn_frame, f"Installeer v{_update_info}",
                        lambda v=_update_info: _install_update(v, root)).pack(side="left")
        else:
            _ghost_btn(tk, upd_btn_frame, "Download →",
                       lambda: __import__("webbrowser").open("https://lazytype.com")).pack(side="left")
    else:
        upd_var.set("Je hebt de nieuwste versie.")
        _ghost_btn(tk, upd_btn_frame, "Controleer op updates",
                   lambda: threading.Thread(target=check_now, daemon=True).start()).pack(side="left")

    divider(ov)
    tk.Label(ov, text="© Lazytype — lazytype.com",
             bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9), anchor="w").pack(
             fill="x", padx=20, pady=(8, 0))
    _ghost_btn(tk, ov, "Website openen",
               lambda: __import__("webbrowser").open("https://lazytype.com")).pack(
               padx=20, pady=(4, 0), anchor="w")

    # ── Afsluiten ─────────────────────────────────────────────────────────
    def on_close():
        global _keypicking
        _keypicking = False
        pressed.clear()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    for _tid, _cv in tab_canvas.items():   # muiswiel-scroll op alle tab-inhoud
        _bind_wheel(tab_frames[_tid], _cv)
    show_tab(start_tab)
    root.mainloop()


def _install_update(new_version: str, parent_window=None):
    """Download de nieuwe exe en vervang de huidige na afsluiten (Windows, frozen).

    Robuust tegen de PyInstaller one-file valkuilen:
    • De updater-cmd draait DETACHED met cwd buiten _MEI en close_fds, zodat hij de
      _MEI-map niet vasthoudt (anders faalt de cleanup → blokkerende waarschuwing).
    • Een retry-copy-loop wacht tot de oude exe het bestand vrijgeeft (i.p.v. een
      vaste 2s-wacht die vaak te kort is).
    • os._exit(0) geeft de exe-lock ONMIDDELLIJK vrij en slaat de bootloader-cleanup
      over die anders de modale 'Failed to remove temporary directory'-waarschuwing
      toont en het afsluiten blokkeerde (oude sys.exit liep bovendien op een
      daemon-thread → beëindigde het proces niet).
    • De bat ruimt achtergebleven _MEI-mappen op vóór de herstart.
    """
    import urllib.request, tempfile, subprocess, hashlib
    try:
        tmp = Path(tempfile.gettempdir()) / "Lazytype_update.exe"
        urllib.request.urlretrieve("https://lazytype.com/downloads/Lazytype.exe", tmp)
        if not tmp.exists() or tmp.stat().st_size < 1_000_000:
            raise RuntimeError("Download onvolledig of mislukt — probeer het opnieuw.")
        # Verifieer SHA256 ALS de hash-lijst beschikbaar is. Ontbreekt sha256.txt
        # (404) of is hij onbereikbaar, dan vertrouwen we op de HTTPS-verbinding en
        # slaan we de check over — anders blokkeert een ontbrekend hash-bestand ELKE
        # update (precies de "404 Not Found" die gebruikers zagen). Bij een hash die
        # WEL aanwezig is maar NIET klopt, blokkeren we wél (manipulatie/beschadiging).
        expected_hash = ""
        try:
            with urllib.request.urlopen("https://lazytype.com/downloads/sha256.txt", timeout=10) as r:
                expected_hash = r.read().decode().strip().split()[0].lower()
        except Exception:
            expected_hash = ""
        if expected_hash:
            h = hashlib.sha256()
            with open(tmp, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            if h.hexdigest() != expected_hash:
                tmp.unlink(missing_ok=True)
                raise RuntimeError("Verificatie mislukt — download beschadigd of gemanipuleerd. Probeer opnieuw.")
        exe = Path(sys.executable)
        bat = Path(tempfile.gettempdir()) / "lazytype_update.bat"
        bat.write_text(
            "@echo off\r\n"
            f'set "SRC={tmp}"\r\n'
            f'set "DST={exe}"\r\n'
            "set /a n=0\r\n"
            ":retry\r\n"
            "set /a n+=1\r\n"
            "if %n% gtr 60 goto launch\r\n"
            "ping -n 2 127.0.0.1 >nul\r\n"          # ~1s wacht (timeout faalt in detached cmd)
            'copy /y "%SRC%" "%DST%" >nul 2>&1\r\n'
            "if errorlevel 1 goto retry\r\n"        # exe nog vergrendeld → opnieuw proberen
            ":launch\r\n"
            # Start via PowerShell ZONDER -UseNewEnvironment: de nieuwe exe erft de
            # omgeving van deze cmd, die al draait met _clean_env (Popen env=) — dus
            # de PyInstaller-onefile-vars (_PYI_*/_MEIPASS2) zijn weg (verse uitpak,
            # geen "Failed to load Python DLL"), MAAR USERPROFILE/APPDATA blijven
            # behouden. -UseNewEnvironment bouwde de omgeving uit het register en liet
            # USERPROFILE/APPDATA weg → Path.home() crashte met "Could not determine
            # home directory". $env:DST leest de batch-var DST incl. spaties in 't pad.
            'powershell -noprofile -windowstyle hidden -command "Start-Process -FilePath $env:DST"\r\n'
            'del "%SRC%" >nul 2>&1\r\n'
            'del "%~f0" >nul 2>&1\r\n',
            encoding="ascii")
        DETACHED_PROCESS          = 0x00000008
        CREATE_NEW_PROCESS_GROUP  = 0x00000200
        CREATE_NO_WINDOW          = 0x08000000
        # Geef de cmd een omgeving ZONDER de PyInstaller-onefile-vars, zodat de nieuwe
        # exe (via 'start') vers uitpakt i.p.v. de oude _MEI-map te zoeken.
        _clean_env = {k: v for k, v in os.environ.items()
                      if k not in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR",
                                   "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL")}
        subprocess.Popen(
            ["cmd", "/c", str(bat)],
            cwd=tempfile.gettempdir(),   # NIET _MEI → bootloader kan straks opruimen
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            close_fds=True,
            env=_clean_env,
        )
        try:
            if parent_window:
                parent_window.destroy()
        except Exception:
            pass
        try:
            if icon:
                icon.visible = False
                icon.stop()
        except Exception:
            pass
        try:
            overlay_ui.stop()
        except Exception:
            pass
        # NETTE afsluiting i.p.v. os._exit(0): de icon.stop() hierboven laat de
        # hoofd-thread main() normaal afronden, waarna de PyInstaller-bootloader zijn
        # EIGEN _MEI-map opruimt. Dat voorkomt de "Failed to load Python DLL"-race die
        # bij os._exit optrad (de bootloader-parent ruimde _MEI op terwijl het proces
        # nog niet netjes weg was). De copy-stap in de bat wacht op de exe-lock, dus die
        # gaat pas door zodra dit proces écht volledig is afgesloten.
        # Watchdog: mocht de nette afsluiting onverhoopt blijven hangen, forceer dan
        # alsnog exit zodat de update niet vastloopt.
        def _force_exit():
            import time as _t
            _t.sleep(6)
            os._exit(0)
        threading.Thread(target=_force_exit, daemon=True).start()
        return
    except Exception as e:
        import tkinter.messagebox as mb
        mb.showerror("Update mislukt", str(e))


def _run_settings_window(tab="instellingen"):
    """Opent het dashboard op de gevraagde tab."""
    open_dashboard(tab)


def _reload_from_env():
    """Herlaad instellingen uit .env na een externe wijziging (bijv. macOS settings-subprocess)."""
    env_path = dictate.ROOT / ".env"
    if not env_path.exists():
        return
    vals = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip()
    mapping = {
        "DICTATE_HOTKEY": "hotkey_name",
        "DICTATE_COMMAND_HOTKEY": "hotkey_command",
        "DICTATE_TRANSLATE_HOTKEY": "hotkey_translate",
        "DICTATE_TRANSLATE_TARGET": "translate_target",
        "DICTATE_LANGUAGE": "language",
        "DICTATE_ENGINE": "engine",
    }
    for env_key, state_key in mapping.items():
        if env_key in vals and vals[env_key]:
            state[state_key] = vals[env_key]
            os.environ[env_key] = vals[env_key]
    rebuild_hotkeys()
    if icon:
        state["last"] = "Instellingen opgeslagen ✓"; refresh()


def open_settings(icon_=None, item=None):
    if IS_MAC:
        def _mac():
            import subprocess
            proc = subprocess.Popen([sys.executable, "--settings"])
            proc.wait()
            _reload_from_env()
        threading.Thread(target=_mac, daemon=True).start()
        return
    threading.Thread(target=_run_settings_window, daemon=True).start()


def open_stats(icon_=None, item=None):
    """Opent het dashboard op de Statistieken-tab."""
    if IS_MAC:
        def _mac():
            import subprocess
            proc = subprocess.Popen([sys.executable, "--stats"])
            proc.wait()
            _reload_from_env()
        threading.Thread(target=_mac, daemon=True).start()
        return
    threading.Thread(target=lambda: _run_settings_window("statistieken"), daemon=True).start()


def run_onboarding():
    """First-run wizard (runs on main thread, before the tray loop)."""
    import tkinter as tk
    root = tk.Tk()
    root.title("Welcome to Lazytype")
    root.configure(bg=UI_PAPER)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    W, H = 500, 560
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")
    try: root.attributes("-alpha", 0.0)   # voor de fade-in
    except Exception: pass
    body = tk.Frame(root, bg=UI_PAPER)
    body.pack(fill="both", expand=True)
    sel = {"d": state["hotkey_name"], "t": state["hotkey_translate"],
           "g": state["translate_target"], "l": state["language"] or "en"}

    UI_LANG_CHOICES = [
        ("English", "en"), ("Nederlands", "nl"), ("Deutsch", "de"),
        ("Français", "fr"), ("Español", "es"), ("Italiano", "it"),
    ]
    # "Translate to" gebruikt de volledige talenlijst (TRANSLATE_TO_CHOICES, globaal).

    # ── Animatie-infra ────────────────────────────────────────────────────
    _anim_ids = []

    def _after(ms, fn):
        aid = root.after(ms, fn)
        _anim_ids.append(aid)
        return aid

    def clear():
        while _anim_ids:   # lopende animaties stoppen vóór we de stap wisselen
            try: root.after_cancel(_anim_ids.pop())
            except Exception: pass
        for w in body.winfo_children():
            w.destroy()

    def _fade_in(a=0.06):
        try: root.attributes("-alpha", min(1.0, round(a, 2)))
        except Exception: pass
        if a < 1.0:
            root.after(16, lambda: _fade_in(a + 0.08))
    _fade_in()

    def _waveform(parent, height=58, nbars=24):
        """Geanimeerde merk-waveform (paarse staafjes die pulseren)."""
        width = W - 48
        cv = tk.Canvas(parent, width=width, height=height, bg=UI_PAPER, highlightthickness=0)
        cv.pack(pady=(16, 2))
        st = {"t": 0}
        cy = height // 2
        def draw():
            if not cv.winfo_exists():
                return
            cv.delete("all")
            for i in range(nbars):
                x = 14 + i * ((width - 28) / (nbars - 1))
                env = 0.35 + 0.65 * abs(math.sin(i * 0.55))
                h = 3 + abs(math.sin(st["t"] * 0.16 + i * 0.45)) * (height * 0.42) * env
                cv.create_line(x, cy - h, x, cy + h, fill=UI_ACCENT, width=3, capstyle="round")
            st["t"] += 1
            _after(45, draw)
        draw()
        return cv

    def _typing(label, examples):
        """Typ-animatie: typt elk voorbeeld uit, houdt vast, en gaat naar het volgende."""
        st = {"ex": 0, "ch": 0, "phase": "type"}
        def tick():
            if not label.winfo_exists():
                return
            ex = examples[st["ex"]]
            if st["phase"] == "type":
                st["ch"] += 1
                label.config(text=ex[:st["ch"]] + "▏")
                if st["ch"] >= len(ex):
                    st["phase"] = "hold"; _after(1500, tick)
                else:
                    _after(42, tick)
            elif st["phase"] == "hold":
                label.config(text=ex)
                st["phase"] = "next"; _after(800, tick)
            else:
                st["ex"] = (st["ex"] + 1) % len(examples)
                st["ch"] = 0; st["phase"] = "type"
                _after(200, tick)
        tick()

    def logo_row(step_n, total=5):
        hdr = tk.Frame(body, bg=UI_PAPER)
        hdr.pack(fill="x", padx=24, pady=(18, 0))
        c = tk.Canvas(hdr, width=30, height=30, bg=UI_PAPER, highlightthickness=0)
        c.pack(side="left")
        c.create_rectangle(0, 0, 30, 30, fill=UI_INK, outline="")
        for corner in [(0,0,6,6),(24,0,30,6),(0,24,6,30),(24,24,30,30)]:
            c.create_rectangle(*corner, fill=UI_PAPER, outline="")
        c.create_rectangle(6,  12, 9,  20, fill="#f5f6fa", outline="")
        c.create_rectangle(11, 8,  14, 22, fill="#f5f6fa", outline="")
        c.create_rectangle(16, 14, 19, 19, fill="#f5f6fa", outline="")
        c.create_rectangle(21, 6,  23, 24, fill=UI_ACCENT, outline="")
        tk.Label(hdr, text="Lazytype", bg=UI_PAPER, fg=UI_INK,
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=(8, 0))
        tk.Label(hdr, text=f"Step {step_n} of {total}", bg=UI_PAPER, fg=UI_ACCENT,
                 font=("Segoe UI", 9, "bold")).pack(side="right")

    def head(t, sub):
        tk.Label(body, text=t, bg=UI_PAPER, fg=UI_INK, font=("Segoe UI", 15, "bold"),
                 wraplength=W - 48, justify="left", anchor="w").pack(fill="x", padx=24, pady=(12, 4))
        tk.Label(body, text=sub, bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 10),
                 wraplength=W - 48, justify="left", anchor="w").pack(fill="x", padx=24)

    def nav(back_cmd, primary_text, primary_cmd, skip_cmd=None):
        bar = tk.Frame(body, bg=UI_PAPER)
        bar.pack(side="bottom", fill="x", padx=22, pady=16)
        _accent_btn(tk, bar, primary_text, primary_cmd).pack(side="right")
        if skip_cmd:
            _ghost_btn(tk, bar, "Skip", skip_cmd).pack(side="right", padx=(0, 6))
        if back_cmd:
            _ghost_btn(tk, bar, "← Back", back_cmd).pack(side="left")

    # ── Step 1: Language ──────────────────────────────────────────────────
    def s_lang():
        clear()
        logo_row(1)
        head("Choose your language",
             "This sets both the app interface and the language you'll speak when dictating.")
        grid = tk.Frame(body, bg=UI_PAPER)
        grid.pack(fill="x", padx=24, pady=(14, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        btns = {}

        def select_lang(code):
            sel["l"] = code
            for c2, b in btns.items():
                b.config(bg=UI_ACCENT if c2 == code else "#ffffff",
                         fg="#ffffff" if c2 == code else UI_INK,
                         highlightbackground=UI_ACCENT if c2 == code else "#dedbd3")

        for i, (name, code) in enumerate(UI_LANG_CHOICES):
            b = tk.Button(grid, text=name, font=("Segoe UI", 10),
                          bg="#ffffff", fg=UI_INK, relief="flat", bd=0,
                          cursor="hand2", pady=8, highlightthickness=1,
                          highlightbackground="#dedbd3",
                          command=lambda c=code: select_lang(c))
            b.grid(row=i // 2, column=i % 2, padx=4, pady=4, sticky="ew")
            btns[code] = b

        cur = sel["l"] if sel["l"] in dict(UI_LANG_CHOICES).values() else "en"
        select_lang(cur)
        tk.Label(body, text="Speak 99+ languages — pick any of them later in Settings.",
                 bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9), anchor="w").pack(
                 fill="x", padx=24, pady=(12, 0))
        nav(None, "Continue →", s_welcome)

    # ── Step 2: Welcome (geanimeerd) ──────────────────────────────────────
    def s_welcome():
        clear()
        logo_row(2)
        _waveform(body)            # geanimeerde merk-waveform
        head("Welcome to Lazytype",
             "Hold a key, speak — your words appear in any app, cleaned up by AI.")

        # Live "typende" demo die de features laat zien
        demo = tk.Label(body, text="", bg="#f0efe9", fg=UI_INK, font=UI_MONO,
                        anchor="w", justify="left", wraplength=W - 64, padx=12, pady=10)
        demo.pack(fill="x", padx=24, pady=(10, 2))
        _typing(demo, [
            "Hold a key, speak — your words appear instantly.",
            "AI removes filler words and fixes punctuation automatically.",
            'Say "scratch that" to undo your last dictation.',
            "Translate to 99 languages while you speak.",
            'AI modes: select text, press a hotkey, let AI rewrite it.',
        ])

        feats = [
            ("♪", "Press & speak", "Hold the key, talk, release. Clean text in any app."),
            ("⌘", "AI modes", "Save an instruction under a hotkey (e.g. “make formal”). "
             "Select text, press the key — AI rewrites it for you. Set them up later in Settings → Modi."),
            ("⇄", "Translate · 99 languages", "Speak one language, get another — AI-polished."),
        ]
        for icon_ch, title, desc in feats:
            row = tk.Frame(body, bg=UI_PAPER)
            row.pack(fill="x", padx=24, pady=(8, 0))
            icon_f = tk.Frame(row, bg="#eeedfe", width=30, height=30)
            icon_f.pack(side="left", anchor="n")
            icon_f.pack_propagate(False)
            tk.Label(icon_f, text=icon_ch, bg="#eeedfe", fg=UI_ACCENT,
                     font=("Segoe UI", 12)).pack(expand=True)
            txt_f = tk.Frame(row, bg=UI_PAPER)
            txt_f.pack(side="left", fill="x", expand=True, padx=(10, 0))
            tk.Label(txt_f, text=title, bg=UI_PAPER, fg=UI_INK,
                     font=("Segoe UI", 10, "bold"), anchor="w", justify="left").pack(fill="x")
            tk.Label(txt_f, text=desc, bg=UI_PAPER, fg=UI_SUB,
                     font=("Segoe UI", 9), anchor="w", justify="left",
                     wraplength=W - 110).pack(fill="x")

        nav(s_lang, "Next →", s_dictkey)

    # ── Step 3: Dictation key ─────────────────────────────────────────────
    def s_dictkey():
        clear()
        logo_row(3)
        head("Choose your dictation key",
             "Hold this key while you speak, then release to stop. "
             "It won't interfere with normal keyboard use.")
        g = _keypicker(tk, body, "Dictation key", sel["d"])
        tk.Label(body, text="Popular: Right Ctrl · Right Alt · F8 · F9 · Ctrl + Alt",
                 bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9),
                 anchor="w").pack(fill="x", padx=24, pady=(4, 0))
        tip = tk.Frame(body, bg="#eeedfe")
        tip.pack(fill="x", padx=24, pady=(14, 0))
        tk.Label(tip, text="✨ Your speech is cleaned up by AI automatically (filler words, "
                           "punctuation). Say \"scratch that\" to undo your last dictation. "
                           "Tone even adapts to the app you're in.",
                 bg="#eeedfe", fg=UI_INK, font=("Segoe UI", 9), justify="left",
                 anchor="w", wraplength=W - 72, padx=12, pady=10).pack(fill="x")
        nav(s_welcome, "Next →", lambda: (sel.update(d=g()), s_transkey()))

    # ── Step 4: Translation key + target language ─────────────────────────
    def s_transkey():
        clear()
        logo_row(4)
        head("Translation key",
             "Hold this key to dictate and translate at the same time. "
             "You can skip this and set it up later in Settings.")
        lang_name = LANG_DISPLAY.get(sel["l"], sel["l"])
        info = tk.Frame(body, bg="#f0efe9")
        info.pack(fill="x", padx=24, pady=(10, 0))
        tk.Label(info, text=f"Speak: {lang_name}  ·  set in step 1",
                 bg="#f0efe9", fg=UI_SUB, font=("Segoe UI", 9),
                 anchor="w", padx=10, pady=5).pack(fill="x")
        g2 = _dropdown(tk, body, "Translate to", TRANSLATE_TO_CHOICES, sel["g"])
        g1 = _keypicker(tk, body, "Translation key", sel["t"])
        tk.Label(body, text="Popular: Right Alt · F9 · Ctrl + Alt",
                 bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9),
                 anchor="w").pack(fill="x", padx=24, pady=(4, 0))
        save_and_next = lambda: (sel.update(t=g1(), g=g2()), s_email())
        nav(s_dictkey, "Next →", save_and_next, skip_cmd=save_and_next)

    # ── Step 5: Email + summary + trial ──────────────────────────────────
    def s_email():
        clear()
        logo_row(5)
        try:
            import socket
            s = socket.create_connection(("lazytype.com", 443), timeout=5)
            s.close()
        except Exception:
            head("No internet connection",
                 "An internet connection is required to start the free trial. "
                 "Check your connection and restart Lazytype.")
            bar = tk.Frame(body, bg=UI_PAPER)
            bar.pack(side="bottom", fill="x", padx=22, pady=20)
            _ghost_btn(tk, bar, "← Back", s_transkey).pack(side="left")
            _accent_btn(tk, bar, "Quit", root.destroy).pack(side="right")
            return

        head("You're all set — let's go",
             "Start your 14-day free trial. No credit card needed.")

        lang_name = LANG_DISPLAY.get(sel["l"], sel["l"])
        get_name  = LANG_DISPLAY.get(sel["g"], sel["g"])
        hk_name   = _hk_display(sel["d"])
        tr_name   = _hk_display(sel["t"])

        sum_f = tk.Frame(body, bg="#f0efe9")
        sum_f.pack(fill="x", padx=24, pady=(10, 0))
        tk.Label(sum_f,
                 text=f"Dictation key: {hk_name}  —  hold to speak in {lang_name}",
                 bg="#f0efe9", fg=UI_INK, font=("Segoe UI", 9),
                 anchor="w", padx=10, pady=4).pack(fill="x")
        tk.Frame(sum_f, bg="#dedbd3", height=1).pack(fill="x", padx=10)
        tk.Label(sum_f,
                 text=f"Translation key: {tr_name}  —  {lang_name} → {get_name}, AI-corrected",
                 bg="#f0efe9", fg=UI_INK, font=("Segoe UI", 9),
                 anchor="w", padx=10, pady=4).pack(fill="x")
        tk.Frame(sum_f, bg="#dedbd3", height=1).pack(fill="x", padx=10)
        tk.Label(sum_f,
                 text="More in Settings: AI modes (hotkeys), realtime preview, command mode.",
                 bg="#f0efe9", fg=UI_SUB, font=("Segoe UI", 9),
                 anchor="w", padx=10, pady=4).pack(fill="x")

        tk.Frame(body, bg="#dedbd3", height=1).pack(fill="x", padx=24, pady=(12, 6))

        ef = tk.Frame(body, bg=UI_PAPER)
        ef.pack(fill="x", padx=24)
        tk.Label(ef, text="Email address", bg=UI_PAPER, fg=UI_SUB,
                 font=UI_FONT, anchor="w").pack(fill="x")
        email_var = tk.StringVar()
        email_entry = tk.Entry(ef, textvariable=email_var, font=UI_FONT,
                               relief="flat", bg="#ffffff", fg=UI_INK,
                               insertbackground=UI_INK, highlightthickness=1,
                               highlightbackground="#dedbd3", highlightcolor=UI_ACCENT)
        email_entry.pack(fill="x", ipady=6, pady=(4, 0))
        email_entry.focus_set()

        # Codeveld — verschijnt pas nadat er een verificatiecode is verstuurd.
        code_frame = tk.Frame(body, bg=UI_PAPER)
        tk.Label(code_frame, text="6-digit code from your email",
                 bg=UI_PAPER, fg=UI_SUB, font=UI_FONT, anchor="w").pack(fill="x", padx=24)
        code_var = tk.StringVar()
        code_entry = tk.Entry(code_frame, textvariable=code_var, font=("Consolas", 15),
                              relief="flat", bg="#ffffff", fg=UI_INK, justify="center",
                              insertbackground=UI_INK, highlightthickness=1,
                              highlightbackground="#dedbd3", highlightcolor=UI_ACCENT)
        code_entry.pack(fill="x", padx=24, ipady=6, pady=(4, 0))
        _ghost_btn(tk, code_frame, "Resend code",
                   lambda: resend_code()).pack(anchor="w", padx=24, pady=(2, 0))

        status_var = tk.StringVar()
        tk.Label(body, textvariable=status_var, bg=UI_PAPER, fg=UI_SUB,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=24, pady=(4, 0))

        alt = tk.Frame(body, bg=UI_PAPER)
        alt.pack(fill="x", padx=24, pady=(4, 0))
        tk.Label(alt, text="Already have a license or Pro key?",
                 bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9)).pack(side="left")
        _ghost_btn(tk, alt, "Enter it here…",
                   lambda: set_license_action(None, None)).pack(side="left", padx=(6, 0))

        def finish():
            global _keypicking
            _keypicking = False
            pressed.clear()
            state["hotkey_name"]      = sel["d"]
            state["hotkey_translate"] = sel["t"]
            state["translate_target"] = sel["g"]
            state["language"]         = sel["l"]
            for k, envk in (("hotkey_name",     "DICTATE_HOTKEY"),
                            ("hotkey_translate", "DICTATE_TRANSLATE_HOTKEY"),
                            ("translate_target", "DICTATE_TRANSLATE_TARGET"),
                            ("language",         "DICTATE_LANGUAGE")):
                dictate.save_env_value(envk, state[k])
            # Als er een (trial/pro/lifetime) licentie is, gebruik de managed-engine.
            # Anders blijft 'state' op de stale default "groq" zonder key staan → geen transcriptie.
            if os.environ.get("LAZYTYPE_LICENSE"):
                import license as _lic
                _p = _lic.decode(os.environ["LAZYTYPE_LICENSE"])
                if _p and _lic.TIERS.get(_p.get("tier"), {}).get("managed"):
                    state["engine"] = "managed"
                    dictate.save_env_value("DICTATE_ENGINE", "managed")
            dictate.save_env_value("DICTATE_ONBOARDED", "1")
            rebuild_hotkeys()
            root.destroy()

        def send_code():
            email = email_var.get().strip()
            if not email or "@" not in email:
                status_var.set("Please enter a valid email address.")
                return
            status_var.set("Sending a verification code…")
            root.update()
            try:
                _request_trial_code(email)
                email_entry.config(state="disabled")
                code_frame.pack(fill="x", pady=(8, 0), after=ef)
                code_entry.focus_set()
                primary["btn"].config(text="Verify & start →", command=verify_code)
                status_var.set("Code sent — check your inbox (and spam), then enter it above.")
            except Exception as e:
                status_var.set(str(e))

        def resend_code():
            try:
                _request_trial_code(email_var.get().strip())
                status_var.set("New code sent — check your email (and spam).")
            except Exception as e:
                status_var.set(str(e))

        def verify_code():
            code = code_var.get().strip()
            if not code:
                status_var.set("Enter the 6-digit code from your email.")
                return
            status_var.set("Verifying…")
            root.update()
            try:
                key = _verify_trial_code(email_var.get().strip(), code)
                dictate.save_env_value("LAZYTYPE_LICENSE", key)
                os.environ["LAZYTYPE_LICENSE"] = key
                state["engine"] = "managed"
                dictate.save_env_value("DICTATE_ENGINE", "managed")
                status_var.set("Done! Your 14-day trial has started.")
                root.after(1000, finish)
            except Exception as e:
                status_var.set(str(e))

        bar = tk.Frame(body, bg=UI_PAPER)
        bar.pack(side="bottom", fill="x", padx=22, pady=14)
        primary = {"btn": _accent_btn(tk, bar, "Send code →", send_code)}
        primary["btn"].pack(side="right")
        _ghost_btn(tk, bar, "← Back", s_transkey).pack(side="left")
        _ghost_btn(tk, bar, "Skip for now", finish).pack(side="left", padx=(12, 0))

    def skip():
        global _keypicking
        _keypicking = False
        pressed.clear()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", skip)
    s_lang()
    root.mainloop()


def build_menu():
    engines = [
        ("Managed — abonnement (geen eigen key)", "managed"),
        ("Groq (eigen key)", "groq"),
        ("OpenAI", "openai"),
        ("Lokaal (offline)", "local"),
    ]
    langs = [
        ("Nederlands", "nl"), ("Engels", "en"), ("Duits", "de"),
        ("Frans", "fr"), ("Automatisch", "auto"),
    ]
    engine_menu = pystray.Menu(*[
        pystray.MenuItem(label, choose_engine(code), radio=True,
                         checked=(lambda c: (lambda item: state["engine"] == c))(code))
        for label, code in engines
    ])
    lang_menu = pystray.Menu(*[
        pystray.MenuItem(label, choose_language(code), radio=True,
                         checked=(lambda c: (lambda item: state["language"] == c))(code))
        for label, code in langs
    ])
    postproc = [
        ("Uit", "off"),
        ("Opschonen (zelfde taal)", "clean"),
        ("Vertaal → Engels", "en"),
        ("Vertaal → Nederlands", "nl"),
        ("Vertaal → Duits", "de"),
        ("Vertaal → Frans", "fr"),
        ("Vertaal → Spaans", "es"),
    ]
    postproc_menu = pystray.Menu(*[
        pystray.MenuItem(label, choose_postprocess(code), radio=True,
                         checked=(lambda c: (lambda item: state["postprocess"] == c))(code))
        for label, code in postproc
    ])
    admin_menu = pystray.Menu(
        pystray.MenuItem("Nieuwe sleutel…", admin_gen_action),
        pystray.MenuItem("Sleutels tonen", _admin_show("list_text", "Admin — uitgegeven sleutels")),
        pystray.MenuItem("Sleutel verifiëren…", admin_verify_action),
        pystray.MenuItem("Sleutel intrekken…", admin_revoke_action),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Prijzen & tiers", _admin_show("tiers_text", "Lazytype — prijzen & tiers")),
        pystray.MenuItem("Server-config", _admin_show("server_text", "Admin — server-config")),
    )
    hist = dictate.load_history()
    hist_items = ([pystray.MenuItem(_hist_label(h.get("text", "")), history_copy(h.get("text", "")))
                   for h in hist[:12]]
                  or [pystray.MenuItem("— leeg —", None, enabled=False)])
    hist_menu = pystray.Menu(
        *hist_items,
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Bewaren", toggle_history, checked=lambda item: state["history"]),
        pystray.MenuItem("Wissen", clear_history_action),
    )
    update_items = (
        pystray.MenuItem(
            f"↑ Update beschikbaar: v{_update_info} — lazytype.com",
            lambda icon_, item_: __import__("webbrowser").open("https://lazytype.com"),
        ),
        pystray.Menu.SEPARATOR,
    ) if _update_info else ()
    return pystray.Menu(
        pystray.MenuItem(lambda item: f"● {PHASE_LABEL[current_phase()]}", None, enabled=False),
        pystray.MenuItem(lambda item: short_last(), None, enabled=False),
        pystray.MenuItem(lambda item: license_status(), None, enabled=False),
        *update_items,
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Engine", engine_menu),
        pystray.MenuItem("Taal (spreektaal)", lang_menu),
        pystray.MenuItem("Nabewerking (AI)", postproc_menu),
        pystray.MenuItem("Woordenboek bewerken…", _edit_file_action(
            dictate.DICTIONARY_FILE, "Lazytype — Woordenboek",
            "Eén term per regel (namen/jargon). Verbetert de herkenning.",
            "# Eén term per regel: namen, jargon, afkortingen.\n")),
        pystray.MenuItem("Snippets bewerken…", _edit_file_action(
            dictate.SNIPPETS_FILE, "Lazytype — Snippets",
            "Per regel:  trigger = tekst   (gebruik \\n voor een nieuwe regel).",
            "# trigger = uit te vouwen tekst\n# bijv:  mijn agenda = https://calendly.com/bas\n")),
        pystray.MenuItem("Geschiedenis (klik = kopieer)", hist_menu),
        pystray.MenuItem("Dicteren actief", toggle_enabled,
                         checked=lambda item: state["enabled"]),
        pystray.MenuItem("Overlay-balkje tonen", toggle_overlay,
                         checked=lambda item: state["overlay"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Statistieken…", open_stats),
        pystray.MenuItem("Instellingen…", open_settings),
        pystray.MenuItem("Abonnement-sleutel invoeren…", set_license_action),
        pystray.MenuItem("API-key instellen…", set_key_action),
        pystray.MenuItem("Automatisch starten", toggle_autostart,
                         checked=lambda item: is_autostart(),
                         visible=autostart_supported()),
        pystray.MenuItem("Admin", admin_menu, visible=lambda item: is_owner()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: f"Dicteren: {_hk_display(state['hotkey_name'])}", None, enabled=False),
        pystray.MenuItem(lambda item: f"Command: {_hk_display(state['hotkey_command'])}", None, enabled=False),
        pystray.MenuItem(lambda item: f"Vertaal→{state['translate_target']}: {_hk_display(state['hotkey_translate'])}",
                         None, enabled=False),
        pystray.MenuItem("Afsluiten", do_quit),
    )


# ── Klein drijvend overlay-pilletje ─────────────────────────────────────────
# Layout: [4 bars] | [nl] [→en alleen als actief] [⚙]
# Idle: bars vlak op 3px · Opnemen: bars animeren · Transcriberen: bars bevriezen (amber)
class Overlay:
    PILL     = "#0d0e13"
    PILL_REC = "#130d1a"
    PILL_BSY = "#110d18"
    BORDER   = "#1c1426"
    BAR_IDLE = "#4c1d95"   # dim paars bij rust
    BAR_REC  = "#c084fc"   # helder paars bij opnemen
    BAR_BSY  = "#7c3aed"   # middel paars bij transcriberen

    W = 88    # 6 bars
    H = 36

    def __init__(self):
        self.root = None; self.thread = None; self.cv = None
        self._t = 0; self._sm = 0.0
        self._bar_h = [1.5] * 6
        self._frozen = [4.0, 11.0, 7.0, 9.0, 5.0, 8.0]
        self._chips = []; self._drag = None; self._moved = False
        self._alpha = 0.25

    def start(self):
        # macOS: Tk/Cocoa MOET op de main-thread draaien; de overlay op een
        # achtergrond-thread starten geeft een native abort ("onverwacht gestopt").
        # Het tray-icoon claimt de main-thread al, dus geen overlay op Mac.
        if IS_MAC:
            return
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._safe, daemon=True)
        self.thread.start()

    def stop(self):
        r = self.root; self.root = None
        if r:
            try: r.after(0, r.destroy)
            except Exception: pass

    # ── Realtime preview-balk ────────────────────────────────────────────────
    def show_preview(self, text):
        """Toon (of werk bij) de drijvende preview-balk met de live transcriptie."""
        r = self.root
        if not r or not text:
            return
        import tkinter as tk

        def _do():
            try:
                if not getattr(self, "_pv", None):
                    self._pv = tk.Toplevel(r)
                    self._pv.overrideredirect(True)
                    self._pv.attributes("-topmost", True)
                    try: self._pv.attributes("-alpha", 0.96)
                    except Exception: pass
                    self._pv.configure(bg="#0d0e13")
                    self._pv_lbl = tk.Label(self._pv, text="", bg="#0d0e13", fg="#e8e6f5",
                                            font=("Segoe UI", 11), wraplength=520,
                                            justify="left", padx=14, pady=10)
                    self._pv_lbl.pack()
                self._pv_lbl.config(text=text)
                self._pv.update_idletasks()
                w = min(560, max(220, self._pv_lbl.winfo_reqwidth() + 28))
                h = self._pv_lbl.winfo_reqheight() + 18
                sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
                self._pv.geometry(f"{w}x{h}+{(sw - w) // 2}+{sh - h - 110}")
                self._pv.deiconify()
            except Exception:
                pass
        try: r.after(0, _do)
        except Exception: pass

    def hide_preview(self):
        r = self.root
        if not r or not getattr(self, "_pv", None):
            return
        def _do():
            try: self._pv.withdraw()
            except Exception: pass
        try: r.after(0, _do)
        except Exception: pass

    def _safe(self):
        try: self._run()
        except Exception as e: print(f"  (overlay uit: {e})")

    def _run(self):
        import tkinter as tk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        transp = "#000001"
        try:
            root.configure(bg=transp)
            root.attributes("-transparentcolor", transp)
        except Exception:
            transp = self.PILL; root.configure(bg=transp)
        try: root.attributes("-alpha", 0.25)
        except Exception: pass
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        saved_pos = os.environ.get("DICTATE_OVERLAY_POS", "")
        try:
            ox, oy = (int(v) for v in saved_pos.split(","))
            ox = max(0, min(ox, sw - self.W))
            oy = max(0, min(oy, sh - self.H))
        except Exception:
            ox, oy = (sw - self.W) // 2, sh - self.H - 60
        root.geometry(f"{self.W}x{self.H}+{ox}+{oy}")
        self.cv = tk.Canvas(root, width=self.W, height=self.H,
                            highlightthickness=0, bg=transp, cursor="hand2")
        self.cv.pack()
        self.cv.bind("<ButtonPress-1>",   self._press)
        self.cv.bind("<B1-Motion>",       self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.root = root
        self._tick()
        root.mainloop()

    def _tick(self):
        if not self.root: return
        try: self._draw()
        except Exception: pass
        self._t += 1
        try: self.root.after(40, self._tick)
        except Exception: pass

    # ── tekenhelpers ────────────────────────────────────────────────────────

    def _pill(self, x0, y0, x1, y1, r, fill):
        self.cv.create_polygon(
            [x0+r, y0, x1-r, y0, x1, y0, x1, y0+r,
             x1, y1-r, x1, y1, x1-r, y1, x0+r, y1,
             x0, y1, x0, y1-r, x0, y0+r, x0, y0],
            smooth=True, fill=fill, outline=fill)

    def _chip_draw(self, x0, x1, cy, text, fg, bg, cb):
        r = 5
        self._pill(x0, cy-8, x1, cy+8, r, bg)
        self.cv.create_text((x0+x1)//2, cy, text=text, fill=fg,
                            font=("Segoe UI", 8, "bold"), anchor="c")
        self._chips.append((x0, x1, cy-9, cy+9, cb))

    # ── hoofd-renderfunctie ──────────────────────────────────────────────────

    def _draw(self):
        cv = self.cv; cv.delete("all")
        self._chips = []
        ph = "recording" if state["phase"] == "arming" else current_phase()
        W, H = self.W, self.H
        cy = H // 2

        # Audio-niveau smoothen
        self._sm += (recorder.last_level - self._sm) * 0.35
        lv = self._sm

        # Bar-hoogtes — elke bar eigen tempo + fase voor organisch gevoel
        _freqs  = [0.43, 0.55, 0.38, 0.62, 0.48, 0.52]
        _phases = [0.0,  0.9,  1.8,  2.7,  3.6,  4.5]
        if ph == "recording":
            for i in range(6):
                osc    = 0.25 + 0.75 * abs(math.sin(self._t * _freqs[i] + _phases[i]))
                target = 2.0 + lv * 28 * osc + abs(math.sin(self._t * _freqs[i] * 0.6 + i)) * 5
                target = min(target, H - 4)
                speed  = 0.55 if target > self._bar_h[i] else 0.22
                self._bar_h[i] += (target - self._bar_h[i]) * speed
                self._frozen[i] = self._bar_h[i]
        elif ph != "working":
            for i in range(6):
                idle = 1.5 + 0.6 * abs(math.sin(self._t * 0.04 + _phases[i] * 0.4))
                self._bar_h[i] += (idle - self._bar_h[i]) * 0.06

        # Venster-alpha: 25% rust → 100% actief
        target_alpha = 1.0 if ph in ("recording", "working") else 0.25
        self._alpha += (target_alpha - self._alpha) * 0.15
        try: self.root.attributes("-alpha", round(self._alpha, 3))
        except Exception: pass

        # Pill
        bg = self.PILL_REC if ph == "recording" else self.PILL_BSY if ph == "working" else self.PILL
        self._pill(2, 2, W-2, H-2, 14, self.BORDER)
        self._pill(3, 3, W-3, H-3, 13, bg)

        # 6 paarse verticale bars
        bar_col = self.BAR_REC if ph == "recording" else self.BAR_BSY if ph == "working" else self.BAR_IDLE
        CENTERS = [12.0, 24.0, 36.0, 52.0, 64.0, 76.0]
        heights = self._frozen if ph == "working" else self._bar_h
        for bx, bh in zip(CENTERS, heights):
            cv.create_line(bx, cy - bh/2, bx, cy + bh/2,
                           fill=bar_col, width=5, capstyle="round")

    # ── acties ──────────────────────────────────────────────────────────────

    def _cycle_lang(self):
        order = ["nl", "en", "de", "fr", "es", "auto"]
        i = order.index(state["language"]) if state["language"] in order else -1
        state["language"] = order[(i + 1) % len(order)]
        dictate.save_env_value("DICTATE_LANGUAGE", state["language"])

    def _cycle_ai(self):
        order = ["off", "clean", "en", "nl", "de", "fr"]
        i = order.index(state["postprocess"]) if state["postprocess"] in order else -1
        state["postprocess"] = order[(i + 1) % len(order)]
        dictate.save_env_value("DICTATE_POSTPROCESS", state["postprocess"])

    def _open_settings(self):
        threading.Thread(target=_run_settings_window, daemon=True).start()

    # ── drag ────────────────────────────────────────────────────────────────

    def _press(self, e):
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        self._moved = False

    def _motion(self, e):
        if not self._drag: return
        dx, dy = e.x_root - self._drag[0], e.y_root - self._drag[1]
        if abs(dx) + abs(dy) > 4: self._moved = True
        self.root.geometry(f"+{self._drag[2]+dx}+{self._drag[3]+dy}")

    def _release(self, e):
        moved, self._drag = self._moved, None
        if moved:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            threading.Thread(
                target=lambda: dictate.save_env_value("DICTATE_OVERLAY_POS", f"{x},{y}"),
                daemon=True).start()
            return
        for x0, x1, y0, y1, cb in self._chips:
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                cb(); return


overlay_ui = Overlay()


def toggle_overlay(icon_, item):
    state["overlay"] = not state["overlay"]
    dictate.save_env_value("DICTATE_OVERLAY", "1" if state["overlay"] else "0")
    overlay_ui.start() if state["overlay"] else overlay_ui.stop()
    refresh()


# ── macOS-permissies (Invoerbewaking / Toegankelijkheid) ────────────────────
def _mac_open_pref(pane: str):
    """Open het juiste paneel in Systeeminstellingen → Privacy en beveiliging."""
    try:
        subprocess.run(["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
                       check=False)
    except Exception:
        pass


def _mac_permission_status():
    """(toegankelijkheid_ok, invoerbewaking_ok) op macOS; (True, True) elders.
    Checkt zónder een prompt te forceren. Bij twijfel: True (niet blokkeren)."""
    if not IS_MAC:
        return True, True
    import ctypes
    acc = inp = True
    try:
        a = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        a.AXIsProcessTrusted.restype = ctypes.c_bool
        acc = bool(a.AXIsProcessTrusted())
    except Exception:
        acc = True
    try:
        k = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        k.IOHIDCheckAccess.restype = ctypes.c_int
        k.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        inp = (k.IOHIDCheckAccess(1) == 0)   # 1 = ListenEvent, return 0 = granted
    except Exception:
        inp = True
    return acc, inp


def _show_mac_permissions_window(acc_ok: bool, inp_ok: bool):
    """Eenmalig uitleg-venster (main-thread, vóór de tray) dat de gebruiker naar de
    juiste Systeeminstellingen stuurt. Toont alleen wat nog ontbreekt."""
    import tkinter as tk
    root = tk.Tk()
    root.title("Lazytype — toestemming nodig")
    root.configure(bg=UI_PAPER)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    W, H = 470, 380
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")
    tk.Label(root, text="Toestemming nodig om te kunnen dicteren", bg=UI_PAPER, fg=UI_INK,
             font=("Segoe UI", 15, "bold"), wraplength=W - 48, justify="left").pack(
             fill="x", padx=24, pady=(20, 4))
    tk.Label(root, text="Zet Lazytype aan in Systeeminstellingen → Privacy en beveiliging. "
                        "Sluit Lazytype daarna af en open 'm opnieuw.",
             bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 10), wraplength=W - 48,
             justify="left").pack(fill="x", padx=24, pady=(0, 6))

    def row(label, ok, pane, hint):
        f = tk.Frame(root, bg=UI_PAPER); f.pack(fill="x", padx=24, pady=(10, 0))
        if ok is None:
            mark, col = "•", UI_SUB
        else:
            mark, col = ("✓", "#1f9d57") if ok else ("✗", "#cc3333")
        tk.Label(f, text=mark, bg=UI_PAPER, fg=col, font=("Segoe UI", 13, "bold"),
                 width=2).pack(side="left", anchor="n")
        tf = tk.Frame(f, bg=UI_PAPER); tf.pack(side="left", fill="x", expand=True)
        tk.Label(tf, text=label, bg=UI_PAPER, fg=UI_INK, font=("Segoe UI", 10, "bold"),
                 anchor="w").pack(fill="x")
        tk.Label(tf, text=hint, bg=UI_PAPER, fg=UI_SUB, font=("Segoe UI", 9), anchor="w",
                 justify="left", wraplength=W - 150).pack(fill="x")
        if ok is False:
            _ghost_btn(tk, f, "Openen", lambda p=pane: _mac_open_pref(p)).pack(side="right")

    row("Invoerbewaking", inp_ok, "Privacy_ListenEvent",
        "Nodig om je dicteer-toets te kunnen horen.")
    row("Toegankelijkheid", acc_ok, "Privacy_Accessibility",
        "Nodig om de herkende tekst in je app te plakken.")
    row("Microfoon", None, "Privacy_Microphone",
        "Sta toe wanneer macOS het vraagt bij je eerste dictaat.")
    bar = tk.Frame(root, bg=UI_PAPER); bar.pack(side="bottom", fill="x", padx=22, pady=18)
    _accent_btn(tk, bar, "Doorgaan", root.destroy).pack(side="right")
    try:
        root.lift(); root.focus_force()
    except Exception:
        pass
    root.mainloop()


# ── Single-instance: één exemplaar tegelijk (Windows) ───────────────────────
_SINGLE_MUTEX = None  # handle vasthouden zolang de app draait


def _signal_open_settings():
    try:
        (dictate.ROOT / ".open_settings").write_text("1", encoding="utf-8")
    except Exception:
        pass


def _acquire_single_instance() -> bool:
    """True = wij zijn de primaire/enige instantie. False = er draait er al een
    (die is dan gesignaleerd om het instellingen-venster te tonen). macOS regelt
    single-instance zelf via LaunchServices → daar altijd True."""
    global _SINGLE_MUTEX
    if not IS_WIN:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        _SINGLE_MUTEX = k32.CreateMutexW(None, False, "Lazytype_singleton_v1")
        if k32.GetLastError() == 183:        # ERROR_ALREADY_EXISTS
            _signal_open_settings()
            return False
    except Exception:
        return True                          # bij twijfel: gewoon starten
    return True


def _watch_open_settings():
    """Primaire instantie: open de instellingen als een tweede start ons signaleert."""
    sf = dictate.ROOT / ".open_settings"
    try:
        sf.unlink(missing_ok=True)           # achtergebleven signaal negeren
    except Exception:
        pass
    while True:
        time.sleep(1.0)
        try:
            if sf.exists():
                sf.unlink(missing_ok=True)
                open_settings(None, None)
        except Exception:
            pass


def main():
    global icon

    if "--make-icons" in sys.argv:
        here = Path(__file__).resolve().parent
        accent = ACCENT["idle"]
        # app-icoon (squircle) als .ico in meerdere maten (voor de exe)
        app = render_icon(accent, size=256, shape="squircle")
        app.save(here / "icon.ico",
                 sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        # macOS app-icoon (.icns) — voor de Mac-build
        try:
            render_icon(accent, size=512, shape="squircle").save(here / "icon.icns")
            print("icon.icns geschreven")
        except Exception as e:
            print(f"(icon.icns overgeslagen — maak op Mac met iconutil: {e})")
        # NB: site/favicon.png en site/icon-512.png bewust NIET overschreven
        # (die horen bij de website; we stemmen alleen de app erop af).
        # preview-montage van app-icoon + tray-statussen (warm-witte achtergrond zoals de site)
        cell = 96
        prev = Image.new("RGBA", (cell * 5 + 30, cell + 10), (247, 246, 243, 255))
        imgs = [("app", render_icon(accent, cell, "squircle"))] + \
               [(k, make_icon(k, cell)) for k in ("idle", "recording", "working", "disabled")]
        for i, (_, im) in enumerate(imgs):
            prev.paste(im, (i * (cell + 5) + 5, 5), im)
        prev.save(here / "icon-preview.png")
        print("app-icons gemaakt: icon.ico, icon.icns, icon-preview.png "
              "(site-favicons ongemoeid gelaten)")
        return

    if "--settings" in sys.argv:
        _run_settings_window()
        return

    if "--stats" in sys.argv:
        _run_settings_window("statistieken")
        return

    if "--uninstall" in sys.argv:
        _win_uninstall()
        return

    if "--dlg" in sys.argv:
        # macOS-dialoog in een apart proces (Tk op de main-thread van DIT proces).
        import base64, json
        i = sys.argv.index("--dlg")
        try:
            spec = json.loads(base64.b64decode(sys.argv[i + 1]).decode())
        except Exception:
            return
        kind = spec.get("kind")
        if kind == "input":
            v = _themed_input_tk(spec.get("title", ""), spec.get("prompt", ""),
                                 spec.get("initial", ""), spec.get("secret", False))
            if v is not None:
                print("RESULT:" + base64.b64encode(v.encode()).decode())
        elif kind == "text":
            _show_text_tk(spec.get("title", ""), spec.get("text", ""))
        elif kind == "edit":
            _edit_file_tk(spec.get("path", ""), spec.get("title", ""),
                          spec.get("label", ""), spec.get("default", ""))
        return

    if "--selftest" in sys.argv:
        # bouw alles op zonder de blokkerende tray-loop; schrijf een preview
        menu = build_menu()

        # render élk menu-item (tekst + checked) net zoals pystray dat doet,
        # zodat callable-fouten hier opduiken i.p.v. pas in het systeemvak
        def render(items, depth=0):
            for it in items:
                label = it.text  # roept de tekst-callable aan met het item
                mark = ""
                try:
                    if it.checked is not None:
                        mark = " [x]" if it.checked else " [ ]"
                except Exception:
                    pass
                print("   " * depth + f"- {label}{mark}")
                if it.submenu:
                    render(it.submenu, depth + 1)

        # roep ook de fasewissels aan, zodat dynamische labels in elke staat kloppen
        for ph in ("idle", "recording", "working"):
            state["phase"] = ph
            render(menu.items)

        montage = Image.new("RGBA", (64 * 4 + 30, 64), (24, 27, 37, 255))
        for i, k in enumerate(["idle", "recording", "working", "disabled"]):
            montage.paste(ICONS[k], (i * (64 + 10) + 5, 0), ICONS[k])
        montage.save("tray-preview.png")
        state["phase"] = "idle"
        print("selftest OK — alle menu-teksten gerenderd; preview -> tray-preview.png")
        return

    # Eén exemplaar tegelijk: draait Lazytype al en open je 'm opnieuw (snelkoppeling/
    # autostart), dan opent het instellingen-venster van de draaiende app i.p.v. een
    # tweede, botsend exemplaar.
    if not _acquire_single_instance():
        return

    # Onboarding bij eerste start, of als er nog niets geconfigureerd is (gebroken .env).
    _nothing_configured = (
        not os.environ.get("DICTATE_ENGINE") and
        not os.environ.get("LAZYTYPE_LICENSE") and
        not os.environ.get("GROQ_API_KEY")
    )
    if not os.environ.get("DICTATE_ONBOARDED") or _nothing_configured:
        try:
            run_onboarding()
        except Exception as e:
            print(f"  (onboarding overgeslagen: {e})")

    # Groq-key vragen alleen als de gebruiker groq expliciet heeft gekozen (niet standaard bij eerste start).
    if state["engine"] == "groq" and not os.environ.get("GROQ_API_KEY") and os.environ.get("DICTATE_ENGINE"):
        try:
            key = ask_groq_key()
            if key:
                dictate.save_env_value("GROQ_API_KEY", key.strip())
        except Exception as e:
            print(f"  (key-dialoog overgeslagen: {e})")

    # macOS: zonder Invoerbewaking/Toegankelijkheid doet de hotkey/plakken stil
    # niets. Detecteer dat en wijs de gebruiker gericht de weg (main-thread, vóór
    # de tray — net als de onboarding).
    if IS_MAC:
        try:
            _acc_ok, _inp_ok = _mac_permission_status()
            if not (_acc_ok and _inp_ok):
                _show_mac_permissions_window(_acc_ok, _inp_ok)
        except Exception as e:
            print(f"  (permissie-check overgeslagen: {e})")

    # Autostart STANDAARD aan: bij login meteen actief. Eenmalig inschakelen tenzij
    # de gebruiker 'm bewust heeft uitgezet (DICTATE_AUTOSTART_OPTOUT=1).
    if autostart_supported() and os.environ.get("DICTATE_AUTOSTART_OPTOUT", "").lower() not in ("1", "true", "yes"):
        try:
            if not is_autostart():
                set_autostart(True)
                print("  Autostart: standaard ingeschakeld (uit te zetten via tray/Instellingen)")
        except Exception as e:
            print(f"  (autostart aanzetten overgeslagen: {e})")

    # Windows: eenmalig als 'echt' programma registreren (Start-menu + Geïnstalleerde apps).
    _win_install_integration()

    icon = pystray.Icon("lazytype", ICONS["idle"], "Lazytype", build_menu())
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    threading.Thread(target=_check_update, daemon=True).start()
    threading.Thread(target=dictate.verify_personal_key, daemon=True).start()
    threading.Thread(target=_cleanup_stale_mei, daemon=True).start()
    if IS_WIN:   # luister naar een 2e start → toon instellingen i.p.v. duplicaat
        threading.Thread(target=_watch_open_settings, daemon=True).start()

    print("=" * 58)
    print("  🎙️  Lazytype (systeemvak) is actief")
    print(f"  Engine     : {state['engine']}")
    print(f"  Taal       : {state['language']}")
    print(f"  Nabewerking: {state['postprocess']}")
    print(f"  {license_status()}")
    if is_owner():
        print("  Admin      : actief (geheim aanwezig) — Admin-menu zichtbaar")
    print(f"  Dicteren   : HOUD '{state['hotkey_name']}' in, spreek, laat los")
    print(f"  Command    : selecteer tekst, HOUD '{state['hotkey_command']}' in, spreek de instructie")
    print(f"  Vertalen   : HOUD '{state['hotkey_translate']}' in → vertaal naar {state['translate_target']}")
    print("  Bediening: rechtsklik op het icoon in het systeemvak")
    print("=" * 58)

    refresh()
    if state["overlay"]:
        overlay_ui.start()
    icon.run()
    listener.stop()
    overlay_ui.stop()
    if overlay_ui.thread:
        overlay_ui.thread.join(timeout=1.5)   # nette Tk-afbouw bij afsluiten


if __name__ == "__main__":
    main()
