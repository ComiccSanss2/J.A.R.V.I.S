# J.A.R.V.I.S. OS

A personal voice assistant that lives on your desk, talks like a slightly sarcastic British butler, and actually does things on your computer.

You say *"Hey Jarvis, put on something by Lil Tecca"* and the song starts in the background. You say *"turn the music down"* and only the music gets quieter. You ask what's on your screen and it looks. You start a conversation on your Windows PC, walk over to your MacBook, and carry on exactly where you left off.

It's built on Claude (Anthropic's API) with a small Python server and a holographic HUD that runs in the browser.

> Italian version of the detailed guide: [README.it.md](README.it.md)

---

## What it can do

**Conversation**
- Always-on microphone, but it only reacts when you say **"Hey Jarvis"** (or just "Jarvis, …"). Talk to your friends on Discord all you like.
- Answers out loud with streaming speech: it starts talking as soon as the first sentence is ready.
- You can cut it off mid-sentence: say "Jarvis", "stop", "wait", "basta", "arrête"… and it shuts up and listens.
- Matches your energy: "thanks" gets "My pleasure", a real question gets a real answer. Commands like "open YouTube" just happen, with a short beep instead of a speech.
- Three full language modes, switchable from the HUD: **English**, **Italian**, **French** (voice, UI, recognition and personality).

**Your computer**
- Opens websites and apps, reports CPU / RAM / battery.
- Plays music on **YouTube Music** (in the installed desktop app if you have one, minimized so it doesn't steal focus), plus play/pause/next.
- Per-app volume: "turn YouTube Music down" moves that app's slider in the Windows volume mixer, not your whole system.
- Looks at your screen when you ask ("do you see this?").
- Has its own browser window (Edge, via Playwright) to search, read, look at and click through web pages while you watch.
- Writes code and files into a sandboxed workspace and runs terminal commands, **each one approved by you** on screen.

**Everywhere**
- One central server, many devices: PC, Mac, a tablet in the corner. Same conversation, same long-term memory, live sync over WebSockets.
- Only the device you spoke to answers out loud.
- Weather and time follow the real position and time zone of the device you're using.
- A small helper script lets it control a Mac too (volume, music, apps, screenshots), using only tools built into macOS.

**The look**
- Dark HUD with a reactive energy core that pulses with your voice and its own.
- Collapsible chat, live skills log, device mesh, weather and clock panels.
- **Hologram mode**: just the core, full screen on pure black, for a Pepper's-ghost pyramid, a glass plate or a LED fan.

---

## How it works

```
  Windows PC (browser)        MacBook (browser + helper)        Tablet / anything
          │                            │                               │
          └──────────── WebSocket + HTTPS (one shared session) ────────┘
                                       │
                       ┌───────────────▼────────────────┐
                       │      Central server (FastAPI)  │
                       │  brain   ─ Claude + tool loop  │──► Anthropic API
                       │  skills  ─ music, volume, web, │──► Open-Meteo, YouTube Music,
                       │            screen, terminal…   │    OpenStreetMap
                       │  voice   ─ TTS / STT providers │──► edge-tts / ElevenLabs / OpenAI
                       │  memory  ─ SQLite              │
                       └────────────────────────────────┘
```

1. The browser listens continuously (Web Speech API in Chrome/Edge, or Whisper/Deepgram on the server) and only forwards phrases that start with the wake word.
2. The server stores the message, gives Claude the shared history plus live context (local time, current weather, where you are), and lets it call tools.
3. The reply is streamed sentence by sentence to the device that asked, which turns each sentence into speech while the rest is still being written.
4. Everything is broadcast to every connected device, so the conversation stays in sync.

---

## Getting started

### Requirements

- **Python 3.10+** on the computer that runs the server (tested on Windows 10 with Python 3.14)
- An **Anthropic API key** (pay-as-you-go; with Claude Haiku a few dollars last a long time)
- **Chrome or Edge** for voice recognition (Brave doesn't support it)
- Optional: Microsoft Edge or Chrome installed for the web-browsing skills

### Install

```bash
git clone https://github.com/<you>/jarvis-os.git
cd jarvis-os
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
```

Open `.env` and paste your key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

### Run

```bash
python run.py
```

Open **http://localhost:8765** in Chrome or Edge, press **INITIALISE SYSTEM**, allow the microphone, and say *"Hey Jarvis"*.

On Windows you can also just double-click `start_windows.bat` (macOS: `./start_mac.sh`). It creates the virtual environment, installs everything and starts the server.

### Using it from a second device

Browsers only allow the microphone over HTTPS (or on localhost), so start the server with a self-signed certificate:

```bash
python run.py --ssl
```

Then open `https://<PC-IP>:8765` on the other device, accept the certificate warning once, give the device a name and you're in. The server prints the right address when it starts.

If the page loads forever on the other device, it's almost always the Windows firewall. Allow port 8765 on your private network (from an admin PowerShell):

```powershell
New-NetFirewallRule -DisplayName "JARVIS 8765" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private
```

If the server is reachable on a network you don't fully trust, set `JARVIS_ACCESS_TOKEN` in `.env` and open the page with `?token=...`.

### Controlling a Mac

From a Mac browser, J.A.R.V.I.S. can talk but can't touch the system. Run the helper in the Mac's Terminal and keep the window open:

```bash
curl -sk https://<PC-IP>:8765/agent/mac_agent.js -o ~/mac_agent.js
osascript -l JavaScript ~/mac_agent.js https://<PC-IP>:8765 MACBOOK
```

Use the same name you gave the device in the HUD. It's plain JavaScript for Automation, with nothing to install.

---

## Configuration

Everything lives in `.env` (see `.env.example`, every option is commented). The ones you'll probably touch:

| Setting | What it does |
|---|---|
| `JARVIS_MODEL` | `claude-haiku-5-5` (cheap, fast, default) · `claude-sonnet-5-5` · `claude-opus-5-5` (smartest) |
| `JARVIS_LANGUAGE` | Starting language: `en`, `it` or `fr` (switchable later from the HUD) |
| `JARVIS_USER_TITLE` | How it addresses you ("Sir" by default) |
| `JARVIS_CITY` | Fallback city when the device position isn't available |
| `EDGE_TTS_VOICE` / `_RATE` / `_PITCH` | Free voice and its tone (`en-GB-RyanNeural`, `-4%`, `-3Hz`) |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | Much more realistic voice, if you have an ElevenLabs account |
| `JARVIS_WAKE_WORD` | `false` to make it respond to everything it hears |
| `JARVIS_FOLLOWUP_SECONDS` | Seconds after an answer where you can follow up without "Hey Jarvis" |
| `JARVIS_MUSIC_FOREGROUND` | Bring the YouTube Music window to the front, or keep it in the background (also in the HUD ⚙ menu) |
| `JARVIS_DEV_TOOLS` / `JARVIS_CONFIRM_COMMANDS` | Files and terminal access, and whether every command needs approval |
| `JARVIS_WEB_BROWSING` | Its own browser window for web tasks |

---

## Things you can say

| You say | What happens |
|---|---|
| "Hey Jarvis, what's the weather like?" | Answers right away for where you actually are |
| "Jarvis, put Down With Me by Lil Tecca" | Finds it on YouTube Music and plays it in the background |
| "Turn the music down" / "next song" | Lowers only YouTube Music / skips the track |
| "Open Notepad" / "open YouTube" | Opens it, with a short beep and no speech |
| "Do you see what's on my screen?" | Takes a screenshot and tells you |
| "Search Amazon for a hologram fan and open the first result" | Does it in its own browser window while you watch |
| "Write a Python script that renames my photos and run it" | Writes the file and asks you to approve the command |
| "Remember that my favourite colour is red" | Saved in long-term memory, shared across devices |
| "Stop" / "Jarvis" (while it's talking) | It stops and listens |

---

## Safety

It's an assistant that can touch your computer, so a few rules are built in:

- **Terminal commands** always show up on screen with the exact command line and wait for you to approve (click, Enter, or say "approve").
- **Files** can only be created or read inside the workspace folder (`~/JARVIS-Workspace` by default).
- **Web pages** are treated as untrusted data: it won't follow instructions written on a page. Clicks that buy, pay, send, post or delete need your approval, and it never types passwords or card numbers.
- **Other devices** can't use the terminal, screen, browser or volume of the server PC unless you protect the server with an access token.
- **Screenshots** are only taken when you ask about your screen.

---

## Project layout

```
backend/
  main.py          FastAPI app: WebSocket sync, REST API, static HUD
  brain.py         Claude conversation loop, streaming, silent actions
  hub.py           Connected devices, broadcast, helper agents
  i18n.py          English / Italian / French personas and messages
  voice.py         Text-to-speech and speech-to-text providers
  memory.py        SQLite: history, long-term facts, devices, preferences
  skills/          Everything it can do, one module per area:
                   weather, datetime, system_control, media, app_audio,
                   screen, web_browser, devtools, integrations, memory…
frontend/
  index.html       The HUD
  js/              app.js (UI + sync), voice.js (listening/speaking),
                   core.js (the animated core), i18n.js (translations)
  agent/           mac_agent.js, the macOS helper
desktop/           Optional Electron wrapper
config/            Webhooks and local scripts it's allowed to call
```

### Adding a skill

Drop a file in `backend/skills/`:

```python
from . import Skill, SkillContext

async def coin_flip(params: dict, ctx: SkillContext) -> dict:
    import random
    return {"result": random.choice(["heads", "tails"])}

SKILLS = [Skill(name="coin_flip", label="COIN", description="Flips a coin.",
                input_schema={"type": "object", "properties": {}}, handler=coin_flip)]
```

Restart the server and it's available. Claude decides when to use it from the description.

---

## Troubleshooting

- **"Network error" from speech recognition**: you're probably in Brave. Use Chrome or Edge.
- **Method Not Allowed / weird behaviour after an update**: the server is still running old code. Restart it and hard-refresh the page (Ctrl+F5).
- **It hears your Discord friends**: check that Discord outputs to your headset and that the browser uses the right microphone. With the wake word on, it ignores them anyway.
- **The other device loads forever**: firewall (see above), or the two devices aren't on the same network.
- **Silent YouTube Music**: allow autoplay for music.youtube.com in the app/browser.

---

## Costs

The only paid part is the Claude API. With the default model (Claude Haiku) a typical exchange costs a fraction of a cent. Voice, weather, maps and music lookups use free services. ElevenLabs and OpenAI voices are optional.

---

## Credits

Inspired by a certain AI butler from the movies. This is an independent fan project, not affiliated with Marvel or Disney.

Built with [Claude](https://www.anthropic.com), [FastAPI](https://fastapi.tiangolo.com), [edge-tts](https://github.com/rany2/edge-tts), [Playwright](https://playwright.dev), [Open-Meteo](https://open-meteo.com) and [OpenStreetMap](https://www.openstreetmap.org).
