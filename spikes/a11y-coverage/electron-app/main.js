// Minimal Electron surface for the Shinken a11y-coverage spike (#2 / E5):
// one BrowserWindow rendering the same labeled form the chromium row uses, so
// AT-SPI and CDP coverage are directly comparable across the two Chromium
// embeddings. Launched by spikes/a11y-coverage/run.sh with
// --force-renderer-accessibility (AT-SPI path) and --remote-debugging-port
// (CDP path); --no-sandbox is required inside the container.
const { app, BrowserWindow } = require("electron");

app.whenReady().then(() => {
  const win = new BrowserWindow({ width: 900, height: 700, title: "Electron Form" });
  win.loadFile("index.html");
});

app.on("window-all-closed", () => app.quit());
