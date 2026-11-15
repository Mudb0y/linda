"""Writes the benchmark's web pages. The same arguments always give the same page.

usage: makePage.py normal|large OUT.html
       makePage.py mutation OUT.html START_MS INTERVAL_MS STEPS SEED

The normal page comes to about 4,100 accessible objects in Firefox 160 and the
large one to about 39,500. The mutation page changes itself STEPS times,
INTERVAL_MS apart, the first START_MS after load, each change drawn at random
from SEED.
"""

import json
import sys

TITLES = {
	"normal": "Basedreader bench normal page",
	"large": "Basedreader bench large page",
	"mutation": "Basedreader bench mutation page",
}

# Sections, table rows and form fields.
SIZES = {
	"normal": (100, 60, 30),
	"large": (1000, 600, 60),
}

MUTATION_SECTIONS = 40


def head(title: str) -> str:
	return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title></head><body>'


def form(fields: int) -> str:
	p = ['<form id="f" aria-label="Test form"><fieldset><legend>Fields</legend>']
	for i in range(fields):
		kind = i % 5
		if kind == 0:
			p.append(f'<label for="t{i}">Text {i}</label> <input id="t{i}" type="text" value="value {i}"><br>')
		elif kind == 1:
			p.append(f'<label><input type="checkbox" id="c{i}"> Check {i}</label><br>')
		elif kind == 2:
			options = "".join(f"<option>Option {j}</option>" for j in range(5))
			p.append(f'<label for="s{i}x">Select {i}</label> <select id="s{i}x">{options}</select><br>')
		elif kind == 3:
			p.append(f'<label><input type="radio" name="r" id="r{i}"> Radio {i}</label><br>')
		else:
			p.append(f'<label for="a{i}">Area {i}</label> <textarea id="a{i}">Some text {i}</textarea><br>')
	p.append('<button type="button" id="b1">Push me</button></fieldset></form>')
	return "".join(p)


def section(s: int, sections: int) -> str:
	nxt = (s + 1) % sections
	p = [f'<section id="s{s}" aria-labelledby="h{s}"><h2 id="h{s}">Heading {s}</h2>']
	for k in range(3):
		p.append(
			f'<p>Paragraph {k} of section {s} has <a href="#s{nxt}">a link to section {nxt}</a>, '
			f'some <strong>bold text</strong>, <em>emphasis</em> and <a href="#h{s}">another link {k}</a>. '
			"Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor.</p>"
		)
	items = "".join(f'<li>Item {j} with <a href="#s{j}">link {j}</a></li>' for j in range(8))
	p.append(f"<h3>Sub heading</h3><ul>{items}</ul></section>")
	return "".join(p)


def table(rows: int) -> str:
	header = "".join(f'<th scope="col">Col {c}</th>' for c in range(6))
	p = [f'<table border="1"><caption>Data table</caption><thead><tr>{header}</tr></thead><tbody>']
	for r in range(rows):
		cells = "".join(f"<td>R{r}C{c}</td>" for c in range(1, 6))
		p.append(f'<tr><th scope="row">Row {r}</th>{cells}</tr>')
	p.append("</tbody></table>")
	return "".join(p)


def nav() -> str:
	links = "".join(f'<li><a href="#s{i}">Section {i}</a></li>' for i in range(10))
	return f'<header><nav aria-label="Main"><ul>{links}</ul></nav></header>'


def staticPage(kind: str) -> str:
	sections, rows, fields = SIZES[kind]
	p = [head(TITLES[kind]), nav(), f"<main><h1>{TITLES[kind]}</h1>", form(fields)]
	p.extend(section(s, sections) for s in range(sections))
	p.append(table(rows))
	p.append("</main><footer><p>End of page.</p></footer></body></html>")
	return "\n".join(p)


# Each step draws an operation and its target from a generator seeded with
# the page's seed, so a seed always gives the same sequence of DOM changes and
# different seeds give independent ones. A step whose target does not exist
# does nothing. Every operation leaves the status heading alone, since the
# bench reads the schedule's progress from it.
MUTATION_SCRIPT = r"""
const main = document.getElementById("main");
const status = document.getElementById("status");
let state = config.seed >>> 0;
function random() {
	state = (state + 0x6d2b79f5) >>> 0;
	let t = state;
	t = Math.imul(t ^ (t >>> 15), t | 1);
	t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
	return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}
const pick = (list) => list.length ? list[Math.floor(random() * list.length)] : null;
const all = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const sections = () => all("#main > section");
const inSections = (sel) => all("#main > section " + sel);
const firstText = (el) => el && Array.from(el.childNodes).find((n) => n.nodeType === 3 && n.length > 0);
const hidden = [];
let k = 0;
function newSection() {
	const s = document.createElement("section");
	s.id = "added" + k;
	s.setAttribute("aria-label", "Added section " + k);
	let h = "<h2>Added heading " + k + "</h2>";
	for (let i = 0; i < 3; i++) h += "<p>Added paragraph " + i + " <a href='#s1'>added link " + i + "</a></p>";
	h += "<ul>";
	for (let j = 0; j < 6; j++) h += "<li>Added item " + j + "</li>";
	s.innerHTML = h + "</ul>";
	return s;
}
function frameDoc(n) {
	return "<!doctype html><title>Frame " + n + "</title><h1>Frame heading " + n + "</h1><p>Frame text " + n + " <a href='#x'>frame link</a></p><ul><li>One</li><li>Two</li></ul>";
}
const ops = {
	insertSection() { main.insertBefore(newSection(), pick(sections())); },
	removeSection() { const s = pick(sections()); if (s) s.remove(); },
	hideSection() { const s = pick(sections().filter((x) => x.style.display !== "none")); if (s) { s.style.display = "none"; hidden.push(s); } },
	showSection() { const s = hidden.splice(Math.floor(random() * hidden.length), 1)[0]; if (s) s.style.display = ""; },
	replaceText() { const p = pick(inSections("p")); if (p) p.textContent = "Replaced paragraph text " + k; },
	appendText() { const t = firstText(pick(inSections("p"))); if (t) t.appendData(" appended " + k); },
	deleteText() { const t = firstText(pick(inSections("p"))); if (t && t.length > 8) t.deleteData(Math.floor(random() * (t.length - 8)), 8); },
	renameHeading() { const h = pick(inSections("h2")); if (h) h.textContent = "Renamed heading " + k; },
	insertItems() { const ul = pick(inSections("ul")); if (ul) for (let j = 0; j < 3; j++) { const li = document.createElement("li"); li.textContent = "Inserted item " + k + "." + j; ul.insertBefore(li, pick(Array.from(ul.children))); } },
	removeItem() { const li = pick(inSections("li")); if (li) li.remove(); },
	editField() { const i = pick(all("#f input[type=text]")); if (i) i.value = "edited " + k; },
	togglePressed() { const b = document.getElementById("toggle"); b.setAttribute("aria-pressed", b.getAttribute("aria-pressed") === "true" ? "false" : "true"); },
	reparent() { const node = pick(inSections("p, ul")); const to = pick(sections()); if (node && to && !node.contains(to)) to.insertBefore(node, pick(Array.from(to.children))); },
	reorderSections() { const a = pick(sections()), b = pick(sections()); if (a && b && a !== b) main.insertBefore(a, b); },
	toggleAriaHidden() { const el = pick(inSections("p").concat(sections())); if (el) { if (el.getAttribute("aria-hidden") === "true") el.removeAttribute("aria-hidden"); else el.setAttribute("aria-hidden", "true"); } },
	changeRole() { const p = pick(inSections("p")); const r = pick(["note", "group", "button", "heading", "list", ""]); if (p) { if (r) p.setAttribute("role", r); else p.removeAttribute("role"); } },
	changeAttribute() {
		const which = Math.floor(random() * 6);
		if (which === 0) { const h = pick(inSections("h2")); if (h) h.setAttribute("aria-level", String(2 + Math.floor(random() * 4))); }
		else if (which === 1) { const s = pick(sections()); if (s) s.setAttribute("aria-label", "Relabelled section " + k); }
		else if (which === 2) { const a = pick(inSections("a")); if (a) { if (a.hasAttribute("aria-current")) a.removeAttribute("aria-current"); else a.setAttribute("aria-current", "page"); } }
		else if (which === 3) { const p = pick(inSections("p")); if (p) p.setAttribute("title", "Title " + k); }
		else if (which === 4) { const b = document.getElementById("toggle"); b.setAttribute("aria-expanded", b.getAttribute("aria-expanded") === "true" ? "false" : "true"); }
		else { const p = pick(inSections("p")); if (p) p.setAttribute("aria-roledescription", "custom " + k); }
	},
	typeInEditor() {
		const ed = document.getElementById("editor");
		const which = Math.floor(random() * 4);
		if (which === 3) { const p = document.createElement("p"); p.textContent = "Typed line " + k; ed.insertBefore(p, pick(Array.from(ed.children))); return; }
		const t = firstText(pick(all("#editor p")));
		if (!t) return;
		if (which === 2 && t.length > 3) t.deleteData(Math.floor(random() * (t.length - 3)), 3);
		else t.insertData(Math.floor(random() * (t.length + 1)), "typed" + k + " ");
	},
	updateLive() { const l = document.getElementById("live"); if (random() < 0.5) l.textContent = "Live status " + k; else { const s = document.createElement("span"); s.textContent = " update " + k; l.appendChild(s); } },
	changeTable() {
		const body = document.querySelector("#main table tbody");
		const which = Math.floor(random() * 4);
		if (which === 0) { const tr = body.insertRow(Math.floor(random() * (body.rows.length + 1))); const th = document.createElement("th"); th.scope = "row"; th.textContent = "Added row " + k; tr.appendChild(th); for (let c = 1; c < 6; c++) tr.insertCell().textContent = "A" + k + "C" + c; }
		else if (which === 1) { if (body.rows.length > 1) body.deleteRow(Math.floor(random() * body.rows.length)); }
		else if (which === 2) { const td = pick(all("td", body)); if (td) td.textContent = "Changed " + k; }
		else { for (const tr of body.rows) tr.insertCell().textContent = "Col " + k; }
	},
	changeFrame() {
		const frames = all("#frames iframe");
		const which = Math.floor(random() * 3);
		if (which === 0) { const f = document.createElement("iframe"); f.title = "Frame " + k; f.srcdoc = frameDoc(k); document.getElementById("frames").insertBefore(f, pick(frames)); }
		else if (which === 1) { const f = pick(frames); if (f) f.remove(); }
		else { const f = pick(frames); try { const d = f && f.contentDocument; if (d && d.body) { const p = d.createElement("p"); p.textContent = "Frame update " + k; d.body.insertBefore(p, d.body.firstChild); } } catch (e) {} }
	},
	changeListbox() {
		const lb = document.getElementById("lb");
		const which = Math.floor(random() * 4);
		if (which === 0) { const o = document.createElement("div"); o.setAttribute("role", "option"); o.id = "opt-added" + k; o.textContent = "Added choice " + k; lb.insertBefore(o, pick(Array.from(lb.children))); }
		else if (which === 1) { const o = pick(Array.from(lb.children)); if (o && lb.children.length > 1) { if (lb.getAttribute("aria-activedescendant") === o.id) lb.removeAttribute("aria-activedescendant"); o.remove(); } }
		else if (which === 2) { const o = pick(Array.from(lb.children)); if (o) lb.setAttribute("aria-activedescendant", o.id); }
		else { const o = pick(Array.from(lb.children)); if (o) o.setAttribute("aria-selected", o.getAttribute("aria-selected") === "true" ? "false" : "true"); }
	},
	changeElementType() { const p = pick(inSections("p")); if (p) { const q = document.createElement(pick(["blockquote", "pre", "div", "h4"])); q.textContent = p.textContent; p.replaceWith(q); } },
	toggleDetails() { const d = pick(all("#main details")); if (d) d.open = !d.open; },
};
const names = Object.keys(ops);
window.addEventListener("load", () => setTimeout(() => {
	status.textContent = "Mutations running";
	const tick = () => {
		const name = pick(names);
		ops[name]();
		console.log("bench mutation step " + k + ": " + name);
		k++;
		if (k < config.steps) setTimeout(tick, config.intervalMs);
		else setTimeout(() => { status.textContent = "Mutations done"; }, config.intervalMs);
	};
	tick();
}, config.startMs));
"""


def fixtures() -> str:
	options = "".join(f'<div role="option" id="opt{i}">Choice {i}</div>' for i in range(8))
	frames = "".join(
		f'<iframe title="Frame start {i}" srcdoc="&lt;h1&gt;Frame start {i}&lt;/h1&gt;&lt;p&gt;Frame text&lt;/p&gt;"></iframe>'
		for i in range(2)
	)
	details = "".join(
		f"<details><summary>Details {i}</summary><p>Details body {i} with <a href='#s1'>a link</a>.</p></details>"
		for i in range(3)
	)
	return (
		'<div id="editor" contenteditable="true" role="textbox" aria-multiline="true" aria-label="Editor">'
		"<p>Editable first line</p><p>Editable second line</p></div>"
		'<div id="live" aria-live="polite">Live status</div>'
		f'<div role="listbox" id="lb" tabindex="0" aria-label="Choices" aria-activedescendant="opt0">{options}</div>'
		f'<div id="frames">{frames}</div>'
		f"{details}"
	)


def mutationPage(startMs: int, intervalMs: int, steps: int, seed: int) -> str:
	title = TITLES["mutation"]
	p = [head(title), nav(), '<main id="main"><h1 id="status">Mutations waiting</h1>', form(25)]
	p.append('<button type="button" id="toggle" aria-pressed="false">Toggle</button>')
	p.append(fixtures())
	p.extend(section(s, MUTATION_SECTIONS) for s in range(MUTATION_SECTIONS))
	p.append(table(10))
	p.append("</main><footer><p>End of page.</p></footer>")
	config = json.dumps({"startMs": startMs, "intervalMs": intervalMs, "steps": steps, "seed": seed})
	p.append(f"<script>const config = {config};{MUTATION_SCRIPT}</script></body></html>")
	return "\n".join(p)


def main(argv: list[str]) -> int:
	if len(argv) < 3 or argv[1] not in TITLES:
		print(__doc__.strip(), file=sys.stderr)
		return 2
	kind, out = argv[1], argv[2]
	if kind == "mutation":
		if len(argv) != 7:
			print(__doc__.strip(), file=sys.stderr)
			return 2
		html = mutationPage(int(argv[3]), int(argv[4]), int(argv[5]), int(argv[6]))
	else:
		html = staticPage(kind)
	with open(out, "w", encoding="utf-8") as fh:
		fh.write(html)
	print(TITLES[kind])
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv))
