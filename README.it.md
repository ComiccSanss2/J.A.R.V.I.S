# J.A.R.V.I.S. OS

> **Due modalità di lingua**, sceglibili con i pulsanti **EN / IT** (nella schermata iniziale e in alto a destra):
> - **English** (predefinita): J.A.R.V.I.S. capisce solo l'inglese e risponde sempre in inglese, con voce britannica.
> - **Italiano**: interfaccia, riconoscimento vocale, voce e risposte tutto in italiano.
>
> La scelta vale per tutti i dispositivi collegati e resta salvata. Lingua iniziale: `JARVIS_LANGUAGE=en|it` nel `.env`.

Assistente personale vocale ispirato a J.A.R.V.I.S.: ascolto continuo, voce naturale, skills reali via Claude
Tool Use e **memoria condivisa in tempo reale tra Windows e macOS**.

## 1. Architettura

```
 ┌──────────── PC Windows ────────────┐          ┌──────────── MacBook ───────────────┐
 │ HUD (browser o app Electron)       │          │ HUD (browser o app Electron)       │
 │  • mic sempre attivo + VAD         │          │  • mic sempre attivo + VAD         │
 │  • STT (Web Speech / Whisper)      │          │  • STT (Web Speech / Whisper)      │
 │  • TTS + nucleo reattivo           │          │  • TTS + nucleo reattivo           │
 └───────────────┬────────────────────┘          └───────────────┬────────────────────┘
                 │  WebSocket /ws  (sessione "default")           │
                 └──────────────────────┬─────────────────────────┘
                          ┌─────────────▼──────────────┐
                          │  SERVER CENTRALE (FastAPI) │
                          │  Hub WS ─ broadcast eventi │
                          │  Brain ─ Claude + tool loop│──► API Anthropic (Claude, web search)
                          │  Skills ─ meteo, sistema…  │──► Open-Meteo, webhook, script
                          │  Voice ─ /api/tts /api/stt │──► ElevenLabs / OpenAI / edge-tts
                          │  SQLite ─ data/jarvis.db   │
                          └────────────────────────────┘
```

* Un solo **server centrale** (gira su uno dei due computer, o su un mini-server/VPS) possiede la verità:
  cronologia, memoria permanente (`facts`), dispositivi.
* Ogni client si collega via **WebSocket** indicando `session`, `device_id` e nome del dispositivo.
* Quando parli su Windows, il testo trascritto arriva al server → salvato in SQLite → **trasmesso a tutti i
  client della sessione** → Claude risponde usando l'intera cronologia condivisa → la risposta viene salvata e
  ritrasmessa a tutti, ma **solo il dispositivo che ha fatto la domanda la pronuncia**.
  Passando al Mac, la domanda successiva parte con lo stesso contesto: la conversazione è una sola.
* `system_control` viene eseguito **sul dispositivo da cui hai parlato**: il server delega il comando al client
  via WebSocket (`client_tool` → `client_tool_result`); l'app Electron risponde con CPU/RAM/batteria reali e può
  aprire app e URL.

## 2. Struttura

```
JARVIS/
├── run.py                    # avvio server (+ HTTPS self-signed con --ssl)
├── requirements.txt
├── .env.example
├── start_windows.bat         # setup + avvio in un clic (Windows)
├── start_mac.sh              # setup + avvio (macOS)
├── backend/
│   ├── main.py               # FastAPI: WebSocket /ws, /api/config|weather|tts|stt, frontend statico
│   ├── config.py             # impostazioni da .env
│   ├── hub.py                # connessioni, broadcast, tool delegati ai client
│   ├── brain.py              # Claude: persona, tool loop, fallback, errori
│   ├── memory.py             # SQLite: messaggi, fatti, dispositivi
│   ├── voice.py              # TTS (ElevenLabs/OpenAI/edge-tts) e STT (Whisper/Deepgram/locale)
│   ├── certs.py              # certificato HTTPS per la LAN
│   └── skills/
│       ├── __init__.py       # registry con auto-discovery
│       ├── datetime_skill.py # get_current_datetime
│       ├── weather.py        # get_weather (Open-Meteo)
│       ├── system_control.py # system_control, list_devices
│       ├── integrations.py   # web_search, custom_api_webhook, run_local_script
│       └── memory_skill.py   # remember_fact, forget_fact
├── config/integrations.json  # webhook e script consentiti
├── scripts/save_note.py      # esempio di script locale
├── frontend/                 # HUD: index.html, css/hud.css, js/{app,voice,core}.js
└── desktop/                  # app Electron (main.js, preload.js, package.json)
```

## 3. Avvio rapido

### Server (sul PC Windows)

```powershell
cd JARVIS
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # poi inserisci ANTHROPIC_API_KEY
python run.py
```

Apri **http://localhost:8765** in Chrome o Edge → *INITIALISE SYSTEM* → consenti il microfono.
(oppure doppio clic su `start_windows.bat`)

### Server su macOS (se preferisci che il Mac sia il server)

```bash
cd JARVIS
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # inserisci ANTHROPIC_API_KEY
python run.py
```

### Collegare il secondo dispositivo alla stessa sessione

Il microfono nel browser funziona solo su `https://` o `localhost`, quindi per la LAN avvia il server in HTTPS:

```powershell
python run.py --ssl
```

Il server stampa gli indirizzi, es. `https://192.168.1.20:8765/`. Sul Mac apri quell'indirizzo in Chrome,
accetta il certificato self-signed (Avanzate → Procedi), dai un nome al dispositivo (es. `MACBOOK`) e avvia.
Entrambi i client sono nella sessione `default`; per un'altra sessione aggiungi `?session=lavoro` all'URL.

Su Windows apri **firewall** sulla porta 8765 la prima volta (Windows lo chiede in automatico).
Se esponi il server oltre la LAN imposta `JARVIS_ACCESS_TOKEN` e usa `https://IP:8765/?token=...`.

### App desktop (opzionale, Windows e macOS)

Serve Node.js 18+. L'app aggiunge stato CPU/RAM reale, apertura app e la scorciatoia globale Ctrl+Shift+M.

```bash
cd desktop
npm install
JARVIS_SERVER=https://192.168.1.20:8765/ npm start      # macOS
```
```powershell
cd desktop; npm install
$env:JARVIS_SERVER="http://localhost:8765/"; npm start   # Windows
```

> Dentro Electron la Web Speech API di Google non è disponibile: per la voce imposta `OPENAI_API_KEY`
> (Whisper), `DEEPGRAM_API_KEY`, oppure `STT_PROVIDER=local` con `pip install faster-whisper`.

## 4. Uso

| Azione | Come |
|---|---|
| Parlare | Il microfono è sempre in ascolto dopo l'avvio |
| Mute / unmute | Pulsante grande in basso, **Ctrl+M** (Ctrl+Shift+M globale nell'app desktop) |
| Interrompere la voce | Clic sul nucleo o **Esc** |
| Scrivere | Campo "Emergency text input" |
| Azzerare la conversazione | RESET MEMORY (la memoria permanente resta) |

Anti-eco: `echoCancellation` del browser + sospensione della trascrizione mentre J.A.R.V.I.S. parla, con ripresa
automatica dopo ~0,4 s.

## 5. Aggiungere skills

Crea `backend/skills/mia_skill.py`:

```python
from . import Skill, SkillContext

async def handler(params: dict, ctx: SkillContext):
    return {"risultato": "..."}

SKILLS = [Skill(name="mia_skill", label="MIA_API", description="Cosa fa e quando usarla.",
                input_schema={"type": "object", "properties": {}}, handler=handler)]
```

Viene caricata al riavvio. Per webhook (Home Assistant, Zapier, n8n…) e script locali basta aggiungere una voce
in `config/integrations.json`: il modello può chiamare **solo** le voci elencate.

## 6. Cambiare il tono della voce

Tutto si regola nel `.env`, sezione **VOICE TONE**, poi si riavvia il server:

| Impostazione | Effetto |
|---|---|
| `EDGE_TTS_VOICE` / `EDGE_TTS_VOICE_IT` | Quale voce (gratuita) in modalità inglese / italiana |
| `EDGE_TTS_RATE` | Velocità: `-10%` più calmo, `+10%` più svelto |
| `EDGE_TTS_PITCH` / `EDGE_TTS_PITCH_IT` | Tono: `-3Hz` più profondo (oltre `-10Hz` suona finto) |
| `ELEVENLABS_*` | Voce più realistica: `STABILITY` bassa = più espressiva, `STYLE` alto = più teatrale |
| `OPENAI_TTS_INSTRUCTIONS` | Descrivi a parole il tono desiderato |

## 7. Codice e terminale

Se glielo chiedi ("create a Python script that…", "crea un sito HTML…", "run it", "installa requests"),
J.A.R.V.I.S. può creare file, scrivere codice e usare il terminale sul computer che fa da server.

- I file vengono creati solo nella cartella **workspace** (`JARVIS_WORKSPACE`, predefinita `~/JARVIS-Workspace`).
- **Ogni comando del terminale va approvato**: compare un riquadro con il comando esatto; premi
  **APPROVA** (Invio) o **RIFIUTA** (Esc), oppure di' "approve"/"deny" ("approva"/"rifiuta" in italiano).
- Funziona dal computer che esegue il server; dagli altri dispositivi solo se è impostato `JARVIS_ACCESS_TOKEN`.
- Per disattivare tutto: `JARVIS_DEV_TOOLS=false`. Per lavori di programmazione complessi conviene
  `JARVIS_MODEL=claude-sonnet-5-5` o `claude-opus-5-5`.

## 8. Controllo del PC e musica

- *"Jarvis, open YouTube Music"* → apre https://music.youtube.com nel browser, davanti a te.
- *"Jarvis, put Down With Me by Lil Tecca"* → cerca il brano su YouTube Music e lo apre già in riproduzione.
- *"Pause"*, *"next song"*, *"turn the volume up"*, *"mute"* → tasti multimediali del PC (Windows; su macOS solo volume).
- Se la musica non parte da sola, nel browser consenti l'**autoplay** per music.youtube.com.
- Se hai l'**app YouTube Music** installata (da Brave/Chrome/Edge), i brani si aprono lì e non nel browser,
  **minimizzati in background**: la finestra non ti passa davanti e non serve Alt+Tab.
  `JARVIS_MUSIC_FOREGROUND=true` per vederla in primo piano; `JARVIS_MUSIC_PLAYER=browser` per usare sempre il browser.
  A voce puoi chiedere anche *"play ... and show it to me"* per aprirla davanti solo quella volta.

## 9. "Hey Jarvis" e interruzioni

- Il microfono resta sempre acceso, ma J.A.R.V.I.S. **risponde solo alle frasi che iniziano con "Hey Jarvis"**
  (o "Jarvis, ..."). Il resto viene ignorato e non viene inviato da nessuna parte. Al riconoscimento senti un doppio "bip".
- "Hey Jarvis" da solo → bip, poi hai 8 secondi per la richiesta.
- **Interrompere ("couper la parole")**: mentre parla basta dire **"Jarvis"** (anche a metà frase) oppure
  **"stop" / "wait" / "basta" / "aspetta" / "arrête" / "attends"** → si zittisce subito e ascolta la frase successiva,
  senza ridire "Hey Jarvis". *"Jarvis, next song"* lo interrompe ed esegue direttamente la nuova richiesta.
- `.env`: `JARVIS_WAKE_WORD=false` per tornare all'ascolto continuo; `JARVIS_FOLLOWUP_SECONDS=6` per poter
  rispondere senza "Hey Jarvis" nei 6 secondi dopo una sua risposta; `JARVIS_BARGE_IN=false` per disattivare le interruzioni.
- Con le casse a volume alto il microfono può sentire la sua voce: nessun problema, viene ignorata finché non c'è "Hey Jarvis".
  Le cuffie rendono le interruzioni ancora più affidabili.

## 10. Pagine web: vedere e cliccare

*"Hey Jarvis, go on Amazon and search for a gaming mouse"*, *"open Wikipedia and tell me what's on the home page"*:
J.A.R.V.I.S. apre una **sua finestra di Microsoft Edge** (profilo separato, le tue sessioni di Brave non vengono toccate),
legge la pagina, la **guarda** (screenshot), clicca, scrive e scorre mentre tu guardi.

- Pulsanti che **comprano, pagano, inviano, pubblicano o cancellano** → riquadro di approvazione, come per il terminale.
- **Password e dati di pagamento** non li scrive mai: te lo chiede nella finestra.
- Il testo delle pagine è trattato come dato non affidabile: J.A.R.V.I.S. non esegue ordini scritti sulle pagine.
- Richiede `pip install playwright` (già fatto su questo PC). `JARVIS_WEB_BROWSING=false` per disattivarlo.

## 11. Mac helper (controllare il Mac)

Dal browser del Mac J.A.R.V.I.S. può solo parlare. Per fargli controllare **il Mac** (volume, YouTube Music,
Spotify, aprire app e siti, vedere lo schermo) avvia sul Mac il piccolo agente, nel Terminale:

```bash
curl -sk https://<PC-IP>:8765/agent/mac_agent.js -o ~/mac_agent.js
osascript -l JavaScript ~/mac_agent.js https://<PC-IP>:8765 MACBOOK
```

- `MACBOOK` deve essere lo stesso nome dato al dispositivo nella pagina di J.A.R.V.I.S. sul Mac.
- Lascia la finestra del Terminale aperta (Ctrl+C per fermarlo). Usa solo strumenti già presenti in macOS.
- Volume di YouTube Music sul Mac: in Chrome/Brave attiva una volta **Visualizza → Sviluppatore → Consenti JavaScript da Apple Events**.
  Senza, cambia il volume generale del Mac (macOS non ha un volume per singola app).
- Screenshot del Mac: la prima volta macOS chiede di consentire **Terminale** in *Impostazioni → Privacy → Registrazione schermo*.
