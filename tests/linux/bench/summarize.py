"""Summarises benchmark result files: the median of each measure over the runs,
with the lowest and highest value as the spread. Exits with status 1, after
naming each one, when any run has a failed call, a timeout, a difference
from the reference walk, or a mirror that does not match the walk for a
reason the signals could have shown.

usage: summarize.py DIR [DIR...]
"""

import glob
import json
import os
import statistics
import sys
from collections import defaultdict
from typing import Any

DIFF_KEYS = (
	"missing",
	"extra",
	"parent_wrong",
	"child_order_wrong",
	"index_wrong",
	"role_wrong",
	"name_wrong",
	"description_wrong",
	"states_wrong",
	"interfaces_wrong",
	"attributes_wrong",
	"text_wrong",
)

Result = dict[str, Any]


def spread(values: list[float], scale: float = 1.0, digits: int = 3) -> str:
	if not values:
		return "none"
	values = [v * scale for v in values]
	med = statistics.median(values)
	if all(float(v).is_integer() for v in values):
		return f"{med:.0f} ({min(values):.0f} to {max(values):.0f})"
	return f"{med:.{digits}f} ({min(values):.{digits}f} to {max(values):.{digits}f})"


def median(values: list[float]) -> float:
	return statistics.median(values) if values else float("nan")


def diffKinds(d: Result) -> dict[str, int]:
	"""Every kind of difference with a count, absent fields included."""
	kinds = {k: d[k] for k in DIFF_KEYS if d.get(k)}
	for field, n in d.get("absent", {}).items():
		if n:
			kinds[f"{field}_absent"] = n
	return kinds


def loadResults(dirs: list[str]) -> list[Result]:
	out = []
	for d in dirs:
		for path in sorted(glob.glob(os.path.join(d, "*.json"))):
			if path.endswith(".trees.json"):
				continue
			with open(path, encoding="utf-8") as fh:
				r = json.load(fh)
			r["_file"] = os.path.basename(path)
			out.append(r)
	return out


class Problems:
	def __init__(self) -> None:
		self.items: list[str] = []

	def add(self, where: str, what: str) -> None:
		self.items.append(f"{where}: {what}")

	def checkCost(self, where: str, cost: Result) -> None:
		if cost.get("errors"):
			self.add(where, f"{cost['errors']} failed calls")
		if cost.get("timeouts"):
			self.add(where, f"{cost['timeouts']} timed out calls")


def costLine(runs: list[Result]) -> str:
	c = [m["cost"] for m in runs]
	return (
		f"wall {spread([x['wall_s'] for x in c])} s, "
		f"Firefox main thread {spread([x['app_main_thread_cpu_s'] for x in c])} s, "
		f"Firefox process {spread([x['app_cpu_s'] for x in c])} s, "
		f"client {spread([x['client_cpu_s'] for x in c])} s, "
		f"calls {spread([x['calls'] for x in c])}, "
		f"reply bytes {spread([x['reply_bytes'] for x in c])}"
	)


def summarizeLoad(page: str, runs: list[Result], problems: Problems) -> None:
	byMode: dict[str, list[Result]] = defaultdict(list)
	for r in runs:
		byMode[r["load"]["wait_mode"]].append(r)
	for mode, rs in sorted(byMode.items()):
		loads = [r["load"] for r in rs]
		label = (
			"cold, nothing in the page touched before activation"
			if mode == "cold"
			else "warm, every object created by walks before activation"
		)
		print(f"{page} page, first load, {label}, {len(rs)} runs:")
		print(f"  Activation: {costLine([{'cost': x['activation']} for x in loads])}.")
		print(f"  Peer-to-peer connect: {spread([x['p2p_connect_s'] for x in loads if x['p2p_connect_s'] is not None], 1000)} ms.")
		print(
			f"  First GetItems over {loads[0]['first_getitems_transport']}: "
			f"{costLine([{'cost': x['first_getitems']} for x in loads])}, objects {spread([x['first_getitems_objects'] for x in loads])}."
		)
		print(f"  Activation through first GetItems: {costLine([{'cost': x['total']} for x in loads])}.")
		for r in rs:
			where = f"{r['_file']} first load"
			for k in ("activation", "first_getitems", "total"):
				problems.checkCost(where, r["load"][k])
			vs = r["load"].get("first_getitems_vs_reference")
			if vs is not None and not vs["equal"]:
				problems.add(where, f"first GetItems is not the walk's object set: {vs}")
			d = r["load"].get("first_getitems_differences")
			if d and diffKinds(d):
				problems.add(where, f"first GetItems differs from the walk: {diffKinds(d)}")
			if mode == "cold" and r["wait"].get("quiet") is False:
				problems.add(where, "the page never went quiet before activation")


def summarizeFetch(results: list[Result], problems: Problems) -> None:
	pages: dict[str, list[Result]] = defaultdict(list)
	for r in results:
		pages[r["page"]].append(r)
	for page, runs in sorted(pages.items()):
		summarizeLoad(page, runs, problems)
		full = [r for r in runs if r["methods"]]
		if not full:
			continue
		print(
			f"{page} page objects: {spread([r['application_objects'] for r in full])} in the whole application, "
			f"{spread([r['document_objects'] for r in full])} of them in the document."
		)
		groups: dict[tuple[str, str], list[Result]] = defaultdict(list)
		warm: dict[str, list[Result]] = defaultdict(list)
		for r in full:
			for m in r["methods"]:
				groups[(m["method"], m["transport"])].append(m)
			for m in r["warmup"]:
				warm[m["method"]].append(m)
		for method, ws in sorted(warm.items()):
			print(f"  Untimed warm-up {method}, the first after the first load: {costLine(ws)}.")
		for (method, transport), ms in sorted(groups.items()):
			ok = [m for m in ms if not m["error"]]
			for m in ms:
				if m["error"]:
					problems.add(f"{page} {method} over {transport}", m["error"])
			if not ok:
				continue
			same = [m["same_objects_as_reference"] for m in ok if m["same_objects_as_reference"]]
			line = f"  {method} over {transport}, {len(ok)} runs: {costLine(ok)}"
			if same:
				line += f", same objects as the sequential walk in {sum(s['equal'] for s in same)} of {len(same)}"
			print(line + ".")
			for r in full:
				for m in r["methods"]:
					if m["method"] != method or m["transport"] != transport:
						continue
					where = f"{r['_file']} {method} over {transport}"
					problems.checkCost(where, m["cost"])
					if m["call_errors"]:
						problems.add(where, f"call errors {m['call_errors']}")
					s = m["same_objects_as_reference"]
					if s is not None and not s["equal"]:
						problems.add(where, f"not the walk's object set: {s}")
					d = m["differences_from_reference"]
					if d and diffKinds(d):
						problems.add(where, f"differs from the walk: {diffKinds(d)}")
		derived(page, groups)


def med(groups: dict[tuple[str, str], list[Result]], method: str, transport: str, key: str) -> float:
	return median([m["cost"][key] for m in groups.get((method, transport), []) if not m["error"]])


def derived(page: str, groups: dict[tuple[str, str], list[Result]]) -> None:
	p2p, bus = "peer-to-peer", "bus"
	print(f"  {page} page, compared with equal data, medians:")
	for transport in (p2p, bus):
		for a, b, what in (
			("getitems+attrs+text-16", "walk-16", "every field, attributes and text included"),
			("getitems", "walk-itemfields-16", "only what GetItems returns"),
		):
			wa, wb = med(groups, a, transport, "wall_s"), med(groups, b, transport, "wall_s")
			ca, cb = med(groups, a, transport, "app_main_thread_cpu_s"), med(groups, b, transport, "app_main_thread_cpu_s")
			if wa == wa and wb == wb:
				print(
					f"    {what}, over {transport}: {a} took {wa:.3f} s of wall time against {wb:.3f} s for {b}, "
					f"{wb / wa:.1f} times less; Firefox main thread {ca:.3f} s against {cb:.3f} s, {cb / ca:.1f} times less."
				)
	for transport in (p2p, bus):
		windows = sorted(
			int(m.split("-")[1]) for (m, t) in groups if t == transport and m.startswith("walk-") and m.split("-")[1].isdigit()
		)
		parts = [f"{w}: {med(groups, f'walk-{w}', transport, 'wall_s'):.2f} s" for w in windows]
		print(f"    Full walk by calls in flight over {transport}: " + ", ".join(parts) + ".")
	for transport in (p2p, bus):
		calls = med(groups, "walk-1", transport, "calls")
		if calls == calls and calls:
			print(
				f"    Sequential walk over {transport}, per call: wall {med(groups, 'walk-1', transport, 'wall_s') / calls * 1e6:.0f} microseconds, "
				f"a property of this client; Firefox main thread {med(groups, 'walk-1', transport, 'app_main_thread_cpu_s') / calls * 1e6:.0f}; "
				f"client {med(groups, 'walk-1', transport, 'client_cpu_s') / calls * 1e6:.0f}."
			)
		calls = med(groups, "walk-64", transport, "calls")
		if calls == calls and calls:
			print(
				f"    Walk with 64 in flight over {transport}, per call: Firefox main thread "
				f"{med(groups, 'walk-64', transport, 'app_main_thread_cpu_s') / calls * 1e6:.0f} microseconds."
			)
	for (method, transport), ms in sorted(groups.items()):
		if not method.startswith("latency-"):
			continue
		probes = [m["details"]["probe"] for m in ms if not m["error"]]

		def q(which: str, key: str) -> str:
			return spread([p[which][key] for p in probes if p[which][key] is not None])

		print(
			f"    One interactive call during a full walk with {method.split('-')[1]} in flight, in ms: "
			f"on the walk's connection median {q('loaded_same_connection', 'median_ms')}, 99th percentile {q('loaded_same_connection', 'p99_ms')}; "
			f"on a second connection median {q('loaded_second_connection', 'median_ms')}, 99th percentile {q('loaded_second_connection', 'p99_ms')}; "
			f"idle median {q('idle_same_connection', 'median_ms')}."
		)


def summarizeMirror(results: list[Result], problems: Problems) -> None:
	groups: dict[tuple[int, int], list[Result]] = defaultdict(list)
	for r in results:
		groups[(r.get("interval_ms", 0), r.get("coalesce_ms", 0))].append(r)
	for (interval, coalesce), runs in sorted(groups.items()):
		seeds = sorted(r.get("seed", 0) for r in runs)
		print(
			f"Mirror runs with {interval} ms between changes and signals coalesced for {coalesce} ms: "
			f"{len(runs)} runs, seeds {', '.join(map(str, seeds))}, each its own random schedule."
		)
		built = sum(r["build"]["status"] == "Mutations waiting" for r in runs)
		done = sum(r["reference_walk"]["status"] == "Mutations done" for r in runs)
		print(f"  Built before the first change in {built} runs; schedule finished in {done}.")
		print(
			f"  Build: objects {spread([r['build']['objects'] for r in runs])}, "
			f"wall {spread([r['build']['cost']['wall_s'] for r in runs])} s, "
			f"calls {spread([r['build']['cost']['calls'] for r in runs])}, "
			f"calls to prime attributes {spread([r['build']['attribute_priming_calls'] for r in runs])}."
		)
		m = [r["maintenance"] for r in runs]
		print(
			f"  Maintenance: signals {spread([x['loop']['signals'] for x in m])}, "
			f"batches {spread([x['loop']['batches'] for x in m])}, "
			f"repairs run {spread([x['loop']['repairs_run'] for x in m])}, "
			f"repair calls {spread([x['cost']['calls'] for x in m])}, "
			f"objects fetched by repairs {spread([x['loop']['objects_fetched_by_repairs'] for x in m])}, "
			f"time in repairs {spread([x['loop']['repair_wall_s'] for x in m])} s, "
			f"Firefox main thread {spread([x['cost']['app_main_thread_cpu_s'] for x in m])} s, "
			f"client CPU {spread([x['cost']['client_cpu_s'] for x in m])} s."
		)
		errs: dict[str, int] = defaultdict(int)
		for x in m:
			for k, n in x["loop"]["repair_call_errors"].items():
				errs[k] += n
		print(
			"  Repair calls that failed, summed over runs, mostly objects gone before their repair: "
			+ (", ".join(f"{k} {n}" for k, n in sorted(errs.items())) if errs else "none")
			+ "."
		)
		ruleKeys = sorted({k for x in m for k, v in x["rules"].items() if isinstance(v, int)})
		rules = [f"{k} {spread([x['rules'][k] for x in m])}" for k in ruleKeys if any(x["rules"][k] for x in m)]
		print("  Rules applied: " + ", ".join(rules) + ".")
		for key, label in (
			("mirror_vs_walk", "Mirror against a fresh walk"),
			("cache_signals_only_vs_walk", "Snapshot patched from Cache signals only"),
			("fresh_getitems_vs_walk", "Fresh GetItems"),
			("fresh_getitems_pruned_vs_walk", "Fresh GetItems pruned to what is reachable"),
		):
			diffs = [diffKinds(r[key]) for r in runs]
			kinds = sorted({k for d in diffs for k in d})
			parts = [f"{k} {spread([d.get(k, 0) for d in diffs])}" for k in kinds]
			clean = sum(not d for d in diffs)
			print(
				f"  {label}: "
				+ (", ".join(parts) + f"; no differences in {clean} of {len(runs)} runs" if parts else "no differences in any run")
				+ "."
			)
		without: dict[str, int] = defaultdict(int)
		for r in runs:
			for k, n in r["mirror_attribute_drift"]["without_signal"].items():
				without[k] += n
		print(
			"  Attributes that changed with no signal from the object or its parent, which no mirror fed by "
			"signals can follow, summed over runs: "
			+ (", ".join(f"{k} on {n} objects" for k, n in sorted(without.items())) if without else "none")
			+ "."
		)
		for r in runs:
			where = f"{r['_file']} mirror"
			if r["mirror_differences_unexplained"]:
				problems.add(
					where,
					f"{r['mirror_differences_unexplained']} differences from the walk the signals could have shown: "
					f"{diffKinds(r['mirror_vs_walk'])}",
				)
			loop = r["maintenance"]["loop"]
			if loop["repair_timeouts"]:
				problems.add(where, f"{loop['repair_timeouts']} repair calls timed out")
			if loop["repair_undecodable_replies"] or loop["undecodable_signals"]:
				problems.add(where, "undecodable replies or signals")
			if r["build"]["call_errors"]:
				problems.add(where, f"build call errors {r['build']['call_errors']}")
			if r["reference_walk"]["call_errors"]:
				problems.add(where, f"reference walk call errors {r['reference_walk']['call_errors']}")
			problems.checkCost(f"{where} build", r["build"]["cost"])
			problems.checkCost(f"{where} reference walk", r["reference_walk"]["cost"])
			if r["build"]["status"] != "Mutations waiting":
				problems.add(where, "the mirror was built after the changes began")
			if r["reference_walk"]["status"] != "Mutations done":
				problems.add(where, "the schedule had not finished")


def main(argv: list[str]) -> int:
	if len(argv) < 2:
		print(__doc__.strip(), file=sys.stderr)
		return 2
	results = loadResults(argv[1:])
	problems = Problems()
	fetch = [r for r in results if r.get("schema", "") == "basedreader-bench/fetch/2"]
	mirror = [r for r in results if r.get("schema", "") == "basedreader-bench/mirror/2"]
	old = len(results) - len(fetch) - len(mirror)
	if old:
		problems.add("input", f"{old} files from another version of the bench")
	if fetch:
		summarizeFetch(fetch, problems)
	if mirror:
		summarizeMirror(mirror, problems)
	if problems.items:
		print(f"summarize: {len(problems.items)} problems", file=sys.stderr)
		for p in problems.items:
			print(f"  {p}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv))
