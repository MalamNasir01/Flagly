"""CLI helpers for state format onboarding."""

from __future__ import annotations

import argparse
import json
import os
import sys


def _cmd_generate_mandates(args: argparse.Namespace) -> int:
    from engines.state_formats import parse_state_pdf
    from engines.state_formats.mandates_draft import build_draft_mandates, write_draft_mandates
    from engines.state_formats.registry import load_profile

    profile = load_profile(args.profile)
    contents = open(args.pdf, "rb").read()
    df, meta = parse_state_pdf(contents, args.profile)
    rows = df.to_dict("records")
    doc = build_draft_mandates(
        rows,
        jurisdiction=profile.get("jurisdiction") or args.profile,
        profile_id=profile.get("id") or args.profile,
        source_row_count=len(df),
    )
    path = write_draft_mandates(doc, args.out)
    print(f"wrote {path} mdas={doc['_meta']['mda_count']} reviewed=false")
    print(f"parse rows={len(df)} suspects={meta.get('parse_quality', {}).get('suspect_rows')}")
    return 0


def _cmd_snapshot_baseline(args: argparse.Namespace) -> int:
    from collections import Counter

    from engines.classifier import reload_classifier_data
    import engines.flags as flags
    from engines.flags import run_all_flags
    from engines.scorer import score_items
    from engines.state_formats import parse_state_pdf
    from engines.state_formats.mandates_draft import mandates_reviewed
    from engines.state_formats.parser import description_merge_issues
    from engines.state_formats.registry import load_profile, mandates_path_for_jurisdiction
    from engines.state_formats.trust_gate import baseline_path_for_profile, evaluate_trust_gate

    reload_classifier_data()
    flags.STATE_MANDATE_BUNDLES = flags._load_state_mandates_bundle()
    flags.STATE_MDA_MANDATES = (flags.STATE_MANDATE_BUNDLES.get("niger_state") or {}).get(
        "mandates"
    ) or {}

    profile = load_profile(args.profile)
    contents = open(args.pdf, "rb").read()
    df, meta = parse_state_pdf(contents, args.profile)
    jurisdiction = profile.get("jurisdiction") or args.profile
    # Prefer state_<slug> for run_all_flags
    j_run = args.profile if str(args.profile).startswith("state_") else f"state_{args.profile}"

    scored = score_items(run_all_flags(df, budget_year=args.year, jurisdiction=j_run))
    types = Counter()
    mandate = Counter()
    for r in scored:
        for f in r.get("flags") or []:
            types[f["flag_type"]] += 1
            if f["flag_type"] == "MANDATE_MISMATCH":
                mandate[(f.get("severity") or "").upper()] += 1

    top = df.sort_values("amount", ascending=False, na_position="last").head(10)
    top10 = []
    for i, (_, r) in enumerate(top.iterrows(), 1):
        amt = r.amount
        top10.append(
            {
                "rank": i,
                "description": str(r.description),
                "amount": None if amt != amt else float(amt),
                "mda": None if r.mda_name != r.mda_name else str(r.mda_name),
                "location": None if r.location != r.location else str(r.location),
                "merge_issues": description_merge_issues(str(r.description)),
            }
        )

    mpath = mandates_path_for_jurisdiction(jurisdiction)
    reviewed = mandates_reviewed(mpath) if mpath else False
    trust = evaluate_trust_gate(
        profile_id=args.profile,
        parse_meta=meta,
        mandates_reviewed=reviewed,
    )

    baseline = {
        "jurisdiction": jurisdiction,
        "profile_id": profile.get("id") or args.profile,
        "note": args.note
        or (
            "State format baseline. Re-snapshot after parser/profile changes. "
            "Federal baseline must stay green."
        ),
        "total_items": int(len(df)),
        "amount_non_null_pct": round(float(df["amount"].notna().mean()) * 100, 2),
        "flagged_items": len(scored),
        "mandate_high": int(mandate.get("HIGH", 0)),
        "mandate_medium": int(mandate.get("MEDIUM", 0)),
        "flag_counts": dict(types),
        "parse_quality": meta.get("parse_quality") or {},
        "mandates_reviewed": reviewed,
        "trust_gate": trust,
        "top10_by_amount": top10,
        "columns_detected": meta.get("columns_detected"),
        "amount_column_used": meta.get("amount_column_used"),
    }
    out = args.out or baseline_path_for_profile(args.profile)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, allow_nan=False)
        f.write("\n")
    print(f"wrote {out}")
    print(
        f"items={baseline['total_items']} flagged={baseline['flagged_items']} "
        f"suspects={baseline['parse_quality'].get('suspect_rows')} "
        f"mandate_H/M={baseline['mandate_high']}/{baseline['mandate_medium']} "
        f"reviewed={reviewed} provisional={trust['provisional']}"
    )
    for t in top10:
        print(f"  {t['rank']}. {t['amount']} | {t['description'][:90]}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="state_formats.tools")
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate_mandates", help="Draft mda_mandates_states_<state>.json")
    g.add_argument("--profile", required=True)
    g.add_argument("--pdf", required=True)
    g.add_argument("--out", default=None)
    g.set_defaults(func=_cmd_generate_mandates)

    s = sub.add_parser("snapshot_baseline", help="Write samples/<profile>_baseline.json")
    s.add_argument("--profile", required=True)
    s.add_argument("--pdf", required=True)
    s.add_argument("--year", default="2025")
    s.add_argument("--out", default=None)
    s.add_argument("--note", default=None)
    s.set_defaults(func=_cmd_snapshot_baseline)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
