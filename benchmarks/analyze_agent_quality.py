"""Analysis for the agent-quality study (Arm 2): per-tier success with Wilson CIs,
non-inferiority vs the lossless control, steps-to-success, and the Arm-1 join point.

Input: a ``benchmarks/results/agent_quality_*.json`` produced by
``bench_agent_quality.py``. Only episodes with a verdict (``kind`` in pass/fail) enter
the denominators; infra/agent-error episodes are reported separately, never scored.

Statistics:

- **Wilson 95% score interval** per tier (robust at the small n of a pilot).
- **Non-inferiority vs control** (Newcombe score interval for the difference
  p_tier - p_control): a tier is declared non-inferior when the LOWER bound of the
  difference CI is within the -5 percentage-point margin (delta >= -0.05). This is the
  one-sided question the study asks — "can we serve this tier without losing task
  success?" — not a significance test of superiority.

Arm-1 join point: ``--legibility <file.json>`` merges per-tier legibility scores
produced by the sibling Arm-1 analysis. The join key is the canonical TIER LABEL
(``png-native``, ``q80-1024``, ``q50-1024``, ``q50-512``, ``q10-768`` — defined in
``bench_agent_quality.TIERS``). Accepted shapes: ``{tier: score}``,
``{tier: {...}}``, or a list of row dicts each carrying a ``tier`` key (extra fields
are carried through under ``legibility``). The file is OPTIONAL — absent, the joined
column is null and everything else still computes.

Run:

    python benchmarks/analyze_agent_quality.py results/agent_quality_pilot.json
    python benchmarks/analyze_agent_quality.py results/agent_quality_full.json \\
        --legibility results/legibility_scores.json --plot
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

#: Non-inferiority margin: the tier may cost at most 5 percentage points of success.
MARGIN = 0.05
Z = 1.96  # 95%


def wilson(k: int, n: int, z: float = Z) -> tuple[float | None, float | None, float | None]:
    """(point, lo, hi) Wilson score interval for k successes of n."""
    if n == 0:
        return None, None, None
    p = k / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, centre - half), min(1.0, centre + half)


def newcombe_diff(
    k1: int, n1: int, k0: int, n0: int, z: float = Z
) -> tuple[float | None, float | None, float | None]:
    """(delta, lo, hi) Newcombe score interval for p1 - p0 (tier minus control), built
    from the two Wilson intervals — well-behaved at boundary rates (0% / 100%)."""
    if n1 == 0 or n0 == 0:
        return None, None, None
    p1, l1, u1 = wilson(k1, n1, z)
    p0, l0, u0 = wilson(k0, n0, z)
    d = p1 - p0
    lo = d - math.sqrt((p1 - l1) ** 2 + (u0 - p0) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p0 - l0) ** 2)
    return d, lo, hi


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def load_legibility(source: str | dict | list | None) -> dict[str, object]:
    """Normalize the Arm-1 per-tier legibility input into ``{tier_label: payload}``.
    Accepts a path or an already-loaded object; tolerant of the three documented
    shapes; returns {} when the source is absent (the join is optional by design)."""
    if source is None:
        return {}
    if isinstance(source, str):
        path = Path(source)
        if not path.exists():
            print(f"note: legibility file {source} not found — joining null", file=sys.stderr)
            return {}
        source = json.loads(path.read_text())
    if isinstance(source, dict):
        # either {tier: score-ish} directly, or a wrapper like {"per_tier": {...}}
        for key in ("per_tier", "tiers", "legibility"):
            if key in source and isinstance(source[key], dict | list):
                return load_legibility(source[key])
        return {str(k): v for k, v in source.items()}
    if isinstance(source, list):
        return {str(r.get("tier")): r for r in source if isinstance(r, dict) and r.get("tier")}
    return {}


def analyze(results: dict, legibility: dict[str, object] | None = None) -> dict:
    episodes = results.get("episodes", [])
    tiers: list[str] = list(results.get("tiers", {}).keys()) or sorted(
        {e["tier"] for e in episodes}
    )
    if not tiers:
        raise SystemExit("no tiers in the results file")
    control = tiers[0]  # the harness writes the lossless control first
    legibility = legibility or {}

    verdicts = [e for e in episodes if e.get("kind") in ("pass", "fail")]
    excluded = [e for e in episodes if e.get("kind") not in ("pass", "fail")]

    def tier_rows(label: str) -> list[dict]:
        return [e for e in verdicts if e["tier"] == label]

    k0 = sum(1 for e in tier_rows(control) if e["success"])
    n0 = len(tier_rows(control))

    per_tier = []
    for label in tiers:
        rows = tier_rows(label)
        wins = [e for e in rows if e["success"]]
        k, n = len(wins), len(rows)
        p, lo, hi = wilson(k, n)
        steps = [e["steps"] for e in wins if e.get("steps") is not None]
        entry: dict = {
            "tier": label,
            "n": n,
            "successes": k,
            "success_rate": None if p is None else round(p, 4),
            "wilson_95": None if p is None else [round(lo, 4), round(hi, 4)],
            "steps_to_success_mean": round(sum(steps) / len(steps), 2) if steps else None,
            "steps_to_success_median": _median(steps),
            "legibility": legibility.get(label),
        }
        if label == control:
            entry["control"] = True
        else:
            d, dlo, dhi = newcombe_diff(k, n, k0, n0)
            entry["delta_vs_control"] = None if d is None else round(d, 4)
            entry["delta_ci_95"] = None if d is None else [round(dlo, 4), round(dhi, 4)]
            # Non-inferior iff the WORST plausible deficit stays inside the margin.
            entry["non_inferior_5pp"] = None if d is None else bool(dlo >= -MARGIN)
        per_tier.append(entry)

    return {
        "control": control,
        "margin_pp": MARGIN * 100,
        "agent": results.get("agent"),
        "mode": results.get("mode"),
        "n_episodes": len(episodes),
        "n_verdicts": len(verdicts),
        "excluded_no_verdict": len(excluded),
        "excluded_kinds": sorted({e.get("kind") for e in excluded}) if excluded else [],
        "per_tier": per_tier,
        "legibility_joined": bool(legibility),
    }


def _fmt(v, width: int = 7) -> str:
    if v is None:
        return "-".rjust(width)
    if isinstance(v, float):
        return f"{v:.3f}".rjust(width)
    return str(v).rjust(width)


def print_table(report: dict) -> None:
    agent = report.get("agent") or {}
    print(
        f"\nagent={agent.get('model') or agent.get('kind')}"
        f"  mode={report.get('mode')}  verdicts={report['n_verdicts']}/{report['n_episodes']}"
        f"  (control={report['control']}, non-inferiority margin -{report['margin_pp']:.0f}pp)"
    )
    head = (
        f"{'tier':<12}{'n':>4}{'pass':>6}{'rate':>8}{'wilson95':>18}{'d_ctrl':>8}"
        f"{'d_lo':>8}{'noninf':>8}{'steps':>7}{'legib':>7}"
    )
    print(head)
    print("-" * len(head))
    for row in report["per_tier"]:
        ci = row["wilson_95"]
        ci_s = f"[{ci[0]:.2f},{ci[1]:.2f}]" if ci else "-"
        dci = row.get("delta_ci_95")
        leg = row.get("legibility")
        if isinstance(leg, dict):
            leg = leg.get("score", leg.get("legibility"))
        print(
            f"{row['tier']:<12}{row['n']:>4}{row['successes']:>6}"
            f"{_fmt(row['success_rate'], 8)}{ci_s:>18}"
            f"{_fmt(row.get('delta_vs_control'), 8)}"
            f"{_fmt(dci[0] if dci else None, 8)}"
            f"{('ctrl' if row.get('control') else _fmt(row.get('non_inferior_5pp'), 6)):>8}"
            f"{_fmt(row.get('steps_to_success_mean'), 7)}{_fmt(leg, 7)}"
        )


def plot(report: dict, out_stem: str) -> None:
    """Optional figure (success rate + Wilson CI per tier); meaningful for the full
    study, noise for a pilot — hence opt-in."""
    from _common import PALETTE, new_axes, save_plot

    rows = [r for r in report["per_tier"] if r["n"]]
    fig, ax = new_axes(width=7.0)
    xs = range(len(rows))
    rates = [r["success_rate"] for r in rows]
    los = [r["success_rate"] - r["wilson_95"][0] for r in rows]
    his = [r["wilson_95"][1] - r["success_rate"] for r in rows]
    colors = [PALETTE["png"] if r.get("control") else PALETTE["jpeg"] for r in rows]
    ax.bar(xs, rates, yerr=[los, his], capsize=4, color=colors)
    ax.set_xticks(list(xs), [r["tier"] for r in rows], rotation=20)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("task success rate (Wilson 95% CI)")
    ax.set_title("Agent task success by observation codec tier")
    save_plot(fig, out_stem)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results", help="agent_quality_*.json (path, or name in results/)")
    ap.add_argument("--legibility", default=None, help="Arm-1 per-tier scores JSON (optional)")
    ap.add_argument("--out", default=None, help="write the analysis JSON here")
    ap.add_argument("--plot", action="store_true", help="emit the per-tier figure")
    args = ap.parse_args()

    path = Path(args.results)
    if not path.exists():
        path = Path(__file__).parent / "results" / args.results
    results = json.loads(path.read_text())
    report = analyze(results, load_legibility(args.legibility))
    print_table(report)

    out = Path(args.out) if args.out else path.with_name(path.stem + "_analysis.json")
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwrote {out}")
    if args.plot:
        plot(report, "agent_quality")
    return 0


if __name__ == "__main__":
    sys.exit(main())
