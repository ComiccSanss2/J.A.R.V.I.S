// J.A.R.V.I.S. OS — helper agent for macOS.
// Lets J.A.R.V.I.S. act on THIS Mac: volume, music controls, opening apps/sites, status, screenshots.
// Uses only tools built into macOS (osascript / JavaScript for Automation, curl, screencapture, sips).
//
// Run it in the Mac Terminal (keep the window open):
//   curl -sk https://IP-OF-THE-PC:8765/agent/mac_agent.js -o ~/mac_agent.js
//   osascript -l JavaScript ~/mac_agent.js https://IP-OF-THE-PC:8765 MACBOOK [token]
// The name (MACBOOK) must be the same as the device name you typed in the J.A.R.V.I.S. page on this Mac.
ObjC.import("Foundation");

function run(argv) {
  const server = String(argv[0] || "").replace(/\/+$/, "");
  const name = String(argv[1] || "MACBOOK").toUpperCase();
  const token = String(argv[2] || "");
  if (!/^https?:\/\//.test(server)) {
    console.log("Usage: osascript -l JavaScript mac_agent.js https://IP-OF-THE-PC:8765 MACBOOK [token]");
    return;
  }
  const app = Application.currentApplication();
  app.includeStandardAdditions = true;
  const sh = (cmd) => app.doShellScript(cmd);
  const q = (s) => "'" + String(s).replace(/'/g, "'\\''") + "'";
  const auth = token ? " -H " + q("X-Jarvis-Token: " + token) : "";
  const osa = (script) => sh("osascript -e " + q(script));
  const writeFile = (path, text) =>
    $(text).writeToFileAtomicallyEncodingError(path, true, $.NSUTF8StringEncoding, null);
  const RESULT = "/tmp/jarvis_agent_result.json";

  console.log("J.A.R.V.I.S. helper for " + name + " -> " + server + "  (Ctrl+C to stop)");
  let failures = 0;
  while (true) {
    let out = "";
    try {
      out = sh("curl -sk --max-time 40" + auth + " " +
               q(server + "/api/agent/poll?name=" + encodeURIComponent(name) + "&platform=darwin"));
      if (failures) console.log("Connected again.");
      failures = 0;
    } catch (e) {
      if (++failures === 1) console.log("Server unreachable, retrying... (" + e + ")");
      delay(3);
      continue;
    }
    if (!out) continue;                      // nothing to do (the server answers 204 after 25 s)
    let job;
    try { job = JSON.parse(out); } catch (e) { continue; }
    if (!job || !job.job_id) continue;
    let result;
    try { result = handle(job.action, job.params || {}); } catch (e) { result = { error: String(e) }; }
    console.log("[" + job.action + "] " + (result && result.error ? "error: " + result.error : "ok"));
    writeFile(RESULT, JSON.stringify({ job_id: job.job_id, result: result }));
    try {
      sh("curl -sk --max-time 30 -X POST -H 'Content-Type: application/json'" + auth +
         " --data-binary @" + RESULT + " " + q(server + "/api/agent/result"));
    } catch (e) { console.log("Could not send the result: " + e); }
  }

  // ------------------------------------------------------------ actions
  function handle(action, p) {
    switch (action) {
      case "status": return status();
      case "open_url": return openUrl(p.url);
      case "open_app": return openApp(p.app);
      case "volume": return volume(p.action, p.percent);
      case "media": return media(p.action);
      case "app_volume": return appVolume(p.app, p.action, p.percent);
      case "screenshot": return screenshot();
      default: return { error: "Unknown action on the Mac helper: " + action };
    }
  }

  function status() {
    const info = { device: name, os: "macOS " + sh("sw_vers -productVersion") };
    try { info.battery = sh("pmset -g batt | grep -Eo '[0-9]+%; [a-zA-Z ]+' | head -1"); } catch (e) { info.battery = "n/a"; }
    try { info.cpu = sh("top -l 1 -n 0 | grep 'CPU usage'"); } catch (e) {}
    try { info.memory = sh("top -l 1 -n 0 | grep PhysMem"); } catch (e) {}
    try { info.disk = sh("df -h / | tail -1 | awk '{print $5\" used\"}'"); } catch (e) {}
    return info;
  }

  function openUrl(url) {
    if (!/^https?:\/\//i.test(String(url || ""))) return { error: "Invalid URL" };
    sh("open " + q(url));
    return { ok: true, opened: url, ran_on: name };
  }

  function openApp(appName) {
    if (!/^[\w .\-]{1,64}$/.test(String(appName || ""))) return { error: "Invalid application name" };
    try { sh("open -a " + q(appName)); } catch (e) { return { ok: false, error: "Application not found: " + appName }; }
    return { ok: true, launched: appName, ran_on: name };
  }

  // macOS master volume (0-100)
  function volume(action, percent) {
    const step = Math.max(1, Math.min(Number(percent) || 10, 100));
    const before = Number(osa("output volume of (get volume settings)"));
    let after = before;
    if (action === "mute") { osa("set volume output muted true"); }
    else if (action === "unmute") { osa("set volume output muted false"); }
    else {
      after = action === "set" ? step : action === "up" ? before + step : before - step;
      after = Math.max(0, Math.min(100, Math.round(after)));
      osa("set volume output volume " + after);
      if (action !== "down") osa("set volume output muted false");
    }
    return { ok: true, scope: "Mac system volume", from_pct: before, to_pct: after, action: action };
  }

  function running(appName) {
    try { return Application("System Events").processes.byName(appName).exists(); } catch (e) { return false; }
  }

  // Run JavaScript in the YouTube Music tab of Chrome or Brave
  // (needs: View > Developer > "Allow JavaScript from Apple Events" in that browser, once)
  function inYouTubeMusic(js) {
    for (const browser of ["Google Chrome", "Brave Browser"]) {
      if (!running(browser)) continue;
      const b = Application(browser);
      for (const w of b.windows()) {
        for (const tab of w.tabs()) {
          if (String(tab.url()).indexOf("music.youtube.com") !== -1) {
            try { return { ok: true, browser: browser, value: tab.execute({ javascript: js }) }; }
            catch (e) {
              return { error: "In " + browser + " enable View > Developer > Allow JavaScript from Apple Events, then try again." };
            }
          }
        }
      }
    }
    return null;  // no YouTube Music tab open
  }

  function media(action) {
    const appleCmd = { play_pause: "playpause", next: "next track", previous: "previous track", stop: "pause" }[action];
    if (!appleCmd) {
      if (action === "volume_up") return volume("up", 10);
      if (action === "volume_down") return volume("down", 10);
      if (action === "mute") return volume("mute");
      return { error: "Unknown media action " + action };
    }
    const ytm = inYouTubeMusic(
      action === "next" ? "document.querySelector('.next-button')?.click(); 'next'" :
      action === "previous" ? "document.querySelector('.previous-button')?.click(); 'previous'" :
      "document.querySelector('#play-pause-button')?.click(); 'toggled'");
    if (ytm) return ytm.error ? ytm : { ok: true, player: "YouTube Music (" + ytm.browser + ")", action: action };
    for (const player of ["Spotify", "Music"]) {
      if (running(player)) { osa('tell application "' + player + '" to ' + appleCmd); return { ok: true, player: player, action: action }; }
    }
    return { error: "No music player found on the Mac (YouTube Music tab, Spotify or Music)." };
  }

  // macOS has no per-app volume: YouTube Music is changed inside its tab, anything else uses the Mac volume
  function appVolume(appName, action, percent) {
    const a = String(appName || "youtube music").toLowerCase();
    if (a.indexOf("music") !== -1 || a.indexOf("youtube") !== -1) {
      const step = (Number(percent) || 15) / 100;
      const js = "(() => { const v = document.querySelector('video'); if (!v) return 'no-video';" +
        " const before = v.volume;" +
        (action === "mute" ? " v.muted = true;" : action === "unmute" ? " v.muted = false;" :
         " v.muted = false; v.volume = Math.max(0, Math.min(1, " +
         (action === "set" ? step : action === "up" ? "before + " + step : "before - " + step) + "));") +
        " const s = document.querySelector('#volume-slider'); if (s) { s.value = Math.round(v.volume * 100); }" +
        " return Math.round(before * 100) + '>' + Math.round(v.volume * 100); })()";
      const r = inYouTubeMusic(js);
      if (r && !r.error && r.value !== "no-video") {
        const [from, to] = String(r.value).split(">");
        return { ok: true, app: "YouTube Music (" + r.browser + ")", from_pct: Number(from), to_pct: Number(to) };
      }
      if (r && r.error) return r;
    }
    const v = volume(action, percent || 15);
    v.note = "macOS has no per-app volume: the Mac's volume was changed.";
    return v;
  }

  function screenshot() {
    const path = "/tmp/jarvis_screen.jpg";
    sh("screencapture -x -t jpg " + path + " && sips -Z 1366 " + path + " >/dev/null");
    const b64 = sh("base64 -i " + path + " | tr -d '\\n'");
    sh("rm -f " + path);
    if (!b64) return { error: "Screenshot failed: allow Terminal in System Settings > Privacy > Screen Recording." };
    return { note: "Screenshot of the " + name + " screen (untrusted content).",
             _image_b64: b64, _image_media_type: "image/jpeg" };
  }
}
