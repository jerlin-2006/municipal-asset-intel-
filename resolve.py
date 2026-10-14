"""
Resolve mechanism (add-on). Closes a work order with actual cost, downtime and a note.
- Marks the complaint 'Resolved' in work_status.json (same file features.py uses)
- Appends a row to resolutions.csv (separate file; B_history.csv and C_report.csv are never touched)
"""
import csv
import traceback
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import generate_C as g
import features as f

router = APIRouter()

RES_CSV = g.BASE_DIR / "resolutions.csv"
FIELDS = ["resolved_at", "complaint_id", "asset_id", "location", "issue_type",
          "repair_cost", "downtime_hours", "note"]


class ResolveBody(BaseModel):
    repair_cost: Optional[float] = None
    downtime_hours: Optional[float] = None
    note: Optional[str] = None


def _append_resolution(row: dict):
    new = not RES_CSV.exists() or RES_CSV.stat().st_size == 0
    with open(RES_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


@router.get("/resolve.js")
def resolve_js():
    return FileResponse(g.BASE_DIR / "resolve.js", media_type="application/javascript; charset=utf-8")


@router.post("/api/workorders/{complaint_id}/resolve")
def resolve(complaint_id: str, body: ResolveBody):
    df = f._rows()
    if df.empty or complaint_id not in set(df["complaint_id"]):
        raise HTTPException(404, "Unknown complaint_id")
    for name, v in (("repair_cost", body.repair_cost), ("downtime_hours", body.downtime_hours)):
        if v is not None and (v != v or v < 0):          # NaN or negative
            raise HTTPException(400, f"{name} must be a non-negative number")

    r = df[df["complaint_id"] == complaint_id].iloc[0]
    now = datetime.now().isoformat(timespec="seconds")
    resolution = {"resolved_at": now, "complaint_id": complaint_id,
                  "asset_id": r.get("matched_asset_id", ""), "location": r.get("location", ""),
                  "issue_type": r.get("issue_type", ""),
                  "repair_cost": "" if body.repair_cost is None else body.repair_cost,
                  "downtime_hours": "" if body.downtime_hours is None else body.downtime_hours,
                  "note": (body.note or "").strip()}
    try:
        with f._lock:
            data = f._load_status()
            entry = data.setdefault(complaint_id, {"history": []})
            entry["status"], entry["updated"] = "Resolved", now
            entry["resolution"] = {k: resolution[k] for k in ("repair_cost", "downtime_hours", "note")}
            entry["history"].append({"status": "Resolved", "time": now, "note": resolution["note"]})
            f._save_status(data)
            _append_resolution(resolution)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Could not save resolution: {e}")
    return {"complaint_id": complaint_id, "status": "Resolved", "updated": now}


@router.get("/api/resolutions")
def resolutions():
    """All logged resolutions (newest first)."""
    if not RES_CSV.exists() or RES_CSV.stat().st_size == 0:
        return []
    with open(RES_CSV, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))[::-1]