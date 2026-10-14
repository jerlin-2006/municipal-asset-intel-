import os
import traceback
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

import generate_C as g

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Smart City Asset Management API")

from features import router as features_router
app.include_router(features_router)

from resolve import router as resolve_router
app.include_router(resolve_router)

from solutions import router as solutions_router
app.include_router(solutions_router)

from insights import router as insights_router
app.include_router(insights_router) 

@app.get("/", response_class=HTMLResponse)
def serve_index():
    index = BASE_DIR / "index.html"
    if index.exists():
        return HTMLResponse(content=index.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h3>index.html not found</h3>", status_code=404)


@app.get("/api/reports")
def get_reports():
    """All rows of C_report.csv (values are strings; the page converts them)."""
    if os.path.exists(g.C_CSV) and os.path.getsize(g.C_CSV) > 0:
        try:
            df = pd.read_csv(g.C_CSV, dtype=str, keep_default_na=False)
            return df.to_dict(orient="records")
        except Exception:
            traceback.print_exc()
    return []


@app.get("/api/diagnostics")
def diagnostics():
    """Open http://127.0.0.1:8000/api/diagnostics to see why OCR is (not) working."""
    return {
        "gemini_key_loaded": bool(os.getenv("GEMINI_API_KEY")),
        "models_tried_in_order": g.MODELS,
        "known_roads": g.known_roads(),
        "files": {p: os.path.exists(p) for p in (g.A_CSV, g.B_CSV, g.C_CSV)},
    }


# Plain `def` (not `async def`): Gemini calls are blocking, so FastAPI runs this in a worker thread
# instead of freezing the whole server while OCR is running.
@app.post("/api/complaints")
def submit_complaint(
    complaint_id: Optional[str] = Form(None),
    complaint_text: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
):
    try:
        cid = (complaint_id or "").strip() or f"CMP-{uuid.uuid4().hex[:6].upper()}"
        text = (complaint_text or "").strip()

        if image is not None and image.filename:
            image_bytes = image.file.read()
            if not image_bytes:
                raise HTTPException(status_code=400, detail="Uploaded image is empty.")
            print(f"📥 Processing uploaded image: {image.filename} ({len(image_bytes) // 1024} KB)")
            result = g.process_image_complaint(cid, image_bytes, complaint_text=text or None)
        elif text:
            print(f"📝 Processing text: {text}")
            result = g.process_new_complaint(cid, text)
        else:
            raise HTTPException(status_code=400, detail="No petition image or text provided.")

        return {"data": result}

    except HTTPException:
        raise                                   # keep the real status code (was being turned into 500)
    except ValueError as e:                     # e.g. unreadable image
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print("❌ Error during pipeline execution:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
