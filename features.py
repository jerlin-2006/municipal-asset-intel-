"""
Add-on features. Reads A/B/C CSVs but NEVER modifies the core pipeline or C_report.csv.
Extra state (work-order status) lives in its own file: work_status.json
"""
import json
import threading
import traceback
from datetime import datetime
from typing import Optional

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import generate_C as g

router = APIRouter()

STATUS_FILE = g.BASE_DIR / "work_status.json"
STATUSES = ["Open", "Assigned", "In Progress", "Resolved"]
_lock = threading.Lock()

# complaint wording -> complaint_type used in B_history.csv
ISSUE_TO_TYPE = [("burst", "Leakage"), ("leak", "Leakage"), ("block", "Blockage"),
                 ("crack", "Crack"), ("corro", "Corrosion")]


# ---------------- helpers ----------------
def _load_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_status(data: dict):
    STATUS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _num(x, nd=1):
    """float -> JSON-safe number (None for NaN/inf/non-numeric). nd=0 returns int."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return int(round(v)) if nd == 0 else round(v, nd)


def _dt(series: pd.Series) -> pd.Series:
    """Parse dates (timezone-safe). If day-first parses more rows (Excel dd-mm-yyyy), use that."""
    def parse(**kw):
        return pd.to_datetime(series, errors="coerce", utc=True, **kw).dt.tz_localize(None)
    s = parse()
    if s.isna().any():
        alt = parse(dayfirst=True)
        if alt.notna().sum() > s.notna().sum():
            s = alt
    return s


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name] if name in df.columns else pd.Series([""] * len(df), index=df.index)


def _history() -> pd.DataFrame:
    try:
        h = pd.read_csv(g.B_CSV)
        h.columns = h.columns.str.strip()
        return h
    except Exception:
        return pd.DataFrame()


def _cost_table() -> dict:
    """Average repair cost / downtime per complaint_type, from Maintenance records in B."""
    try:
        h = _history()
        if h.empty or not {"record_type", "repair_cost", "complaint_type"}.issubset(h.columns):
            return {}
        m = h[h["record_type"].astype(str).str.strip().str.lower() == "maintenance"].copy()
        clean = lambda s: pd.to_numeric(
            s.astype(str).str.replace(r"[^\d.\-]", "", regex=True), errors="coerce")
        m["repair_cost"] = clean(m["repair_cost"])
        m["downtime_hours"] = clean(_col(m, "downtime_hours"))
        m = m.dropna(subset=["repair_cost"])
        out = {}
        for t, grp in m.groupby("complaint_type"):
            out[str(t)] = {"avg_cost": _num(grp["repair_cost"].mean(), 0),
                           "avg_downtime": _num(grp["downtime_hours"].mean(), 1),
                           "n": int(len(grp))}
        return out
    except Exception:
        traceback.print_exc()
        return {}


def _estimate(issue_type: str, table: dict) -> Optional[dict]:
    s = (issue_type or "").lower()
    for key, ctype in ISSUE_TO_TYPE:
        if key in s:
            return table.get(ctype)
    return None


def _rows() -> pd.DataFrame:
    return g._read_c(g.C_CSV)


# ---------------- endpoints ----------------
@router.get("/features.js")
def features_js():
    return FileResponse(g.BASE_DIR / "features.js", media_type="application/javascript; charset=utf-8")


@router.get("/api/workorders")
def workorders():
    """{complaint_id: {status, updated, est_cost, est_downtime}} for every row in C_report.csv."""
    df, status, table = _rows(), _load_status(), _cost_table()
    out = {}
    for _, r in df.iterrows():
        cid = r.get("complaint_id", "")
        st = status.get(cid, {})
        est = _estimate(r.get("issue_type", ""), table) if r.get("matched_asset_id") else None
        est = est if est and est.get("avg_cost") is not None else None
        out[cid] = {"status": st.get("status", "Open"), "updated": st.get("updated", ""),
                    "est_cost": est["avg_cost"] if est else None,
                    "est_downtime": est["avg_downtime"] if est else None}
    return out


class StatusUpdate(BaseModel):
    status: str
    note: Optional[str] = None


@router.patch("/api/workorders/{complaint_id}/status")
def update_status(complaint_id: str, body: StatusUpdate):
    if body.status not in STATUSES:
        raise HTTPException(400, f"status must be one of {STATUSES}")
    df = _rows()
    if df.empty or complaint_id not in set(df["complaint_id"]):
        raise HTTPException(404, "Unknown complaint_id")
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        data = _load_status()
        entry = data.setdefault(complaint_id, {"history": []})
        entry["status"], entry["updated"] = body.status, now
        entry["history"].append({"status": body.status, "time": now, "note": body.note or ""})
        _save_status(data)
    return {"complaint_id": complaint_id, "status": body.status, "updated": now}


@router.get("/api/summary")
def summary(hotspot_min: int = 3, hotspot_days: int = 30):
    df, status = _rows(), _load_status()
    if df.empty:
        return {"total": 0, "open": 0, "high_open": 0, "resolved": 0, "unmatched": 0,
                "avg_rul": None, "hotspots": []}
    df = df.copy()
    df["status"] = df["complaint_id"].map(lambda c: status.get(c, {}).get("status", "Open"))
    active = df[df["status"] != "Resolved"]
    matched = active[_col(active, "matched_asset_id") != ""]
    rul = pd.to_numeric(_col(matched, "estimated_remaining_life").str.extract(r"([\d.]+)")[0], errors="coerce")

    ts = pd.to_datetime(_col(df, "timestamp"), errors="coerce")
    recent = df[ts >= pd.Timestamp.now() - pd.Timedelta(days=hotspot_days)]
    hotspots = []
    for loc, grp in recent.groupby("location"):
        if loc and loc != "Unknown" and len(grp) >= hotspot_min:
            assets = sorted({a for a in grp["matched_asset_id"] if a})
            hotspots.append({"location": loc, "count": int(len(grp)), "assets": assets})
    hotspots.sort(key=lambda h: -h["count"])

    return {
        "total": int(len(df)),
        "open": int(len(active)),
        "resolved": int((df["status"] == "Resolved").sum()),
        "high_open": int((_col(active, "priority_level") == "High").sum()),
        "unmatched": int((_col(active, "matched_asset_id") == "").sum()),
        "avg_rul": round(float(rul.mean()), 1) if rul.notna().any() else None,
        "hotspots": hotspots,
    }


@router.get("/api/analytics")
def analytics(months: int = 60):
    """Never returns a bare 500: any crash is reported inside `warnings` so the page can show it."""
    try:
        return _analytics(months)
    except Exception as e:
        traceback.print_exc()
        return {"months": [], "history_monthly": [], "live_monthly": [], "by_asset": {},
                "cost_table": {},
                "warnings": [f"Analytics crashed on the server: {type(e).__name__}: {e}"]}


def _analytics(months: int = 60):
    """Returns what it can plus a `warnings` list explaining gaps."""
    months = max(6, min(int(months), 120))
    warnings = []
    h, c = _history(), _rows()
    if h.empty:
        warnings.append(f"B_history.csv is missing or empty (looked in {g.B_CSV}); history charts will be blank.")
    if c.empty:
        warnings.append("No live complaints logged yet (C_report.csv is empty); the orange series will be blank.")

    periods = pd.period_range(end=pd.Timestamp.now(), periods=months, freq="M")
    labels = [str(p) for p in periods]

    def monthly(df, ts_col, mask=None, name=""):
        if df.empty or ts_col not in df.columns:
            return [0] * months
        d = df if mask is None else df[mask]
        ts = _dt(d[ts_col])
        bad = int(ts.isna().sum())
        if bad:
            warnings.append(f"{bad} {name} timestamp(s) could not be parsed and were skipped.")
        p = ts.dropna().dt.to_period("M")
        counts = p.value_counts()
        older = int((p < periods[0]).sum())
        if older:
            warnings.append(f"{older} older {name} record(s) fall outside the selected {months}-month range.")
        return [int(counts.get(m, 0)) for m in periods]

    hist_mask = None
    if not h.empty and "record_type" in h.columns:
        hist_mask = h["record_type"].astype(str).str.strip().str.lower() == "complaint"

    history_monthly = monthly(h, "timestamp", hist_mask, "history")
    live_monthly = monthly(c, "timestamp", None, "live")

    by_asset = {}
    if not h.empty and "asset_id" in h.columns:
        sev = pd.to_numeric(_col(h, "severity"), errors="coerce")
        for a_id, grp in h.groupby("asset_id"):
            by_asset[str(a_id)] = {"history_records": int(len(grp)), "avg_severity": _num(sev[grp.index].mean(), 2)}
    if not c.empty and "matched_asset_id" in c.columns:
        for a_id, n in c[c["matched_asset_id"] != ""]["matched_asset_id"].value_counts().items():
            by_asset.setdefault(str(a_id), {"history_records": 0, "avg_severity": None})["live_complaints"] = int(n)

    return {
        "months": labels,
        "history_monthly": history_monthly,
        "live_monthly": live_monthly,
        "by_asset": by_asset,
        "cost_table": _cost_table(),
        "warnings": list(dict.fromkeys(warnings)),
    }


@router.get("/api/export.csv")
def export_csv():
    df, status, table = _rows(), _load_status(), _cost_table()
    if df.empty:
        return Response("", media_type="text/csv")
    df = df.copy()
    df["work_status"] = df["complaint_id"].map(lambda x: status.get(x, {}).get("status", "Open"))
    est = [(_estimate(i, table) if a else None) for i, a in zip(_col(df, "issue_type"), _col(df, "matched_asset_id"))]
    df["est_repair_cost"] = [e["avg_cost"] if e else "" for e in est]
    df["est_downtime_hours"] = [e["avg_downtime"] if e else "" for e in est]
    df["_s"] = pd.to_numeric(_col(df, "priority_score"), errors="coerce").fillna(0)
    df = df.sort_values("_s", ascending=False).drop(columns="_s")
    return Response(df.to_csv(index=False), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=work_orders.csv"})