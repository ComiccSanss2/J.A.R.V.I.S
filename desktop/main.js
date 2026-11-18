/* J.A.R.V.I.S. OS — Electron desktop client.
 * Loads the HUD from the central server and provides native device functions
 * (CPU/RAM/battery status, opening URLs and applications) to the system_control skill.
 *
 * Server: JARVIS_SERVER variable (e.g. https://192.168.1.20:8765/?token=xxx), default http://localhost:8765
 */
const { app, BrowserWindow, ipcMain, shell, session, globalShortcut, systemPreferences } = require("electron");
const { spawn } = require("child_process");
const os = require("os");
const si = require("systeminformation");

const SERVER = process.env.JARVIS_SERVER || process.argv.find((a) => a.startsWith("http")) || "http://localhost:8765/";
const serverUrl = new URL(SERVER);

// The microphone needs a secure context: treat the JARVIS server as secure even over plain http on the LAN
app.commandLine.appendSwitch("unsafely-treat-insecure-origin-as-secure", serverUrl.origin);
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");

// Self-signed certificate of the JARVIS server (--ssl mode): accepted only for that host
app.on("certificate-error", (event, _wc, url, _err, _cert, callback) => {
  if (new URL(url).host === serverUrl.host) { event.preventDefault(); callback(true); } else callback(false);
});

let win;

function createWindow() {
  win = new BrowserWindow({
    width: 1480, height: 920, minWidth: 900, minHeight: 640,
    backgroundColor: "#030811",
    title: "J.A.R.V.I.S. OS",
    autoHideMenuBar: true,
    webPreferences: { preload: `${__dirname}/preload.js`, contextIsolation: true, sandbox: false, nodeIntegration: false },
  });
  win.loadURL(SERVER);
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" }; });
  win.webContents.on("did-fail-load", (_e, code, desc) => {
    win.loadURL(`data:text/html,<body style="background:%23030811;color:%2300F0FF;font-family:monospace;padding:40px">
      <h2>J.A.R.V.I.S. OS - server unreachable</h2><p>${encodeURIComponent(SERVER)} (${code} ${encodeURIComponent(desc)})</p>
      <p>Start the server with <b>python run.py</b> and try again (Ctrl+R).</p></body>`);
  });
}

app.whenReady().then(async () => {
  if (process.platform === "darwin") {
    try { await systemPreferences.askForMediaAccess("microphone"); } catch { /* ignore */ }
  }
  session.defaultSession.setPermissionRequestHandler((wc, permission, cb) => {
    const fromServer = (() => { try { return new URL(wc.getURL()).origin === serverUrl.origin; } catch { return false; } })();
    cb(fromServer && ["media", "audioCapture", "notifications", "clipboard-sanitized-write"].includes(permission));
  });
  createWindow();
  // Global shortcut: works even when the window is in the background
  globalShortcut.register("CommandOrControl+Shift+M", () => win && win.webContents.send("toggle-mute"));
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on("will-quit", () => globalShortcut.unregisterAll());
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });

// ------------------------------------------------------------ native functions
ipcMain.handle("sys:status", async () => {
  const [load, mem, batt, disks] = await Promise.all([si.currentLoad(), si.mem(), si.battery(), si.fsSize()]);
  const main = disks.find((d) => d.mount === "/" || d.mount.toUpperCase() === "C:") || disks[0];
  const info = {
    device: os.hostname(),
    os: `${os.type()} ${os.release()}`,
    cpu_pct: Math.round(load.currentLoad),
    cpu_cores: os.cpus().length,
    ram_pct: Math.round((mem.active / mem.total) * 100),
    ram_used_gb: +(mem.active / 1024 ** 3).toFixed(1),
    ram_total_gb: +(mem.total / 1024 ** 3).toFixed(1),
    disk_pct: main ? Math.round(main.use) : null,
    uptime_hours: +(os.uptime() / 3600).toFixed(1),
  };
  if (batt.hasBattery) { info.battery_pct = batt.percent; info.charging = batt.isCharging; }
  else info.battery = "none (desktop)";
  return info;
});

ipcMain.handle("sys:openUrl", async (_e, url) => {
  if (!/^https?:\/\//i.test(url)) return { error: "Invalid URL" };
  await shell.openExternal(url);
  return { ok: true, opened: url };
});

ipcMain.handle("sys:openApp", async (_e, appName) => {
  if (!/^[\w .\-]{1,64}$/u.test(appName || "")) return { error: "Invalid application name" };
  return new Promise((resolve) => {
    let child;
    if (process.platform === "darwin") child = spawn("open", ["-a", appName]);
    // the name is already validated (no quotes/metacharacters), so it is safe inside single quotes
    else if (process.platform === "win32") child = spawn("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", `Start-Process -FilePath '${appName}'`], { windowsHide: true });
    else child = spawn(appName, [], { detached: true });
    child.on("error", (err) => resolve({ ok: false, error: err.message }));
    child.on("exit", (code) => resolve(code === 0 ? { ok: true, launched: appName } : { ok: false, error: `Application not found: ${appName}` }));
    setTimeout(() => resolve({ ok: true, launched: appName }), 3000);
  });
});
