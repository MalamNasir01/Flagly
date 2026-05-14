"""
app.py — Flagly Budget Scanner API
Run with: uvicorn app:app --reload --port 8000
"""

import json
import math
from typing import Optional
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import pandas as pd

from engines.parser import parse_file
from engines.flags import (
    flag_duplicates,
    flag_amount_anomalies,
    flag_missing_locations,
    flag_ghost_projects,
    compute_risk_scores,
)

app = FastAPI(
    title="Flagly — Nigerian Budget Red Flag Scanner",
    description="Automated red-flag detection for Nigerian budget appropriations",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store for multi-year ghost-project detection
# { session_id: { year: df } }
_session_store: dict = {}


def _safe_value(v):
    """Convert numpy/pandas types to JSON-safe Python types."""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def _df_to_records(df: pd.DataFrame) -> list:
    """Convert scored DataFrame to clean JSON-serialisable records."""
    records = []
    for _, row in df.iterrows():
        record = {
            "row_id": int(row["row_id"]),
            "project_code": _safe_value(row.get("project_code")),
            "description": str(row["description"]),
            "amount": _safe_value(row.get("amount")),
            "location": _safe_value(row.get("location")),
            "ministry": _safe_value(row.get("ministry")),
            "risk_score": float(row["risk_score"]),
            "risk_level": str(row["risk_level"]),
            "flag_count": int(row["flag_count"]),
            "flags": [
                {
                    "flag_type": f["flag_type"],
                    "severity": f["severity"],
                    "score": float(f["score"]),
                    "reason": f["reason"],
                    "detail": f["detail"],
                }
                for f in row["flags"]
            ],
        }
        records.append(record)
    return records


@app.get("/")
def root():
    return {
        "service": "Flagly — Nigerian Budget Red Flag Scanner",
        "status": "running",
        "endpoints": {
            "upload_single": "POST /scan",
            "upload_multi_year": "POST /scan/multi-year",
            "health": "GET /health"
        }
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/scan")
async def scan_single(
    file: UploadFile = File(...),
    year: Optional[str] = Form(default="unknown"),
    session_id: Optional[str] = Form(default=None),
):
    """
    Upload a single budget file (PDF, Excel, CSV).
    Runs all 4 flag engines and returns flagged line items with risk scores.

    Optional: pass session_id + year to accumulate files for ghost-project detection.
    """
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(400, "Uploaded file is empty.")

    # Parse
    try:
        df = parse_file(content, file.filename)
    except Exception as e:
        raise HTTPException(422, f"Could not parse file: {str(e)}")

    if df.empty:
        raise HTTPException(422, "No budget line items could be extracted from this file.")

    # Store for multi-year detection
    if session_id:
        if session_id not in _session_store:
            _session_store[session_id] = {}
        _session_store[session_id][year] = df

    # Run flag engines
    dup_flags = flag_duplicates(df)
    amt_flags = flag_amount_anomalies(df)
    loc_flags = flag_missing_locations(df)

    # Ghost project: compare with stored sessions if available
    ghost_flags = []
    if session_id and len(_session_store.get(session_id, {})) >= 2:
        ghost_flags = flag_ghost_projects(_session_store[session_id])

    all_flags = dup_flags + amt_flags + loc_flags + ghost_flags

    # Score
    scored_df = compute_risk_scores(df, all_flags)

    # Summary stats
    flagged_rows = scored_df[scored_df["flag_count"] > 0]
    total_amount = df["amount"].sum()
    flagged_amount = flagged_rows["amount"].sum()

    summary = {
        "filename": file.filename,
        "year": year,
        "total_items": len(df),
        "flagged_items": len(flagged_rows),
        "flag_breakdown": {
            "DUPLICATE": len([f for f in all_flags if f["flag_type"] == "DUPLICATE"]),
            "AMOUNT_ANOMALY": len([f for f in all_flags if f["flag_type"] == "AMOUNT_ANOMALY"]),
            "MISSING_LOCATION": len([f for f in all_flags if f["flag_type"] == "MISSING_LOCATION"]),
            "GHOST_PROJECT": len([f for f in all_flags if f["flag_type"] == "GHOST_PROJECT"]),
        },
        "total_budget_amount": _safe_value(total_amount) if not pd.isna(total_amount) else None,
        "flagged_amount": _safe_value(flagged_amount) if not pd.isna(flagged_amount) else None,
        "high_risk_count": len(scored_df[scored_df["risk_level"] == "HIGH"]),
        "medium_risk_count": len(scored_df[scored_df["risk_level"] == "MEDIUM"]),
    }

    # Return all rows but sorted by risk (flagged first)
    records = _df_to_records(scored_df)

    return JSONResponse({
        "status": "success",
        "summary": summary,
        "items": records
    })


@app.post("/scan/multi-year")
async def scan_multi_year(
    files: list[UploadFile] = File(...),
    years: str = Form(...),   # comma-separated: "2022,2023,2024"
):
    """
    Upload multiple budget files at once for ghost-project cross-year detection.
    years param must match the number of files, comma-separated.
    """
    year_list = [y.strip() for y in years.split(",")]
    if len(year_list) != len(files):
        raise HTTPException(400, "Number of years must match number of files.")

    dfs_by_year = {}
    all_flags = []
    all_dfs = {}

    for file, year in zip(files, year_list):
        content = await file.read()
        try:
            df = parse_file(content, file.filename)
        except Exception as e:
            raise HTTPException(422, f"Could not parse {file.filename}: {str(e)}")
        dfs_by_year[year] = df
        all_dfs[year] = df

        dup_flags = flag_duplicates(df)
        amt_flags = flag_amount_anomalies(df)
        loc_flags = flag_missing_locations(df)
        for f in dup_flags + amt_flags + loc_flags:
            f["year"] = year
        all_flags.extend(dup_flags + amt_flags + loc_flags)

    ghost_flags = flag_ghost_projects(dfs_by_year)
    all_flags.extend(ghost_flags)

    # Score across all years
    results_by_year = {}
    for year, df in all_dfs.items():
        year_flags = [f for f in all_flags if f.get("year") == year or "year" not in f]
        scored = compute_risk_scores(df, year_flags)
        results_by_year[year] = _df_to_records(scored)

    return JSONResponse({
        "status": "success",
        "years_analysed": year_list,
        "ghost_project_flags": len(ghost_flags),
        "results_by_year": results_by_year
    })
