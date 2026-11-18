/* J.A.R.V.I.S. OS — client logic: WebSocket sync, HUD, voice, language mode, local tools. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const I18n = window.JarvisI18n;
  const t = (k) => I18n.t(k);
  const desktop = window.jarvisDesktop || null; // Electron bridge (desktop/preload.js)

  // ------------------------------------------------------------ safe storage
  const store = {
    get(k, d = null) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  // ------------------------------------------------------------ device identity
  function detectPlatform() {
    if (desktop) return desktop.platform === "darwin" ? "macos" : desktop.platform === "win32" ? "windows" : desktop.platform;
    const ua = navigator.userAgent;
    if (/Windows/i.test(ua)) return "windows";
    if (/iPhone|iPad/i.test(ua)) return "ios";
    if (/Mac OS X|Macintosh/i.test(ua)) return "macos";
    if (/Android/i.test(ua)) return "android";
    if (/Linux/i.test(ua)) return "linux";
    return "unknown";
  }
  const PLATFORM = detectPlatform();
  const DEFAULT_NAMES = { windows: "PC-WINDOWS", macos: "MACBOOK", linux: "LINUX-BOX", android: "ANDROID", ios: "IPHONE" };

  const params = new URLSearchParams(location.search);
  if (params.get("token")) store.set("jarvis.token", params.get("token"));
  const TOKEN = store.get("jarvis.token", "");
  const SESSION = (params.get("session") || store.get("jarvis.session", "default")).replace(/[^\w-]/g, "").slice(0, 48) || "default";
  store.set("jarvis.session", SESSION);
  let deviceId = store.get("jarvis.deviceId");
  if (!deviceId) {
    deviceId = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replace(/[^\w-]/g, "");
    store.set("jarvis.deviceId", deviceId);
  }
  let deviceName = store.get("jarvis.deviceName", DEFAULT_NAMES[PLATFORM] || "DEVICE");

  const apiFetch = (path, opts = {}) =>
    fetch(path, { ...opts, headers: { ...(opts.headers || {}), ...(TOKEN ? { "X-Jarvis-Token": TOKEN } : {}) } });

  // ------------------------------------------------------------ state
  const S = { ws: null, clientId: null, online: false, thinking: false, config: null, booted: false,
              seen: new Set(), history: [], logCount: 0, retry: 1000, coords: null, place: null };
  const API_VERSION = 4;
  S.awakeUntil = 0;          // after "Hey Jarvis" alone, the next phrase is accepted until this time
  S.spokenTurnText = "";     // what J.A.R.V.I.S. is saying now (to ignore his own voice picked up by the mic)
  const WAKE_WINDOW_MS = 8000;
  const STOP_LISTEN_MS = 20000;   // after "stop" / "Jarvis" he keeps listening this long, no wake word needed
  const TIMEZONE = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; } })();
  try { S.coords = JSON.parse(store.get("jarvis.coords", "null")); } catch { S.coords = null; }
  const core = new JarvisCore($("core"));
  let voice = null;

  // ------------------------------------------------------------ UI helpers
  let toastTimer;
  function toast(msg, ms = 4200) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), ms);
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const hhmmss = (ts) => new Date(ts * 1000).toLocaleTimeString(I18n.locale, { hour12: false });

  // ------------------------------------------------------------ language mode
  function applyLanguage(profile, announce = false) {
    if (typeof profile === "string") profile = { code: profile.slice(0, 2), locale: profile }; // older servers
    if (!profile || !profile.code) return;
    const changed = profile.code !== I18n.code;
    I18n.set(profile.code, profile.locale);
    if (voice) voice.setLang(profile.locale);
    tickClock();
    refreshState();
    if (changed) {
      resetTranscript(S.history);
      loadWeather();
      if (announce) toast(t("lang_switched"));
    }
  }

  async function chooseLanguage(code) {
    if (code === I18n.code) return;
    try {
      const r = await apiFetch("/api/language", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ language: code }),
      });
      if (r.status === 404 || r.status === 405) throw new Error(t("server_outdated"));
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || r.status);
      applyLanguage(data.language, S.booted); // other devices get it through the WebSocket broadcast
    } catch (err) {
      toast(String(err.message || err));
    }
  }

  // ------------------------------------------------------------ core / mic state
  function refreshState() {
    let st;
    if (!S.online) st = S.booted ? "offline" : "muted";
    else if (voice && voice.speaking) st = "speaking";
    else if (S.thinking) st = "thinking";
    else if (!voice || !voice.active) st = "muted";
    else if (wakeMode() && !isAwake()) st = "standby";   // mic on, waiting for "Hey Jarvis"
    else st = "listening";
    core.setState(st);
    document.body.className = `st-${st}`;
    $("coreState").textContent = S.booted || S.online ? t(`st_${st}`) : "STANDBY";
    const sub = voice ? `STT ${voice.sttLabel} ${I18n.locale} · TTS ${voice.ttsProvider === "—" ? (S.config?.tts_chain?.[0] || "browser").toUpperCase() : voice.ttsProvider}` : "";
    $("coreSub").textContent = `${sub}${S.config ? ` · ${S.config.model}` : ""}`;

    const btn = $("micBtn");
    const mic = !voice || !voice.active ? "muted" : voice.suspended ? "paused" : "live";
    btn.className = `mic-btn ${mic}`;
    btn.setAttribute("aria-pressed", String(mic !== "muted"));
    $("micLabel").textContent = t(mic === "muted" ? "mic_muted" : mic === "paused" ? "mic_paused"
      : wakeMode() && !isAwake() ? "mic_wake" : "mic_live");
    $("micHint").textContent = t(mic === "muted" ? "mic_hint_on" : "mic_hint_off");
    $("linkState").textContent = S.online ? t("sync_online") : t("offline");
  }

  function levelLoop() {
    if (voice) {
      const { level, spectrum } = voice.levels();
      core.setLevel(level);
      core.setSpectrum(spectrum);
    }
    requestAnimationFrame(levelLoop);
  }

  // ------------------------------------------------------------ transcript
  function renderMessage(m, remember = true) {
    if (S.seen.has(m.id)) return;
    S.seen.add(m.id);
    if (remember) { S.history.push(m); if (S.history.length > 120) S.history.shift(); }
    const box = $("transcript");
    box.querySelector(".empty")?.remove();
    const el = document.createElement("div");
    el.className = `msg ${m.role}`;
    const who = m.role === "user" ? `${t("you")} · ${m.device_name || "?"}` : "J.A.R.V.I.S.";
    el.innerHTML = `<span class="who">${esc(who)} · ${hhmmss(m.created)}</span><p>${esc(m.content)}</p>`;
    box.appendChild(el);
    while (box.children.length > 120) box.firstChild.remove();
    box.scrollTop = box.scrollHeight;
  }

  function renderPartial(turn, text) {
    const box = $("transcript");
    box.querySelector(".empty")?.remove();
    let el = box.querySelector(`.msg.partial[data-turn="${turn}"]`);
    if (!el) {
      el = document.createElement("div");
      el.className = "msg assistant partial";
      el.dataset.turn = turn;
      el.innerHTML = `<span class="who">J.A.R.V.I.S. · …</span><p></p>`;
      box.appendChild(el);
    }
    el.querySelector("p").textContent = text;
    box.scrollTop = box.scrollHeight;
  }

  function resetTranscript(history) {
    S.seen.clear();
    S.history = [...history];
    $("transcript").innerHTML = `<div class="empty muted">${esc(t("transcript_empty"))}</div>`;
    history.forEach((m) => renderMessage(m, false));
  }

  // ------------------------------------------------------------ panels
  let lastDevices = [];
  function renderDevices(devices) {
    lastDevices = devices;
    const ul = $("deviceList");
    if (!devices.length) { ul.innerHTML = `<li class="muted">${esc(t("no_devices"))}</li>`; return; }
    ul.innerHTML = devices.map((d) => `
      <li class="${d.device_id === deviceId ? "self" : ""}" title="${esc(d.platform)} · ${esc(d.agent)}">
        <span>${esc(d.name)}${d.online && d.mic === "muted" ? `<span class="mic">${esc(t("mic_off"))}</span>` : ""}${d.online && d.location ? `<span class="loc">◉ ${esc(d.location)}</span>` : ""}</span>
        <span class="st ${d.online ? "on" : "off"}">${esc(d.online ? t("online") : t("offline"))}</span>
      </li>`).join("");
  }

  let lastFacts = {};
  function renderFacts(facts) {
    lastFacts = facts || {};
    const entries = Object.entries(lastFacts);
    $("factList").innerHTML = entries.length
      ? entries.map(([k, v]) => `<li><b>${esc(k)}</b>${esc(v)}</li>`).join("")
      : `<li class="muted">${esc(t("memory_empty"))}</li>`;
  }

  function renderTools(skills) {
    $("toolList").innerHTML = (skills || []).map((s) => `<li><code>${esc(s.label)}</code><span>${esc(s.description)}</span></li>`).join("");
  }

  function addLog(e) {
    const ol = $("skillLog");
    ol.querySelector(".muted")?.remove();
    const cls = e.status === "OK" ? "ok" : e.status === "RUN" ? "run" : "fail";
    const li = document.createElement("li");
    li.className = cls === "ok" ? "" : cls;
    const txt = { ok: "ok", run: "run-t", fail: "fail-t" }[cls];
    li.innerHTML = `<span class="ts">${hhmmss(e.ts)}</span><span class="tag">[SKILL: ${esc(e.skill)} -&gt; <span class="${txt}">${esc(e.status)}</span>]</span>` +
      `${e.detail || e.device ? `<span class="det">${esc(e.device)}${e.detail ? " · " + esc(e.detail) : ""}</span>` : ""}`;
    ol.prepend(li);
    while (ol.children.length > 80) ol.lastChild.remove();
    $("logCount").textContent = ++S.logCount;
  }

  // ------------------------------------------------------------ clock / weather
  function tickClock() {
    const now = new Date();
    const hm = now.toLocaleTimeString(I18n.locale, { hour: "2-digit", minute: "2-digit", hour12: false });
    $("clockTime").innerHTML = `${hm}<span>${String(now.getSeconds()).padStart(2, "0")}</span>`;
    $("clockDay").textContent = now.toLocaleDateString(I18n.locale, { weekday: "long" });
    $("clockDate").textContent = now.toLocaleDateString(I18n.locale, { day: "numeric", month: "long", year: "numeric" });
  }

  async function loadWeather() {
    try {
      const q = S.coords ? `?lat=${S.coords.lat.toFixed(4)}&lon=${S.coords.lon.toFixed(4)}` : "";
      const r = await apiFetch(`/api/weather${q}`);
      const w = await r.json();
      if (!r.ok) throw new Error(w.error || r.status);
      const a = w.current;
      $("wxCity").textContent = (w.source === "device" ? "◉ " : "") + w.location.split(",")[0];
      $("wxTemp").textContent = `${Math.round(a.temperature_c)}°`;
      $("wxCond").textContent = a.conditions;
      $("wxMeta").textContent = `${t("feels")} ${Math.round(a.feels_like_c)}° · ${t("humidity")} ${a.humidity_pct}% · ${t("wind")} ${Math.round(a.wind_kmh)} km/h`;
      $("wxForecast").innerHTML = w.forecast.map((d) => {
        const day = new Date(d.date + "T12:00").toLocaleDateString(I18n.locale, { weekday: "short" });
        return `<li><span class="d">${esc(day)}</span><span>${esc(d.conditions)}</span><span class="t">${Math.round(d.min_c)}° / ${Math.round(d.max_c)}° · ${d.rain_chance_pct ?? 0}%</span></li>`;
      }).join("");
    } catch (err) {
      $("wxCond").textContent = t("wx_unavailable");
      $("wxMeta").textContent = String(err.message || err);
    }
  }

  // ------------------------------------------------------------ tools run on this device
  // ------------------------------------------------------------ approval of terminal commands
  const APPROVE_WORDS = /^(approve|approved|confirm|run it|go ahead|yes,? run it|approva|approvato|conferma|procedi|esegui|approuve|approuvé|approuver|confirme|vas-y|allez-y|exécute|exécute-la)[.!]?$/i;
  const DENY_WORDS = /^(deny|denied|cancel|stop|don't|do not|rifiuta|annulla|nega|non farlo|refuse|refusé|annule|non|arrête|ne le fais pas)[.!]?$/i;
  let pendingApproval = null;

  function askApproval(input) {
    return new Promise((resolve) => {
      if (pendingApproval) pendingApproval(false);     // only one request at a time
      $("approvalReason").textContent = input.reason || t("approval_default");
      $("approvalCommand").textContent = input.command || "";
      $("approvalCwd").textContent = input.cwd || "";
      $("approvalCwd").parentElement.firstElementChild.textContent = t(input.kind === "browser" ? "approval_page" : "approval_folder");
      $("approval").classList.remove("hidden");
      $("approveBtn").focus();
      const expire = setTimeout(() => pendingApproval && pendingApproval(false), 115000); // server gives up at 120 s
      pendingApproval = (ok) => {
        clearTimeout(expire);
        pendingApproval = null;
        $("approval").classList.add("hidden");
        toast(t(ok ? "approved" : "denied"), 2500);
        resolve(ok);
      };
    });
  }
  $("approveBtn").addEventListener("click", () => pendingApproval && pendingApproval(true));
  $("denyBtn").addEventListener("click", () => pendingApproval && pendingApproval(false));

  async function execClientTool(name, input) {
    if (name === "approval") return askApproval(input);
    if (name !== "system_control") return { error: `Tool not supported by the client: ${name}` };
    const { action } = input;
    if (desktop) {
      if (action === "status") return desktop.systemStatus();
      if (action === "open_url") return desktop.openUrl(input.url);
      if (action === "open_app") return desktop.openApp(input.app);
    }
    if (action === "status") {
      const info = {
        device: deviceName, os: PLATFORM, browser: navigator.userAgent.split(") ").pop(),
        cpu_cores: navigator.hardwareConcurrency, ram_gb_approx: navigator.deviceMemory ?? "n/a",
        note: "Live CPU/RAM figures are only available in the desktop app (desktop/ folder).",
      };
      if (navigator.getBattery) {
        try {
          const b = await navigator.getBattery();
          info.battery_pct = Math.round(b.level * 100);
          info.charging = b.charging;
        } catch { /* not supported */ }
      }
      return info;
    }
    if (action === "open_url") {
      const w = window.open(input.url, "_blank");
      if (!w) return { error: "The browser blocked the popup: allow popups for this site or use the desktop app." };
      w.opener = null;
      return { ok: true, opened: input.url };
    }
    if (action === "open_app") return { error: "Opening applications requires the J.A.R.V.I.S. desktop app (Electron) on this device." };
    return { error: "Unknown action" };
  }

  // ------------------------------------------------------------ WebSocket
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const q = new URLSearchParams({ session: SESSION, device_id: deviceId, device_name: deviceName, platform: PLATFORM, agent: desktop ? "electron" : "browser", tz: TIMEZONE });
    if (TOKEN) q.set("token", TOKEN);
    const ws = new WebSocket(`${proto}://${location.host}/ws?${q}`);
    S.ws = ws;

    ws.onopen = () => {
      S.online = true;
      S.retry = 1000;
      $("linkChip").classList.add("online");
      sendMic();
      sendLocation();
      refreshState();
    };
    ws.onclose = (e) => {
      S.online = false;
      S.thinking = false;
      $("linkChip").classList.remove("online");
      refreshState();
      if (e.code === 4401) { toast(t("bad_token"), 8000); return; }
      setTimeout(connect, S.retry);
      S.retry = Math.min(S.retry * 1.7, 10000);
    };
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      switch (m.type) {
        case "hello":
          S.clientId = m.client_id;
          applyLanguage(m.language);
          resetTranscript(m.history || []);
          renderDevices(m.devices || []);
          renderFacts(m.facts);
          renderTools(m.skills);
          break;
        case "language":
          applyLanguage(m.language, true);
          renderDevices(lastDevices);
          renderFacts(lastFacts);
          break;
        case "message":
          if (m.turn) $("transcript").querySelector(`.msg.partial[data-turn="${m.turn}"]`)?.remove();
          renderMessage(m.message);
          break;
        case "partial":   // the answer appears while J.A.R.V.I.S. is still writing it
          renderPartial(m.turn, m.text);
          break;
        case "speak_chunk":   // one sentence ready: speak it now (only sent to the device that asked)
          if (voice) {
            if (!voice.speaking) S.spokenTurnText = "";
            S.spokenTurnText = (S.spokenTurnText + " " + m.text).slice(-1500);
            voice.enqueue(m.text, m.turn);
          }
          break;
        case "speak_end":
          if (voice) {
            voice.endTurn(m.turn);
            if (m.silent) voice.doneTone();   // a command executed without words: short "done" sound
          }
          break;
        case "state":
          S.thinking = m.state === "thinking";
          refreshState();
          break;
        case "interim":
          if (m.device !== deviceName) $("interim").textContent = m.text ? `${m.device}: ${m.text}` : "";
          break;
        case "location": S.place = m.place; break;
        case "prefs": applyPrefs(m.prefs); break;
        case "skill_log": addLog(m); break;
        case "devices": renderDevices(m.devices); break;
        case "facts": renderFacts(m.facts); break;
        case "cleared": resetTranscript([]); toast(t("cleared")); break;
        case "client_tool":
          execClientTool(m.name, m.input || {})
            .catch((err) => ({ error: String(err.message || err) }))
            .then((result) => send({ type: "client_tool_result", request_id: m.request_id, result }));
          break;
      }
    };
  }

  function send(obj) {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) { S.ws.send(JSON.stringify(obj)); return true; }
    return false;
  }
  const sendMic = () => send({ type: "mic", state: voice && voice.active ? "listening" : "muted" });
  const sendLocation = () => S.coords && send({ type: "location", lat: S.coords.lat, lon: S.coords.lon, accuracy: S.coords.accuracy });

  // ------------------------------------------------------------ real position (GPS / Wi-Fi)
  let locWarned = false;
  function updateLocation() {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition((pos) => {
      const c = { lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy: Math.round(pos.coords.accuracy) };
      const moved = !S.coords || Math.abs(S.coords.lat - c.lat) > 0.005 || Math.abs(S.coords.lon - c.lon) > 0.005;
      S.coords = c;
      store.set("jarvis.coords", JSON.stringify(c));
      sendLocation();
      if (moved) loadWeather();
    }, () => {
      if (!locWarned && !S.coords) { locWarned = true; toast(t("loc_denied"), 7000); }
    }, { enableHighAccuracy: false, timeout: 20000, maximumAge: 5 * 60 * 1000 });
  }

  // ------------------------------------------------------------ wake word ("Hey Jarvis") & barge-in
  // Speech recognisers spell the name in many ways; "Travis" is left out on purpose (Travis Scott...)
  const NAME = "(?:jarvis|jarviz|jarvi|jarvas|jarves|jervis|jerviss|giarvis|gervis|charvis|garvis|jarbis|j\\.?a\\.?r\\.?v\\.?i\\.?s)";
  const HEY = "(?:hey|hi|hello|ok|okay|ehi|ehy|hei|ei|eh|ciao|ue|hé|he|eh|dis|salut|bonjour|allô|allo)";
  const WAKE_AT_START = new RegExp(`^\\W*(?:${HEY}\\W+)?${NAME}\\b[\\s,.!?:;]*`, "i");
  const WAKE_ANYWHERE = new RegExp(`\\b${HEY}\\W+${NAME}\\b[\\s,.!?:;]*`, "i");

  const wakeMode = () => !!(S.config && S.config.wake_word);
  const isAwake = () => Date.now() < S.awakeUntil;

  /* Returns null if the phrase is not addressed to J.A.R.V.I.S., otherwise the request after the name. */
  function findWake(text) {
    let m = text.match(WAKE_AT_START);
    if (m) return text.slice(m[0].length).trim();
    m = text.match(WAKE_ANYWHERE);
    if (m) return text.slice(m.index + m[0].length).trim();
    return null;
  }

  function wakeUp(ms = WAKE_WINDOW_MS) {
    const wasAwake = isAwake();
    S.awakeUntil = Math.max(S.awakeUntil, Date.now() + ms);
    if (!wasAwake && voice) voice.chime();
    refreshState();
  }

  /* ---------- Cutting J.A.R.V.I.S. off ("couper la parole") ----------
     While he talks (and for a few seconds after you stop him) these also count:
     - his name ANYWHERE in what you say: "… Jarvis, open YouTube" -> he stops and takes the request;
     - a stop word at the start or the end: "stop", "wait", "basta", "aspetta", "arrête", "attends"…
       -> he stops and listens to your next phrase (no need to say "Hey Jarvis" again). */
  const BARGE_MS = 4000;
  const BARE_NAME = new RegExp(`\\b${NAME}\\b[\\s,.!?:;]*`, "i");
  const STOP_WORDS = "(?:stop|stop it|wait|hold on|hang on|enough|silence|shut up|quiet|be quiet|pause|cancel|" +
    "basta|fermati|ferma|stop stop|aspetta|zitto|silenzio|taci|" +
    "arrête|arrete|arrête-toi|attends|tais-toi|chut|ça suffit|ca suffit|silence)";
  const STOP_AT_START = new RegExp(`^\\W*${STOP_WORDS}\\b`, "i");
  const STOP_AT_END = new RegExp(`(?:^|\\s)${STOP_WORDS}\\W*$`, "i");
  S.bargeUntil = 0;

  const bargeWindow = () => !!(voice && voice.speaking) || Date.now() < S.bargeUntil;

  /* While he is talking: is this phrase meant to cut him off? Returns { rest, stop } or null. */
  function findBarge(text) {
    const wake = findWake(text);
    if (wake !== null) return { rest: wake, stop: false };
    const m = text.match(BARE_NAME);
    if (m) return { rest: text.slice(m.index + m[0].length).trim(), stop: false };
    const words = text.trim().split(/\s+/).length;
    if ((words <= 4 && STOP_AT_START.test(text)) || STOP_AT_END.test(text)) return { rest: "", stop: true };
    return null;
  }

  /* Talking over J.A.R.V.I.S.: he stops at once and the server drops the rest of his answer. */
  function interrupt() {
    S.bargeUntil = Date.now() + BARGE_MS;   // the final transcript of this phrase arrives a bit later
    if (!voice || !voice.speaking) return;
    voice.stopSpeaking();
    send({ type: "interrupt" });
  }

  /* Is this transcript just J.A.R.V.I.S.'s own voice coming back through the speakers? */
  function isEcho(text) {
    if (!S.spokenTurnText || !bargeWindow()) return false;
    const norm = (x) => x.toLowerCase().replace(/[^\p{L}\p{N} ]/gu, "").replace(/\s+/g, " ").trim();
    const said = ` ${norm(S.spokenTurnText)} `;
    const heard = norm(text);
    return heard.split(" ").length >= 3 && said.includes(` ${heard} `);
  }

  function onVoiceInterim(text) {
    if (!wakeMode()) return text;
    if (bargeWindow()) {
      const b = findBarge(text);
      if (b && !isEcho(text)) {
        interrupt();          // stop talking as soon as the name or a stop word is heard
        wakeUp(STOP_LISTEN_MS);
        return b.rest || "…";
      }
      if (isAwake() && !(voice && voice.speaking)) { if (text) wakeUp(); return text; }
      return "";
    }
    const rest = findWake(text);
    if (rest !== null) { wakeUp(); return rest || "…"; }
    if (isAwake()) { if (text) wakeUp(); return text; }   // still talking to him: keep listening
    return "";   // not for him: show nothing
  }

  function onVoiceFinal(text) {
    text = text.trim();
    if (!text) return;
    if (pendingApproval && (APPROVE_WORDS.test(text) || DENY_WORDS.test(text))) { sendUserText(text); return; }
    if (!wakeMode()) { sendUserText(text); return; }
    if (isEcho(text)) return;
    const b = bargeWindow() ? findBarge(text) : null;
    const rest = b ? (b.stop ? "" : b.rest) : findWake(text);
    if (b || rest !== null) {
      interrupt();
      if (rest && rest.length > 1) {    // "Hey Jarvis, open YouTube Music" / "… Jarvis, next song"
        S.awakeUntil = 0;
        if (voice) voice.chime();
        sendUserText(rest);
      } else {                          // "Hey Jarvis" / "stop": listen to the next phrase
        wakeUp(b ? STOP_LISTEN_MS : WAKE_WINDOW_MS);
      }
      refreshState();
      return;
    }
    if (isAwake() && !(voice && voice.speaking)) {
      S.awakeUntil = 0;
      sendUserText(text);
      refreshState();
    }
    // otherwise: not addressed to J.A.R.V.I.S. -> ignored
  }

  function sendUserText(text) {
    text = text.trim();
    if (!text) return;
    if (pendingApproval) {   // while a command waits for approval, "approve" / "deny" answer it
      if (APPROVE_WORDS.test(text)) { pendingApproval(true); return; }
      if (DENY_WORDS.test(text)) { pendingApproval(false); return; }
    }
    if (voice && voice.speaking) voice.stopSpeaking();
    if (!send({ type: "user_message", text })) toast(t("not_connected"));
  }

  // ------------------------------------------------------------ voice
  function setupVoice() {
    voice = new VoiceEngine({
      lang: I18n.locale,
      sttServer: S.config.stt_server,
      sttFallback: S.config.stt_fallback,
      ttsChain: S.config.tts_chain,
      apiFetch,
      forceServerStt: !!desktop, // the Web Speech API does not work inside Electron
      bargeIn: !!(S.config.barge_in && S.config.wake_word),   // interrupting needs the wake word (echo safety)
    });
    let lastInterim = 0;
    voice.addEventListener("final", (e) => { $("interim").textContent = ""; send({ type: "interim", text: "" }); onVoiceFinal(e.detail); });
    voice.addEventListener("interim", (e) => {
      const shown = onVoiceInterim(e.detail || "");
      $("interim").textContent = shown;
      const now = Date.now();
      if (now - lastInterim > 300 || !shown) { lastInterim = now; send({ type: "interim", text: shown }); }
    });
    voice.addEventListener("mic", () => { sendMic(); refreshState(); });
    voice.addEventListener("speak", (e) => {
      if (e.detail === false) {
        // a "stop" said right as he finishes still counts
        S.bargeUntil = Math.max(S.bargeUntil || 0, Date.now() + 3000);
        const follow = (S.config && S.config.followup_seconds) || 0;
        if (wakeMode() && follow > 0) S.awakeUntil = Date.now() + follow * 1000;  // optional follow-up window
      }
      refreshState();
    });
    voice.addEventListener("mode", refreshState);
    voice.addEventListener("error", (e) => { toast(e.detail, 7000); refreshState(); });
    voice.addEventListener("warn", (e) => console.warn(e.detail));
  }

  async function toggleMic() {
    if (!voice) return;
    await voice.toggle();
    refreshState();
  }

  // ------------------------------------------------------------ boot
  async function loadConfig() {
    const r = await apiFetch("/api/config");
    if (r.status === 401) throw new Error(t("token_required"));
    if (!r.ok) throw new Error(`${t("unreachable")} (${r.status})`);
    return r.json();
  }

  async function preload() {
    try {
      S.config = await loadConfig();
      if ((S.config.api_version || 0) < API_VERSION) toast(t("server_outdated"), 15000);
      applyPrefs(S.config.prefs);
      applyLanguage(S.config.language);
      renderTools(S.config.skills);
      loadWeather();
    } catch (err) {
      $("bootHint").textContent = err.message;
      $("bootHint").classList.add("err");
    }
  }

  async function boot() {
    const name = $("bootDeviceName").value.trim().toUpperCase().replace(/[^\w\- ]/g, "").slice(0, 24);
    if (name) { deviceName = name; store.set("jarvis.deviceName", name); }
    $("bootBtn").disabled = true;
    try {
      if (!S.config) S.config = await loadConfig();
    } catch (err) {
      $("bootHint").textContent = err.message;
      $("bootHint").classList.add("err");
      $("bootBtn").disabled = false;
      return;
    }
    S.booted = true;
    applyLanguage(S.config.language);
    $("deviceLabel").textContent = deviceName;
    $("sessionName").textContent = SESSION;
    setupVoice();
    await voice.init();
    $("boot").classList.add("hidden");
    connect();
    updateLocation();
    setInterval(updateLocation, 10 * 60 * 1000);
    await voice.start();   // continuous listening from the start
    refreshState();
  }

  // ------------------------------------------------------------ settings panel, chat, hologram
  function applyPrefs(prefs) {
    if (prefs && "music_foreground" in prefs) $("optMusicFront").checked = !!prefs.music_foreground;
  }

  async function savePrefs(changes) {
    try {
      const r = await apiFetch("/api/prefs", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
      });
      if (r.status === 404 || r.status === 405) throw new Error(t("server_outdated"));
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || r.status);
      applyPrefs(data.prefs);
      if ("music_foreground" in changes) toast(t(changes.music_foreground ? "music_front_on" : "music_front_off"), 2500);
    } catch (err) {
      toast(String(err.message || err));
    }
  }

  function setChatVisible(visible) {
    $("dock").classList.toggle("chat-collapsed", !visible);
    $("chatToggle").setAttribute("aria-expanded", String(visible));
    $("optChat").checked = visible;
    store.set("jarvis.chatHidden", visible ? "0" : "1");
    if (visible) $("transcript").scrollTop = $("transcript").scrollHeight;
  }

  function toggleSettings(open) {
    const panel = $("settingsPanel");
    const show = open ?? panel.classList.contains("hidden");
    panel.classList.toggle("hidden", !show);
    $("settingsBtn").setAttribute("aria-expanded", String(show));
  }

  function setHologram(on) {
    document.body.classList.toggle("hologram", on);
    document.body.classList.toggle("mirror", on && $("optMirror").checked);
    if (on) {
      toggleSettings(false);
      document.documentElement.requestFullscreen?.().catch(() => {});
    } else if (document.fullscreenElement) {
      document.exitFullscreen?.().catch(() => {});
    }
  }
  const inHologram = () => document.body.classList.contains("hologram");

  $("settingsBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleSettings(); });
  $("settingsPanel").addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => toggleSettings(false));
  $("optMusicFront").addEventListener("change", (e) => savePrefs({ music_foreground: e.target.checked }));
  $("optChat").addEventListener("change", (e) => setChatVisible(e.target.checked));
  $("chatToggle").addEventListener("click", () => setChatVisible($("dock").classList.contains("chat-collapsed")));
  $("optMirror").addEventListener("change", (e) => store.set("jarvis.holoMirror", e.target.checked ? "1" : "0"));
  $("holoBtn").addEventListener("click", () => setHologram(true));
  document.addEventListener("dblclick", () => { if (inHologram()) setHologram(false); });
  document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement && inHologram()) setHologram(false); });
  $("optMirror").checked = store.get("jarvis.holoMirror") === "1";
  setChatVisible(store.get("jarvis.chatHidden") !== "1");

  // ------------------------------------------------------------ UI events
  $("bootDeviceName").value = deviceName;
  $("bootBtn").addEventListener("click", boot);
  $("bootDeviceName").addEventListener("keydown", (e) => { if (e.key === "Enter") boot(); });
  document.querySelectorAll("[data-lang]").forEach((b) => b.addEventListener("click", () => chooseLanguage(b.dataset.lang)));
  $("micBtn").addEventListener("click", toggleMic);
  $("coreWrap").addEventListener("click", () => voice && voice.speaking && voice.stopSpeaking());
  $("textForm").addEventListener("submit", (e) => {
    e.preventDefault();
    sendUserText($("textInput").value);
    $("textInput").value = "";
  });
  $("clearBtn").addEventListener("click", () => {
    if (confirm(t("confirm_clear"))) send({ type: "clear_session" });
  });
  document.addEventListener("keydown", (e) => {
    if (pendingApproval) {
      if (e.key === "Enter") { e.preventDefault(); pendingApproval(true); return; }
      if (e.key === "Escape") { e.preventDefault(); pendingApproval(false); return; }
    }
    if (e.ctrlKey && !e.shiftKey && (e.key === "m" || e.key === "M")) { e.preventDefault(); toggleMic(); }
    if (e.key === "Escape" && inHologram()) { setHologram(false); return; }
    if (e.key === "Escape" && voice && voice.speaking) voice.stopSpeaking();
  });
  if (desktop && desktop.onToggleMute) desktop.onToggleMute(toggleMic);
  setInterval(() => send({ type: "ping" }), 25000);
  let wasAwake = false;
  setInterval(() => { const a = isAwake(); if (a !== wasAwake) { wasAwake = a; refreshState(); } }, 300);

  tickClock();
  setInterval(tickClock, 1000);
  setInterval(loadWeather, 10 * 60 * 1000);
  refreshState();
  requestAnimationFrame(levelLoop);
  preload().then(() => { if (desktop && store.get("jarvis.deviceName")) boot(); }); // the desktop app boots by itself
})();
