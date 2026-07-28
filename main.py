import os
import math
import json
import re
import traceback
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
import pandas as pd

from engines.parser import parse_file
from engines.format_detect import UnsupportedBudgetFormat, FORMAT_STATE_NIGER, supported_format_catalog
from engines.state_formats.trust_gate import evaluate_trust_gate, apply_severity_cap
from engines.state_formats.registry import load_profile
from engines.flags import get_last_run_stats, ACTIVE_MANDATES_REVIEWED
import engines.flags as flags_mod
from engines.flags import run_all_flags, flag_ghost_projects_multiyear, get_last_run_stats
from engines.scorer import score_items
from engines.query import generate_narratives, answer_question, visuals_payload, safe_float

app = FastAPI(title="Flagly — Nigerian Budget Red Flag Scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")


def sanitize(obj):
    """Recursively replace NaN/Inf floats and numpy types with JSON-safe values."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    try:
        import numpy as np
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            if math.isnan(float(obj)) or math.isinf(float(obj)):
                return None
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
    except ImportError:
        pass
    return obj


def json_response(data: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=sanitize(data), status_code=status_code)


EMPTY_FLAG_SUMMARY = {
    "duplicate_clusters":  0,
    "inflated_amounts":    0,
    "context_mismatch":    0,
    "missing_location":    0,
    "ghost_projects":      0,
    "vague_location":      0,
    "budget_splitting":    0,
    "mandate_mismatch":    0,
    "mandate_mismatch_high": 0,
    "mandate_mismatch_medium": 0,
    "overhead_dominance":  0,
    "composite_duplicate": 0,
    "inflated_projection": 0,
    "phantom_spending":    0,
    "vague_high_value":    0,
    "zero_rollover":       0,
    "blank_approved_amount": 0,
}

_FLAG_MAP = {
    "DUPLICATE_CLUSTER":    "duplicate_clusters",
    "INFLATED_AMOUNT":      "inflated_amounts",
    "CONTEXT_MISMATCH":     "context_mismatch",
    "MISSING_LOCATION":     "missing_location",
    "GHOST_PROJECT":        "ghost_projects",
    "VAGUE_LOCATION":       "vague_location",
    "BUDGET_SPLITTING":     "budget_splitting",
    "MANDATE_MISMATCH":     "mandate_mismatch",
    "OVERHEAD_DOMINANCE":   "overhead_dominance",
    "COMPOSITE_DUPLICATE":  "composite_duplicate",
    "INFLATED_PROJECTION":  "inflated_projection",
    "PHANTOM_SPENDING":     "phantom_spending",
    "VAGUE_HIGH_VALUE_SPEND": "vague_high_value",
    "ZERO_ROLLOVER":        "zero_rollover",
    "BLANK_APPROVED_AMOUNT": "blank_approved_amount",
}


def build_flag_summary(results: List[Dict]) -> Dict[str, int]:
    flag_summary = dict(EMPTY_FLAG_SUMMARY)
    for r in results:
        seen_types = set()
        for f in r.get("flags", []):
            ft = f.get("flag_type", "")
            if ft in seen_types:
                continue
            seen_types.add(ft)
            key = _FLAG_MAP.get(ft)
            if key:
                flag_summary[key] += 1
            if ft == "MANDATE_MISMATCH":
                sev = (f.get("severity") or "").upper()
                kind = ((f.get("evidence") or {}).get("mismatch_kind") or "").lower()
                if sev == "HIGH" or kind == "excluded":
                    flag_summary["mandate_mismatch_high"] += 1
                else:
                    flag_summary["mandate_mismatch_medium"] += 1
    return flag_summary


def summarize_scan(df: pd.DataFrame, results: List[Dict]) -> Dict[str, Any]:
    total_items = len(df)
    total_amount = safe_float(df["amount"].sum()) if "amount" in df.columns else 0.0
    high_risk = sum(1 for r in results if r.get("risk_level") == "HIGH")
    medium_risk = sum(1 for r in results if r.get("risk_level") == "MEDIUM")
    low_risk = sum(1 for r in results if r.get("risk_level") == "LOW")
    at_risk_amount = sum(safe_float(r.get("amount") or 0) for r in results)
    stats = get_last_run_stats()
    shortlist = [r for r in results if r.get("on_shortlist")]
    from engines.classifier import get_flag_config
    catch_all = (get_flag_config().get("catch_all_mda_patterns") or [])
    return {
        "total_items": total_items,
        "flagged_items": len(results),
        "high_risk": high_risk,
        "medium_risk": medium_risk,
        "low_risk": low_risk,
        "at_risk_amount": at_risk_amount,
        "total_amount": total_amount,
        "flag_summary": build_flag_summary(results),
        "results": results,
        "shortlist": shortlist,
        "unclassified_count": stats.get("unclassified_count", 0),
        "flag_rate": (len(results) / total_items) if total_items else 0.0,
        "catch_all_mda_patterns": catch_all,
    }


def _jurisdiction_for_format(budget_format: str) -> str:
    if not budget_format or budget_format == "federal_fgn":
        return "federal"
    if budget_format.startswith("state_"):
        return budget_format
    try:
        profile = load_profile(budget_format)
        j = profile.get("jurisdiction") or budget_format
        if j and not str(j).startswith("state"):
            # kaduna_state → keep; also accept state_kaduna from profile id
            return str(j)
        return str(j)
    except Exception:
        return budget_format


def process_single(contents: bytes, filename: str, budget_year: str) -> Dict[str, Any]:
    try:
        df = parse_file(contents, filename)
    except UnsupportedBudgetFormat as e:
        # Structured refusal — never emit an n/a-filled report
        raise ValueError(str(e)) from e
    if df is None or df.empty:
        raise ValueError("Could not extract any data from the uploaded file. Please check the format.")

    budget_format = df.attrs.get("budget_format") or "federal_fgn"
    parse_meta = df.attrs.get("parse_meta") or {}
    jurisdiction = _jurisdiction_for_format(budget_format)

    flagged_rows = run_all_flags(df, budget_year=budget_year, jurisdiction=jurisdiction)
    scored = score_items(flagged_rows) if flagged_rows else []

    # Trust gate for state formats
    trust = {
        "publishable": True,
        "provisional": False,
        "confidence": "high",
        "has_baseline": True,
        "parse_quality_clean": True,
        "mandates_reviewed": True,
        "max_severity": "HIGH",
        "warnings": [],
    }
    if str(jurisdiction).startswith("state") or str(budget_format).startswith("state_"):
        mandates_reviewed = bool(flags_mod.ACTIVE_MANDATES_REVIEWED)
        trust = evaluate_trust_gate(
            profile_id=budget_format,
            parse_meta=parse_meta,
            mandates_reviewed=mandates_reviewed,
        )
        scored = apply_severity_cap(scored, trust["max_severity"])

    out = summarize_scan(df, scored)
    out["budget_year"] = budget_year
    out["filename"] = filename
    out["budget_format"] = budget_format
    out["parse_meta"] = parse_meta
    out["jurisdiction"] = jurisdiction
    out["parse_only"] = False
    out["trust_gate"] = trust
    out["mandates_reviewed"] = trust.get("mandates_reviewed", True)
    out["provisional"] = trust.get("provisional", False)
    out["narratives"] = generate_narratives(scored)
    out["visuals"] = visuals_payload(scored)
    out["multi_year"] = False
    out["ghost_enabled"] = False
    out["null_approved_amount_count"] = int(df["amount"].isna().sum()) if "amount" in df.columns else 0
    stats = get_last_run_stats()
    out["blank_approved_amount"] = stats.get("blank_approved_amount") or {
        "total": 0, "low": 0, "medium": 0, "informational_only_excluded": 0,
    }
    out["flags_not_checked"] = stats.get("flags_not_checked") or []
    out["supported_formats"] = supported_format_catalog()
    return out


@app.get("/health")
async def health():
    return JSONResponse(
        content={
            "status": "ok",
            "supported_formats": supported_format_catalog(),
        }
    )


@app.post("/format-interest")
async def format_interest(
    state: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    filename: Optional[str] = Form(None),
):
    """Capture demand signal when a user wanted an unsupported state format."""
    payload = {
        "state": (state or "").strip() or None,
        "note": (note or "").strip() or None,
        "filename": (filename or "").strip() or None,
    }
    print(f"[format-interest] {payload}")
    return JSONResponse(
        content={
            "ok": True,
            "message": (
                "Thanks — we logged your request. Flagly currently supports "
                + ", ".join(c["label"] for c in supported_format_catalog())
                + "."
            ),
            "supported_formats": supported_format_catalog(),
        }
    )


@app.get("/")
async def root():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)


@app.get("/favicon.ico")
async def favicon():
    from fastapi.responses import FileResponse
    return FileResponse("frontend/assets/favicon.ico", media_type="image/x-icon")

@app.post("/scan")
async def scan(
    file: UploadFile = File(None),
    files: List[UploadFile] = File(None),
    budget_year: str = Form(None),
    budget_years: Optional[str] = Form(None),
    ministry: Optional[str] = Form(None),
):
    """Scan one or more budget files.

    Backward compatible: single `file` + `budget_year` still works.
    Multi year: send repeated `files` plus `budget_years` as JSON array of years.
    """
    try:
        upload_list: List[UploadFile] = []
        if files:
            upload_list = [f for f in files if f is not None and f.filename]
        if file is not None and file.filename:
            if not upload_list:
                upload_list = [file]

        if not upload_list:
            return json_response({"error": "No file uploaded."}, status_code=400)

        years: List[str] = []
        if budget_years:
            try:
                years = json.loads(budget_years)
            except Exception:
                years = [y.strip() for y in budget_years.split(",") if y.strip()]
        if not years and budget_year:
            years = [budget_year] * len(upload_list)
        if len(years) < len(upload_list):
            years = years + [years[-1] if years else "2026"] * (len(upload_list) - len(years))

        if len(upload_list) == 1:
            contents = await upload_list[0].read()
            file_size_mb = len(contents) / (1024 * 1024)
            if file_size_mb > 50:
                raise HTTPException(
                    status_code=400,
                    detail=f"File too large ({file_size_mb:.1f} MB). Maximum size is 50 MB.",
                )
            filename = upload_list[0].filename or "upload"
            print(f"[scan] Processing '{filename}' ({file_size_mb:.1f} MB)")
            out = process_single(contents, filename, years[0])
            if ministry:
                out["ministry_filter"] = ministry
            return json_response(out)

        # Multi year path
        year_frames: Dict[str, List[Dict]] = {}
        all_dfs = []
        combined_flagged = []
        per_year = []

        for up, yr in zip(upload_list, years):
            contents = await up.read()
            file_size_mb = len(contents) / (1024 * 1024)
            if file_size_mb > 50:
                raise HTTPException(
                    status_code=400,
                    detail=f"File too large ({file_size_mb:.1f} MB). Maximum size is 50 MB.",
                )
            filename = up.filename or "upload"
            print(f"[scan] Multi-year '{filename}' year={yr} ({file_size_mb:.1f} MB)")
            try:
                df = parse_file(contents, filename)
            except UnsupportedBudgetFormat as e:
                return json_response({"error": str(e)}, status_code=400)
            if df is None or df.empty:
                continue
            if str(df.attrs.get("budget_format") or "").startswith("state_"):
                return json_response(
                    {
                        "error": (
                            "Multi-year scanning is not available for state budgets yet. "
                            "Upload a single state file for a full scan."
                        )
                    },
                    status_code=400,
                )
            df = df.copy()
            df["budget_year"] = yr
            all_dfs.append(df)
            flagged = run_all_flags(df, budget_year=yr)
            for r in flagged:
                r["budget_year"] = yr
                r["source_file"] = filename
            year_frames[str(yr)] = []
            # Keep raw row dicts for ghost matching
            raw_rows = df.to_dict("records")
            for row in raw_rows:
                row["_flags"] = []
                row["_exclude"] = False
                row["budget_year"] = yr
            year_frames[str(yr)] = raw_rows
            scored = score_items(flagged)
            combined_flagged.extend(scored)
            per_year.append({
                "budget_year": yr,
                "filename": filename,
                "total_items": len(df),
                "flagged_items": len(scored),
            })

        if not all_dfs:
            return json_response(
                {"error": "Could not extract any data from the uploaded files. Please check the format."},
                status_code=400,
            )

        # Cross year ghost detection
        ghost_rows = flag_ghost_projects_multiyear(year_frames)
        ghost_results = []
        for row in ghost_rows:
            flags = row.get("_flags") or []
            if not flags:
                continue
            ghost_results.append({
                "row_id": row.get("row_id"),
                "description": row.get("description") or row.get("project_name"),
                "amount": row.get("amount"),
                "location": row.get("location"),
                "ministry": row.get("mda_name") or row.get("ministry"),
                "project_code": row.get("project_code"),
                "mda_code": row.get("mda_code"),
                "mda_name": row.get("mda_name") or row.get("ministry"),
                "project_name": row.get("project_name") or row.get("description"),
                "project_status": row.get("project_status"),
                "expenditure_code": row.get("expenditure_code") or row.get("economic_code"),
                "budget_year": row.get("budget_year"),
                "flags": flags,
                "is_mda_level": row.get("is_mda_level"),
            })
        ghost_scored = score_items(ghost_results)

        # Merge ghost into combined (by description+year dedupe loosely)
        existing_keys = {
            (r.get("project_code"), r.get("budget_year"), (r.get("description") or "")[:80])
            for r in combined_flagged
        }
        for g in ghost_scored:
            key = (g.get("project_code"), g.get("budget_year"), (g.get("description") or "")[:80])
            if key in existing_keys:
                # Attach ghost flag onto matching item
                for r in combined_flagged:
                    rk = (r.get("project_code"), r.get("budget_year"), (r.get("description") or "")[:80])
                    if rk == key:
                        types = {f.get("flag_type") for f in r.get("flags") or []}
                        for f in g.get("flags") or []:
                            if f.get("flag_type") not in types:
                                r.setdefault("flags", []).append(f)
                        break
            else:
                combined_flagged.append(g)

        combined_flagged = score_items(combined_flagged)
        combined_df = pd.concat(all_dfs, ignore_index=True)
        out = summarize_scan(combined_df, combined_flagged)
        out["budget_year"] = ",".join(years)
        out["multi_year"] = True
        out["ghost_enabled"] = True
        out["per_year"] = per_year
        out["narratives"] = generate_narratives(combined_flagged)
        out["visuals"] = visuals_payload(combined_flagged)
        if ministry:
            out["ministry_filter"] = ministry
        return json_response(out)

    except HTTPException:
        raise
    except ValueError as e:
        return json_response({"error": str(e)}, status_code=400)
    except Exception as e:
        traceback.print_exc()
        return json_response({"error": f"Scan failed: {str(e)}"}, status_code=400)


@app.post("/chat")
async def chat(request: Request):
    """Natural language query over the current scan result set.

    Expects JSON: { question: str, results: [...] }
    Returns prose answer plus optional filtered_results.
    """
    try:
        body = await request.json()
    except Exception:
        return json_response({"error": "Invalid JSON body."}, status_code=400)

    question = (body.get("question") or "").strip()
    results = body.get("results") or []
    if not question:
        return json_response({"error": "Question is required."}, status_code=400)

    return json_response(answer_question(question, results))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        timeout_keep_alive=120,
    )
