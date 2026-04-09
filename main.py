import os
import math
import json
import traceback
from typing import Optional
from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd

from engines.parser import parse_file
from engines.flags import run_all_flags
from engines.scorer import score_items

app = FastAPI(title="UDEME Budget Red-Flag Scanner")

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
    # Handle numpy scalar types that sneak through from pandas
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
    """Convert a value to float, returning 0 if NaN/Inf."""
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return f
    except Exception:
        return 0.0


def json_response(data: dict, status_code: int = 200) -> JSONResponse:
    """Return a JSONResponse with sanitized data."""
    return JSONResponse(content=sanitize(data), status_code=status_code)


@app.get("/")
async def root():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)


@app.get("/favicon.ico")
async def favicon():
    return JSONResponse(status_code=204, content={})


@app.get("/health")
async def health():
    return JSONResponse(content={"status": "ok"})


@app.post("/scan")
async def scan(
    file: UploadFile = File(...),
    budget_year: str = Form(...),
    ministry: Optional[str] = Form(None),
):
    try:
        contents = await file.read()
        filename = file.filename or "upload"

        df = parse_file(contents, filename)

        if df is None or df.empty:
            return json_response(
                {"error": "Could not extract any data from the uploaded file. Please check the format."},
                status_code=400,
            )

        total_items = len(df)
        total_amount = safe_float(df["amount"].sum()) if "amount" in df.columns else 0.0

        flagged_rows = run_all_flags(df, budget_year=budget_year)

        if not flagged_rows:
            return json_response({
                "total_items": total_items,
                "flagged_items": 0,
                "high_risk": 0,
                "medium_risk": 0,
                "low_risk": 0,
                "at_risk_amount": 0,
                "total_amount": total_amount,
                "flag_summary": {
                    "duplicate_clusters": 0,
                    "inflated_amounts": 0,
                    "context_mismatch": 0,
                    "missing_location": 0,
                    "ghost_projects": 0,
                },
                "results": [],
            })

        scored = score_items(flagged_rows)

        # Include all scored items (scorer already filtered out low-only/score<40)
        results = scored

        high_risk    = sum(1 for r in results if r.get("risk_level") == "HIGH")
        medium_risk  = sum(1 for r in results if r.get("risk_level") == "MEDIUM")
        low_risk     = sum(1 for r in results if r.get("risk_level") == "LOW")
        at_risk_amount = sum(safe_float(r.get("amount") or 0) for r in results)

        flag_summary = {
            "duplicate_clusters": 0,
            "inflated_amounts": 0,
            "context_mismatch": 0,
            "missing_location": 0,
            "ghost_projects": 0,
        }
        for r in results:
            seen_types = set()
            for f in r.get("flags", []):
                ft = f.get("flag_type", "")
                if ft not in seen_types:
                    seen_types.add(ft)
                    if ft == "DUPLICATE_CLUSTER":
                        flag_summary["duplicate_clusters"] += 1
                    elif ft == "INFLATED_AMOUNT":
                        flag_summary["inflated_amounts"] += 1
                    elif ft == "CONTEXT_MISMATCH":
                        flag_summary["context_mismatch"] += 1
                    elif ft == "MISSING_LOCATION":
                        flag_summary["missing_location"] += 1
                    elif ft == "GHOST_PROJECT":
                        flag_summary["ghost_projects"] += 1

        return json_response({
            "total_items": total_items,
            "flagged_items": len(results),
            "high_risk": high_risk,
            "medium_risk": medium_risk,
            "low_risk": low_risk,
            "at_risk_amount": at_risk_amount,
            "total_amount": total_amount,
            "flag_summary": flag_summary,
            "results": results,
        })

    except Exception as e:
        traceback.print_exc()
        return json_response({"error": f"Scan failed: {str(e)}"}, status_code=400)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
