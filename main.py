import os
import math
from typing import Optional
from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
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


@app.get("/")
async def root():
    return FileResponse("frontend/index.html")


@app.get("/favicon.ico")
async def favicon():
    return JSONResponse(status_code=204, content={})


@app.get("/health")
async def health():
    return {"status": "ok"}


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
            return JSONResponse(
                status_code=400,
                content={"error": "Could not extract any data from the uploaded file. Please check the format."},
            )

        flagged_rows = run_all_flags(df, budget_year=budget_year)

        if not flagged_rows:
            return {
                "total_items": len(df),
                "flagged_items": 0,
                "high_risk": 0,
                "medium_risk": 0,
                "low_risk": 0,
                "at_risk_amount": 0,
                "total_amount": float(df["amount"].sum()) if "amount" in df.columns else 0,
                "flag_summary": {
                    "duplicate_clusters": 0,
                    "inflated_amounts": 0,
                    "context_mismatch": 0,
                    "missing_location": 0,
                    "ghost_projects": 0,
                },
                "results": [],
            }

        scored = score_items(flagged_rows)

        results = [r for r in scored if r.get("risk_score", 0) >= 40 or any(
            f["severity"] in ("HIGH", "MEDIUM") for f in r.get("flags", [])
        )]

        high_risk = sum(1 for r in results if r.get("risk_level") == "HIGH")
        medium_risk = sum(1 for r in results if r.get("risk_level") == "MEDIUM")
        low_risk = sum(1 for r in results if r.get("risk_level") == "LOW")
        at_risk_amount = sum(r.get("amount", 0) or 0 for r in results)
        total_amount = float(df["amount"].sum()) if "amount" in df.columns else 0

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
                if ft == "DUPLICATE_CLUSTER" and "DUPLICATE_CLUSTER" not in seen_types:
                    flag_summary["duplicate_clusters"] += 1
                    seen_types.add(ft)
                elif ft == "INFLATED_AMOUNT" and "INFLATED_AMOUNT" not in seen_types:
                    flag_summary["inflated_amounts"] += 1
                    seen_types.add(ft)
                elif ft == "CONTEXT_MISMATCH" and "CONTEXT_MISMATCH" not in seen_types:
                    flag_summary["context_mismatch"] += 1
                    seen_types.add(ft)
                elif ft == "MISSING_LOCATION" and "MISSING_LOCATION" not in seen_types:
                    flag_summary["missing_location"] += 1
                    seen_types.add(ft)
                elif ft == "GHOST_PROJECT" and "GHOST_PROJECT" not in seen_types:
                    flag_summary["ghost_projects"] += 1
                    seen_types.add(ft)

        total_items = len(df)
        flagged_items = len(results)

        return {
            "total_items": total_items,
            "flagged_items": flagged_items,
            "high_risk": high_risk,
            "medium_risk": medium_risk,
            "low_risk": low_risk,
            "at_risk_amount": at_risk_amount,
            "total_amount": total_amount,
            "flag_summary": flag_summary,
            "results": results,
        }

    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"Scan failed: {str(e)}"},
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
