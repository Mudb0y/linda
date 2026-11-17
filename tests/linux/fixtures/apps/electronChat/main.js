// One window, with no menu bar, as chat programs built on Electron open: on index.html, or on the
// page named after the program's directory on the command line.
const { app, BrowserWindow } = require("electron");
const path = require("node:path");

const page = process.argv.find((argument) => argument.endsWith(".html")) ?? "index.html";

app.whenReady().then(() => {
	const window = new BrowserWindow({ width: 900, height: 700, autoHideMenuBar: true });
	window.setMenu(null);
	window.loadFile(path.join(__dirname, page));
});

app.on("window-all-closed", () => app.quit());
