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
from engines.flags import run_all_flags, flag_ghost_projects_multiyear
from engines.scorer import score_items

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


def safe_float(val):
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return f
    except Exception:
        return 0.0


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
    "overhead_dominance":  0,
    "composite_duplicate": 0,
    "inflated_projection": 0,
    "phantom_spending":    0,
    "vague_high_value":    0,
    "zero_rollover":       0,
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
}


def build_flag_summary(results: List[Dict]) -> Dict[str, int]:
    flag_summary = dict(EMPTY_FLAG_SUMMARY)
    for r in results:
        seen_types = set()
        for f in r.get("flags", []):
            ft = f.get("flag_type", "")
            if ft not in seen_types:
                seen_types.add(ft)
                key = _FLAG_MAP.get(ft)
                if key:
                    flag_summary[key] += 1
    return flag_summary


def summarize_scan(df: pd.DataFrame, results: List[Dict]) -> Dict[str, Any]:
    total_items = len(df)
    total_amount = safe_float(df["amount"].sum()) if "amount" in df.columns else 0.0
    high_risk = sum(1 for r in results if r.get("risk_level") == "HIGH")
    medium_risk = sum(1 for r in results if r.get("risk_level") == "MEDIUM")
    low_risk = sum(1 for r in results if r.get("risk_level") == "LOW")
    at_risk_amount = sum(safe_float(r.get("amount") or 0) for r in results)
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
    }


def generate_narratives(results: List[Dict]) -> List[Dict]:
    """Deterministic beat-reporter style cluster summaries."""
    clusters: Dict[str, List[Dict]] = {}
    for r in results:
        mda = (r.get("mda_name") or r.get("ministry") or "Unknown MDA").strip()
        for f in r.get("flags") or []:
            key = f"{mda}||{f.get('flag_type')}"
            clusters.setdefault(key, []).append(r)

    narratives = []
    for key, items in clusters.items():
        mda, flag_type = key.split("||", 1)
        uniq = {id(i): i for i in items}
        items = list(uniq.values())
        total = sum(safe_float(i.get("amount")) for i in items)
        sample = items[0]
        flag = next((f for f in (sample.get("flags") or []) if f.get("flag_type") == flag_type), None)
        title = (flag or {}).get("title") or flag_type.replace("_", " ").title()
        n = len(items)
        exposure = f"NGN {total:,.0f}"
        body = (
            f"{mda} has {n} line item{'s' if n != 1 else ''} flagged for {title.lower()}. "
            f"Combined exposure is {exposure}. "
            f"{(flag or {}).get('explanation') or 'The scanner marked a repeating pattern in this cluster.'} "
            f"Ask the ministry which contract covers each site and request the bill of quantities. "
            f"Ask for geo tagged completion evidence or an Open Treasury payment trail for the same codes."
        )
        # Strip hyphens / em dashes from user facing copy
        body = body.replace("—", ". ").replace("–", ", ").replace(" - ", ", ")
        narratives.append({
            "cluster_key": key,
            "mda": mda,
            "flag_type": flag_type,
            "item_count": n,
            "total_amount": total,
            "summary": body,
            "journalist_questions": [
                f"Which contract and contractor cover each of the {n} items under {mda}?",
                f"Can {mda} produce geo tagged completion evidence for the {exposure} exposure?",
            ],
        })

    narratives.sort(key=lambda n: n.get("total_amount") or 0, reverse=True)
    return narratives[:40]


def process_single(contents: bytes, filename: str, budget_year: str) -> Dict[str, Any]:
    df = parse_file(contents, filename)
    if df is None or df.empty:
        raise ValueError("Could not extract any data from the uploaded file. Please check the format.")
    flagged_rows = run_all_flags(df, budget_year=budget_year)
    scored = score_items(flagged_rows) if flagged_rows else []
    out = summarize_scan(df, scored)
    out["budget_year"] = budget_year
    out["filename"] = filename
    out["narratives"] = generate_narratives(scored)
    out["multi_year"] = False
    out["ghost_enabled"] = False
    return out


@app.get("/")
async def root():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    return JSONResponse(content={"status": "ok"})


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
            df = parse_file(contents, filename)
            if df is None or df.empty:
                continue
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


def _parse_ngn_threshold(question: str) -> Optional[float]:
    q = question.lower().replace(",", "")
    m = re.search(r'₦?\s*([\d.]+)\s*(billion|bn|million|m|trillion|tn)?', q)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in ("billion", "bn"):
        return val * 1_000_000_000
    if unit in ("million", "m"):
        return val * 1_000_000
    if unit in ("trillion", "tn"):
        return val * 1_000_000_000_000
    if val >= 1000:
        return val
    return None


@app.post("/chat")
async def chat(request: Request):
    """Natural language query over the current scan result set.

    Expects JSON: { question: str, results: [...] }
    Returns prose answer plus optional filtered_results.
    Uses a local rule based interpreter. If OPENAI_API_KEY is set, can be
    extended later; the contract stays the same.
    """
    try:
        body = await request.json()
    except Exception:
        return json_response({"error": "Invalid JSON body."}, status_code=400)

    question = (body.get("question") or "").strip()
    results = body.get("results") or []
    if not question:
        return json_response({"error": "Question is required."}, status_code=400)

    q = question.lower()
    filtered = list(results)
    answer_parts = []

    threshold = _parse_ngn_threshold(question)
    if "no location" in q or "missing location" in q or "without location" in q:
        filtered = [
            r for r in filtered
            if not (r.get("location") and str(r.get("location")).strip() not in ("", "—", "-", "None", "nan"))
            or any(f.get("flag_type") in ("MISSING_LOCATION", "VAGUE_LOCATION") for f in (r.get("flags") or []))
        ]
        answer_parts.append(f"Found {len(filtered)} items with missing or vague location.")

    if threshold is not None and ("above" in q or "over" in q or ">" in q):
        filtered = [r for r in filtered if safe_float(r.get("amount")) >= threshold]
        answer_parts.append(f"Restricted to amounts at or above NGN {threshold:,.0f}.")

    if "high risk" in q or "high-risk" in q:
        if "most" in q or "ministry" in q or "mda" in q:
            counts: Dict[str, int] = {}
            for r in results:
                if r.get("risk_level") != "HIGH":
                    continue
                mda = r.get("mda_name") or r.get("ministry") or "Unknown"
                counts[mda] = counts.get(mda, 0) + 1
            if counts:
                top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]
                lines = ", ".join(f"{m} ({c})" for m, c in top)
                answer_parts.append(f"MDAs with the most HIGH risk items: {lines}.")
                top_mda = top[0][0]
                filtered = [r for r in results if (r.get("mda_name") or r.get("ministry")) == top_mda and r.get("risk_level") == "HIGH"]
            else:
                answer_parts.append("No HIGH risk items in the current result set.")
                filtered = []
        else:
            filtered = [r for r in filtered if r.get("risk_level") == "HIGH"]
            answer_parts.append(f"{len(filtered)} HIGH risk items match.")

    if "duplicate" in q:
        filtered = [
            r for r in filtered
            if any(f.get("flag_type") in ("DUPLICATE_CLUSTER", "COMPOSITE_DUPLICATE") for f in (r.get("flags") or []))
        ]
        if threshold is not None:
            # cluster total above threshold
            kept = []
            for r in filtered:
                total = safe_float(r.get("amount"))
                for f in r.get("flags") or []:
                    if f.get("flag_type") == "DUPLICATE_CLUSTER":
                        # approximate with own amount * cluster size when present
                        cs = f.get("cluster_size") or 1
                        total = max(total, safe_float(r.get("amount")) * cs)
                if total >= threshold:
                    kept.append(r)
            filtered = kept
        answer_parts.append(f"{len(filtered)} duplicate cluster items match.")

    if not answer_parts:
        # Generic keyword search
        tokens = [t for t in re.split(r"\W+", q) if len(t) > 3]
        if tokens:
            filtered = [
                r for r in results
                if any(
                    t in (r.get("description") or "").lower()
                    or t in (r.get("mda_name") or "").lower()
                    or t in (r.get("location") or "").lower()
                    for t in tokens
                )
            ]
            answer_parts.append(f"Matched {len(filtered)} items against keywords in your question.")
        else:
            filtered = results[:25]
            answer_parts.append("Showing the top flagged items from the current scan.")

    answer = " ".join(answer_parts)
    answer = answer.replace("—", ". ").replace("–", ", ")
    return json_response({
        "answer": answer,
        "filtered_results": filtered[:100],
        "match_count": len(filtered),
    })


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
