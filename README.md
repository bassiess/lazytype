# Lazytype

Spraak-naar-tekst, twee smaken:

1. **Dicteren (hoofdfunctie)** — houd een sneltoets ingedrukt, spreek, laat los; de tekst
   verschijnt direct in de app waar je cursor staat. Razendsnel via Groq.
2. **Webapp (demo)** — upload of live-opname in de browser; ondersteunend, niet de kern.

---

## 🎙️ Dicteren — snelstart

1. **Groq-key ophalen** (gratis): ga naar https://console.groq.com/keys, log in, maak een
   API-key aan en kopieer hem.
2. **Key invullen**: open `.env` en plak hem achter `GROQ_API_KEY=`.
3. **Eenmalig testen**:
   ```
   python dictate.py --check
   ```
   Spreek 3 seconden; je ziet de tekst + de tijd die het kostte.
4. **Gebruiken**: dubbelklik `Lazytype.bat`. Er verschijnt een **icoon in het systeemvak**.
   Houd **rechter Ctrl** ingedrukt, spreek, laat los → de tekst wordt in je actieve venster geplakt.

### Systeemvak-icoon

`dictate_tray.py` (start via `Lazytype.bat`) toont een microfoon-icoon dat van kleur verandert:
**blauw** = klaar · **rood** = opname · **oranje** = transcriberen · **grijs** = gepauzeerd.
Rechtsklik op het icoon voor het menu:
- **Engine** (Groq/OpenAI/Lokaal) en **Taal** wisselen
- **Dicteren actief** — tijdelijk pauzeren
- **API-key instellen…** — opent een venster om je Groq-key te plakken (wordt in `.env` bewaard)
- **Start met Windows** — zet de tool in de autostart (via het register, geen admin nodig)
- De laatste transcriptie staat bovenin

Liever puur een console zonder icoon? Gebruik `python dictate.py`.

### Sneltoets & modifier-veiligheid

Standaard houd je **linker Ctrl** ingedrukt. Omdat dat ook een modifier is, gebruikt de tool
een "arming"-mechanisme: een snelle tik of een snelkoppeling zoals **Ctrl+C** start géén dictaat —
alleen schoon ~0,35s ingedrukt houden telt. Andere toets als sneltoets? Zet `DICTATE_HOTKEY` in `.env`.

### Slimme afronding

Naast de interpunctie die Whisper zelf al plaatst, kun je opmaak inspreken:
zeg **"nieuwe regel"** (of "enter") voor een regeleinde, of **"nieuwe alinea"** voor een witregel.

---

## 🌐 Online aanbieden (distributie)

De dicteertool is een desktop-app (een browser kan niet in andere programma's typen), dus
"online aanbieden" = een **downloadbare app + landingspagina**. Model: elke gebruiker brengt
zijn **eigen gratis Groq-key** (kost de aanbieder niets; bij eerste start vraagt de app erom).

- **Landingspagina**: `site/index.html` — hero, uitleg, download-knop.
- **Standalone .exe bouwen** (geen Python nodig voor eindgebruiker):
  ```
  powershell -ExecutionPolicy Bypass -File build_exe.ps1
  ```
  Resultaat: `dist/Lazytype.exe` (~33 MB), ook gekopieerd naar `site/downloads/`.
- **Publiceren**: host de inhoud van `site/` op een statische host (Netlify, Vercel, GitHub Pages,
  of je eigen webruimte). De download-knop wijst naar `downloads/Lazytype.exe`.

> De gebouwde exe zoekt `.env` naast zichzelf; bij eerste start vult de gebruiker de key in via
> het dialoogvenster, dat de `.env` daar aanmaakt.

---

## 🍎 macOS

De code is cross-platform. Verschillen worden automatisch afgehandeld:
plakken met **Cmd+V**, geluid via **afplay** (systeemgeluiden), en autostart via een
**LaunchAgent** in `~/Library/LaunchAgents/`.

**Bouwen (op een Mac):**
```
python3 -m pip install -r requirements.txt
./build_app.sh
```
Resultaat: `dist/Lazytype.app` — sleep naar `Programma's`.

**Permissies (eenmalig)** — Systeeminstellingen → Privacy & Beveiliging:
- **Toegankelijkheid** — voor de globale sneltoets én het plakken
- **Invoermonitoring** — om de toets buiten de app te kunnen lezen
- **Microfoon** — wordt bij de eerste opname gevraagd

**Snel testen zonder bouwen** (met Python op de Mac):
```
python3 dictate_tray.py        # tray-versie
python3 dictate.py --check     # neem 3s op en transcribeer
```

> Let op: een .app builden en macOS-permissies geven kan alleen óp een Mac.
> Tk (voor het key-venster) zit in de python.org-installer; bij Homebrew: `brew install python-tk`.

### Instellingen (`.env`)

| Variabele | Wat | Standaard |
|---|---|---|
| `GROQ_API_KEY` | Je Groq API-key | *(leeg — vereist)* |
| `DICTATE_ENGINE` | `groq` · `openai` · `local` | `groq` |
| `DICTATE_HOTKEY` | Toets die je ingedrukt houdt (`ctrl_r`, `alt_r`, `f9`, `pause`…) | `ctrl_r` |
| `DICTATE_LANGUAGE` | `nl` · `en` · … · `auto` | `nl` |
| `DICTATE_TRAILING_SPACE` | Spatie achter elk dictaat | `true` |
| `DICTATE_RESTORE_CLIPBOARD` | Klembord herstellen na plakken | `true` |

### Waarom Groq?

`whisper-large-v3-turbo` op Groq draait ~216× realtime — een dictaat van een paar seconden
komt in **< 1 seconde** terug, in large-v3-kwaliteit (uitstekend Nederlands), voor ~$0,04/uur audio.
Werkt het klembord ooit niet (bv. een app blokkeert het), dan typt de tool de tekst zelf in.

### CLI

```
python dictate.py            # start de dicteer-daemon (hold-to-talk)
python dictate.py --check    # neem 3s op en transcribeer (verificatie)
python dictate.py --test x.wav   # transcribeer een bestaand bestand
python dictate.py --devices  # toon microfoons
```

---

## Webapp (demo) — starten

```
npm install
npm start
```

Open daarna http://localhost:3000 (uploaden + live-opname; ondersteunend).
De `local`-fallback van de dicteertool gebruikt de whisper-server die hierdoor meestart.

## Engines

| Engine | Wat | Vereist |
|---|---|---|
| **Lokaal** (standaard) | whisper.cpp op je eigen CPU | `tools/whisper/` + een model in `models/` (staat er al: ggml-base) |
| **OpenAI** | whisper-1 via de API | `OPENAI_API_KEY` in `.env` (kopieer `.env.example`) |

## Modellen & kwaliteit

Er liggen drie lokale modellen in `models/`. Benchmark op 35s echt Nederlands (luisterboek), CPU (Intel Iris Xe-laptop):

| Model | Verwerkingstijd | Nederlandse kwaliteit |
|---|---|---|
| ggml-base.bin (148 MB) | 17,5s | Slecht — veel verhaspelingen |
| ggml-small.bin (488 MB) | 43,1s | Redelijk |
| ggml-large-v3-turbo-q5_0.bin (574 MB) | 76,9s | Vrijwel foutloos |

- **Uploads** gebruiken standaard het beste model; per upload te wisselen via de dropdown "Kwaliteit".
- **Live** gebruikt standaard `small` (compromis snelheid/kwaliteit) en geeft de tekst van eerdere
  zinnen als context-prompt mee voor betere continuïteit. Te traag? Zet `WHISPER_MODEL_LIVE=ggml-base.bin` in `.env`.
- De échte kwaliteitssprong voor live op deze hardware: OpenAI API-key in `.env` (whisper-1),
  of later GPU-versnelling (OpenVINO/Vulkan op de Intel-GPU).

## Structuur

- `server.js` — Express-server, upload-endpoint `/api/transcribe`, status `/api/status`, WebSocket `/ws/live`
- `lib/localEngine.js` — whisper.cpp-aansturing voor uploads (spawn whisper-cli.exe, JSON-output)
- `lib/whisperServer.js` — beheert whisper-server.exe (model blijft in geheugen, voor live-modus)
- `lib/liveSession.js` — live-sessielogica: PCM-buffer, stiltedetectie, partial/committed-updates
- `lib/openaiEngine.js` — OpenAI Whisper API (transcriptie + vertaling, bestand én buffer)
- `lib/ffmpeg.js` — formaatconversie (alles → 16kHz WAV voor lokaal, compacte MP3 voor OpenAI)
- `lib/wav.js` — WAV-header om ruwe PCM-buffers
- `public/index.html` — frontend: tabs (upload/live), drag-and-drop, taalkeuze, export TXT/SRT
- `public/recorder-worklet.js` — AudioWorklet die microfoon-PCM doorgeeft
- `test-live.mjs` — pipelinetest: streamt test-audio.wav als nepmicrofoon over de WebSocket

## Routekaart (stap voor stap)

1. ✅ **Techniek-POC** — upload → transcript met timestamps, twee engines
2. ✅ **Live opname** — microfoon → WebSocket → near-realtime tekst (partial/committed)
3. ⬜ Vertaling naar elke taal + taaldetectie-UI
4. ⬜ Sprekerlabels (diarisatie)
5. ⬜ Transcript-editor, opslag, export DOCX/PDF
6. ⬜ Accounts, limieten, betalingen, deployment
