// One window on index.html, with no menu bar, as chat programs built on Electron open.
const { app, BrowserWindow } = require("electron");
const path = require("node:path");

app.whenReady().then(() => {
	const window = new BrowserWindow({ width: 900, height: 700, autoHideMenuBar: true });
	window.setMenu(null);
	window.loadFile(path.join(__dirname, "index.html"));
});

app.on("window-all-closed", () => app.quit());
