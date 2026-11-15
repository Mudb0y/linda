"""Writes the benchmark's web pages. The same arguments always give the same page.

usage: makePage.py normal|large OUT.html
       makePage.py mutation OUT.html START_MS INTERVAL_MS STEPS

The normal page comes to about 4,100 accessible objects in Firefox 160 and the
large one to about 39,500. The mutation page changes itself on a fixed
schedule: STEPS changes, INTERVAL_MS apart, the first START_MS after load.
"""

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


# Each step applies the next operation in this list to targets chosen from the
# step number, so a run is the same sequence of DOM changes every time.
MUTATION_SCRIPT = """
const startMs = %(start)d, intervalMs = %(interval)d, steps = %(steps)d, sections = %(sections)d;
const main = document.getElementById("main");
const status = document.getElementById("status");
const hidden = [];
const sec = (k) => document.getElementById("s" + (k %% sections));
function newSection(k) {
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
const ops = [
	(k) => main.insertBefore(newSection(k), sec(k) || null),
	(k) => { const s = sec(k * 7 + 3); if (s) s.remove(); },
	(k) => { const s = sec(k * 5 + 11); if (s && s.style.display !== "none") { s.style.display = "none"; hidden.push(s); } },
	(k) => { const s = hidden.shift(); if (s) s.style.display = ""; },
	(k) => { const s = sec(k * 3 + 1); const p = s && s.querySelector("p"); if (p) p.textContent = "Replaced paragraph text " + k; },
	(k) => { const s = sec(k * 3 + 2); const p = s && s.querySelector("p"); if (p && p.firstChild && p.firstChild.nodeType === 3) p.firstChild.appendData(" appended " + k); },
	(k) => { const s = sec(k * 11 + 5); const h = s && s.querySelector("h2"); if (h) h.textContent = "Renamed heading " + k; },
	(k) => { const s = sec(k * 13 + 7); const ul = s && s.querySelector("ul"); if (ul) for (let j = 0; j < 3; j++) { const li = document.createElement("li"); li.textContent = "Inserted item " + k + "." + j; ul.appendChild(li); } },
	(k) => { const s = sec(k * 17 + 9); const li = s && s.querySelector("li"); if (li) li.remove(); },
	(k) => { const i = document.getElementById("t" + (k %% 5) * 5); if (i) i.value = "edited " + k; },
	(k) => { const b = document.getElementById("toggle"); b.setAttribute("aria-pressed", b.getAttribute("aria-pressed") === "true" ? "false" : "true"); },
	(k) => { const s = sec(k * 19 + 4); const p = s && s.querySelector("p"); if (p && p.firstChild && p.firstChild.nodeType === 3 && p.firstChild.length > 8) p.firstChild.deleteData(0, 8); },
];
window.addEventListener("load", () => setTimeout(() => {
	status.textContent = "Mutations running";
	let k = 0;
	const tick = () => {
		ops[k %% ops.length](k);
		k++;
		if (k < steps) setTimeout(tick, intervalMs);
		else setTimeout(() => { status.textContent = "Mutations done"; }, intervalMs);
	};
	tick();
}, startMs));
"""


def mutationPage(startMs: int, intervalMs: int, steps: int) -> str:
	title = TITLES["mutation"]
	p = [head(title), nav(), '<main id="main"><h1 id="status">Mutations waiting</h1>', form(25)]
	p.append('<button type="button" id="toggle" aria-pressed="false">Toggle</button>')
	p.extend(section(s, MUTATION_SECTIONS) for s in range(MUTATION_SECTIONS))
	p.append(table(10))
	p.append("</main><footer><p>End of page.</p></footer>")
	script = MUTATION_SCRIPT % {
		"start": startMs,
		"interval": intervalMs,
		"steps": steps,
		"sections": MUTATION_SECTIONS,
	}
	p.append(f"<script>{script}</script></body></html>")
	return "\n".join(p)


def main(argv: list[str]) -> int:
	if len(argv) < 3 or argv[1] not in TITLES:
		print(__doc__.strip(), file=sys.stderr)
		return 2
	kind, out = argv[1], argv[2]
	if kind == "mutation":
		if len(argv) != 6:
			print(__doc__.strip(), file=sys.stderr)
			return 2
		html = mutationPage(int(argv[3]), int(argv[4]), int(argv[5]))
	else:
		html = staticPage(kind)
	with open(out, "w", encoding="utf-8") as fh:
		fh.write(html)
	print(TITLES[kind])
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv))
