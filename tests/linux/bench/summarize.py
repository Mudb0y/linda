"""Summarises benchmark result files: the median of each measure over the runs,
with the lowest and highest value as the spread.

usage: summarize.py DIR [DIR...]
"""

import glob
import json
import os
import statistics
import sys
from collections import defaultdict
from typing import Any

COST_KEYS = ("wall_s", "client_cpu_s", "app_cpu_s", "app_tree_cpu_s", "calls", "reply_bytes")
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
	"text_wrong",
)


def spread(values: list[float]) -> str:
	if not values:
		return "none"
	med = statistics.median(values)
	if all(float(v).is_integer() for v in values):
		return f"{med:.0f} ({min(values):.0f} to {max(values):.0f})"
	return f"{med:.3f} ({min(values):.3f} to {max(values):.3f})"


def loadResults(dirs: list[str]) -> list[dict[str, Any]]:
	out = []
	for d in dirs:
		for path in sorted(glob.glob(os.path.join(d, "*.json"))):
			with open(path, encoding="utf-8") as fh:
				out.append(json.load(fh))
	return out


def summarizeFetch(results: list[dict[str, Any]]) -> None:
	groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
	activation: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for r in results:
		activation[r["page"]].append(r["activation"])
		for m in r["methods"]:
			groups[(r["page"], m["method"], m["transport"])].append(m)
	for page, acts in sorted(activation.items()):
		print(
			f"{page} page, activation over {len(acts)} runs: "
			f"wall {spread([a['cost']['wall_s'] for a in acts])} s, "
			f"application CPU {spread([a['cost']['app_cpu_s'] for a in acts])} s, "
			f"first GetItems objects {spread([a['first_getitems_objects'] for a in acts])}."
		)
	for (page, method, transport), runs in sorted(groups.items()):
		failed = [m for m in runs if m["error"]]
		ok = [m for m in runs if not m["error"]]
		parts = [f"{page} page, {method} over {transport}, {len(ok)} runs"]
		if failed:
			parts.append(f"{len(failed)} failed with {failed[0]['error']!r}")
		for k in COST_KEYS:
			parts.append(f"{k} {spread([m['cost'][k] for m in ok])}")
		parts.append(f"objects {spread([m['objects'] for m in ok])}")
		same = [m["same_objects_as_reference"] for m in ok if m["same_objects_as_reference"]]
		if same:
			parts.append(f"same objects as the sequential walk in {sum(s['equal'] for s in same)} of {len(same)}")
		diffs = [m["differences_from_reference"] for m in ok if m["differences_from_reference"]]
		for k in DIFF_KEYS:
			total = sum(d[k] for d in diffs)
			if total:
				parts.append(f"{k} {spread([d[k] for d in diffs])}")
		print(", ".join(parts) + ".")


def summarizeMirror(results: list[dict[str, Any]]) -> None:
	groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
	for r in results:
		groups[r.get("coalesce_ms", 0)].append(r)
	for coalesce, runs in sorted(groups.items()):
		print(f"Mirror runs with signals coalesced for {coalesce} ms: {len(runs)} runs.")
		built = sum(r["build"]["status"] == "Mutations waiting" for r in runs)
		done = sum(r["reference_walk"]["status"] == "Mutations done" for r in runs)
		print(f"  Built before the first change in {built} runs; schedule finished in {done}.")
		print(
			f"  Build: objects {spread([r['build']['objects'] for r in runs])}, "
			f"wall {spread([r['build']['cost']['wall_s'] for r in runs])} s, "
			f"calls {spread([r['build']['cost']['calls'] for r in runs])}."
		)
		m = [r["maintenance"] for r in runs]
		print(
			f"  Maintenance: signals {spread([x['loop']['signals'] for x in m])}, "
			f"batches {spread([x['loop']['batches'] for x in m])}, "
			f"repairs run {spread([x['loop']['repairs_run'] for x in m])}, "
			f"repair calls {spread([x['cost']['calls'] for x in m])}, "
			f"objects fetched by repairs {spread([x['loop']['objects_fetched_by_repairs'] for x in m])}, "
			f"time in repairs {spread([x['loop']['repair_wall_s'] for x in m])} s, "
			f"client CPU {spread([x['cost']['client_cpu_s'] for x in m])} s."
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
			diffs = [r[key] for r in runs]
			parts = [f"{k} {spread([d[k] for d in diffs])}" for k in DIFF_KEYS if any(d[k] for d in diffs)]
			print(f"  {label}: " + (", ".join(parts) if parts else "no differences in any run") + ".")
			bits: dict[str, int] = defaultdict(int)
			for d in diffs:
				for b, n in d["state_bits_wrong"].items():
					bits[b] += n
			if bits:
				print("    State bits that differed, summed over runs: " + ", ".join(f"{b} {n}" for b, n in sorted(bits.items())) + ".")


def main(argv: list[str]) -> int:
	if len(argv) < 2:
		print(__doc__.strip(), file=sys.stderr)
		return 2
	results = loadResults(argv[1:])
	fetch = [r for r in results if r.get("schema", "").startswith("basedreader-bench/fetch/")]
	mirror = [r for r in results if r.get("schema", "").startswith("basedreader-bench/mirror/")]
	if fetch:
		summarizeFetch(fetch)
	if mirror:
		summarizeMirror(mirror)
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv))
