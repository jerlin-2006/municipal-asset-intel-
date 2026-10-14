"""
Resolution recommendations + status timeline (add-on). Read-only:
never modifies C_report.csv, B_history.csv or work_status.json.

GET /api/workorders/{complaint_id}/detail  ->  complaint info, recommended solution, stage tracker, event log
"""
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import generate_C as g
import features as f

router = APIRouter()

STAGES = ["Open", "Assigned", "In Progress", "Resolved"]
TARGET_DAYS = {"High": 1, "Medium": 14, "Low": 30}        # edit to match your service-level targets
TARGET_TEXT = {"High": "Same day (emergency)", "Medium": "Within 14 days", "Low": "Routine patrol / monitor"}

# ---------------------------------------------------------------- playbooks
SAFETY = {
    "Water": ["Barricade the work area and divert pedestrians and traffic",
              "Locate other utilities (cables, gas) before any excavation",
              "Check the road and soil for undermining before the crew stands on it"],
    "Drainage": ["Treat sewage as a biohazard: gloves, boots, mask, wash-up afterwards",
                 "Test for toxic gas before anyone enters a manhole; never enter alone",
                 "Barricade the area and keep people away from the overflow"],
}

PLAYBOOK = {
    "burst": {
        "title": "Pipe burst / rupture",
        "Water": {
            "immediate": ["Close the nearest upstream valve to isolate the section",
                          "Inform affected households of the supply cut and arrange tanker supply"],
            "repair": ["Excavate and expose the damaged section",
                       "Cut out the failed length of pipe",
                       "Fit a new section with couplings or clamps matched to the pipe material and diameter",
                       "Flush and disinfect the line, then re-pressurise slowly and check every joint"],
            "materials": ["Replacement pipe length", "Couplings / repair clamps", "Spare isolation valve",
                          "Chlorine for disinfection", "Road reinstatement material"]},
        "Drainage": {
            "immediate": ["Set up a bypass pump to divert flow around the failed section",
                          "Stop flow into the section from upstream if possible"],
            "repair": ["Excavate and expose the broken or collapsed section",
                       "Replace the damaged pipe and re-bed it on compacted gravel",
                       "Backfill in layers and test with a flush before reinstating the road"],
            "materials": ["Replacement sewer pipe", "Bypass pump and hoses", "Bedding gravel / sand"]},
        "prevent": ["Check for pressure surges and fit surge control if the burst repeats",
                    "Inspect adjacent joints of the same age"],
    },
    "leak": {
        "title": "Leakage / seepage",
        "Water": {
            "immediate": ["Locate the exact leak point (visible seepage, listening rod)",
                          "Monitor flow and isolate the section if the leak grows"],
            "repair": ["Excavate at the leak point",
                       "Fit a repair clamp for a pinhole or small crack, or replace a short section if the joint failed",
                       "Pressure-test, then backfill and reinstate the surface"],
            "materials": ["Repair clamp", "Gasket / sealant", "Short pipe section"]},
        "Drainage": {
            "immediate": ["Inspect the manholes upstream and downstream to find the leaking joint"],
            "repair": ["Seal the joint with epoxy grout, or replace the faulty joint or gasket",
                       "Flush-test to confirm no more seepage"],
            "materials": ["Joint sealant / grout", "Rubber gasket"]},
        "prevent": ["Schedule leak-detection survey on the same road", "Replace aged joints during the repair"],
    },
    "block": {
        "title": "Blockage / choke / overflow",
        "Water": {
            "immediate": ["Flush the line at the nearest wash-out valve or hydrant"],
            "repair": ["Flush until the water runs clear",
                       "If flow stays restricted, open the line and remove the obstruction or scale"],
            "materials": ["Flushing hose", "Wash-out valve key"]},
        "Drainage": {
            "immediate": ["Put up warning signs and pump out standing sewage if it is flowing onto the road"],
            "repair": ["Open the manholes upstream and downstream to locate the blockage",
                       "Clear it with drain rods or a jetting machine",
                       "Remove silt and debris and dispose of it properly",
                       "Flush with water to confirm free flow"],
            "materials": ["Jetting machine / drain rods", "Suction machine", "PPE"]},
        "prevent": ["Add this drain to the regular desilting round",
                    "Check for illegal dumping or solid waste entering the line"],
    },
    "crack": {
        "title": "Crack / structural damage",
        "Water": {
            "immediate": ["Inspect the crack length and check for active leakage"],
            "repair": ["Expose the pipe",
                       "Clamp a hairline crack; replace the section if the crack runs along the pipe or the wall is thin",
                       "Pressure-test before backfilling"],
            "materials": ["Repair clamp", "Replacement pipe section", "Couplings"]},
        "Drainage": {
            "immediate": ["Inspect the line for the crack extent and any sign of collapse"],
            "repair": ["Patch or reline small cracks with epoxy lining",
                       "Replace the section if the pipe is deformed or collapsing",
                       "Flush-test the repaired section"],
            "materials": ["Epoxy lining / patch kit", "Replacement pipe section"]},
        "prevent": ["Check for heavy traffic loads or ground movement near the crack"],
    },
    "corrosion": {
        "title": "Corrosion",
        "Water": {
            "immediate": ["Inspect the corroded length and the pipe on either side"],
            "repair": ["Replace the corroded section with corrosion-resistant pipe (e.g. HDPE or lined ductile iron)",
                       "Check coatings and joints on the adjacent pipe",
                       "Flush and disinfect before returning to service"],
            "materials": ["Corrosion-resistant pipe", "Couplings", "Coating / wrap", "Chlorine"]},
        "Drainage": {
            "immediate": ["Inspect the pipe and manhole walls for the extent of corrosion"],
            "repair": ["Reline or replace the corroded section with corrosion-resistant pipe (PVC / HDPE)",
                       "Repair or coat the affected manhole surfaces"],
            "materials": ["Corrosion-resistant pipe or lining", "Manhole repair mortar"]},
        "prevent": ["Add the whole pipe run to the replacement plan if corrosion is widespread"],
    },
    "general": {
        "title": "General defect",
        "Water": {"immediate": ["Send a crew to inspect on site and confirm the defect"],
                  "repair": ["Diagnose the fault, then apply the matching fix (leak, crack, burst or corrosion)"],
                  "materials": ["Inspection kit", "Standard repair clamps"]},
        "Drainage": {"immediate": ["Send a crew to inspect on site and confirm the defect"],
                     "repair": ["Diagnose the fault, then apply the matching fix (blockage, crack or collapse)"],
                     "materials": ["Inspection kit", "Drain rods"]},
        "prevent": ["Log the finding so the history gets a proper defect type"],
    },
}

VERIFY = ["Pressure or flow test passes after the repair",
          "No leakage, overflow or seepage after 24 hours",
          "Re-inspect after 7 days",
          "Mark the work order Resolved and enter the actual cost and downtime"]


def _category(issue: str) -> str:
    s = (issue or "").lower()
    for key, cat in (("burst", "burst"), ("rupture", "burst"), ("collapse", "burst"),
                     ("leak", "leak"), ("seep", "leak"),
                     ("block", "block"), ("choke", "block"), ("silt", "block"), ("overflow", "block"),
                     ("crack", "crack"), ("corro", "corrosion")):
        if key in s:
            return cat
    return "general"


def _first_number(text) -> Optional[float]:
    m = re.search(r"\d+(\.\d+)?", str(text or ""))
    return float(m.group(0)) if m else None


def _parse(ts) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def _solution(r, level: str, est: Optional[dict]) -> dict:
    cat = _category(r.get("issue_type", ""))
    atype = r.get("asset_type") or r.get("extracted_asset_type") or "Water"
    atype = "Drainage" if "drain" in atype.lower() else "Water"
    book = PLAYBOOK[cat]
    part = book[atype]
    matched = bool(r.get("matched_asset_id"))
    repeated = str(r.get("is_repeated", "")).strip().lower() == "true"
    rul = _first_number(r.get("estimated_remaining_life"))
    cond = _first_number(r.get("current_condition_score"))

    immediate = list(part["immediate"])
    repair = list(part["repair"])
    prevent = list(book["prevent"])
    notes = []

    if not matched:
        immediate = ["Check the original slip or contact the citizen to confirm the exact road and asset type",
                     "Verify the road name against A_assets.csv and assign the asset manually"] + immediate
        notes.append("No asset matched, so the repair steps below are generic until an asset is assigned.")
    if level == "High":
        immediate.insert(0, f"Treat as an emergency: send {r.get('assigned_crew') or 'the response crew'} to site now")
    if repeated:
        repair.append("Repeat complaint: find the root cause (pressure surges, aged joints, ground movement, "
                      "recurring silt load) and consider replacing the whole segment instead of patching")
    if rul is not None and rul <= 1.0:
        repair.append(f"Remaining life is only about {rul:g} year(s): plan replacement of this segment, not another patch")
        notes.append("Asset is near end of life.")
    if cond is not None and cond <= 40:
        notes.append(f"Condition score is low ({cond:g}/100).")

    due = None
    ts = _parse(r.get("timestamp"))
    if ts and level in TARGET_DAYS:
        due = (ts + timedelta(days=TARGET_DAYS[level])).isoformat(timespec="seconds")

    return {
        "category": book["title"],
        "asset_type": atype,
        "immediate": immediate,
        "repair": repair,
        "materials": part["materials"],
        "safety": SAFETY[atype],
        "verify": VERIFY,
        "prevent": prevent,
        "notes": notes,
        "crew": r.get("assigned_crew", ""),
        "target": TARGET_TEXT.get(level, "Manual review first"),
        "due_at": due,
        "estimate": ({"cost": est["avg_cost"], "downtime": est["avg_downtime"], "based_on": est["n"]}
                     if est and est.get("avg_cost") is not None else None),
    }


def _timeline(r, entry: dict) -> dict:
    status = entry.get("status", "Open")
    history = sorted(entry.get("history", []), key=lambda h: str(h.get("time", "")))
    logged = r.get("timestamp", "")

    events = [{"time": logged, "title": "Complaint received",
               "note": (r.get("complaint_text", "") or "")[:200]},
              {"time": logged,
               "title": ("Asset matched: " + r["matched_asset_id"]) if r.get("matched_asset_id")
               else "No asset matched, needs manual review",
               "note": f"Priority {r.get('priority_level', 'Unassigned')} ({r.get('priority_score', '0')}); "
                       f"crew: {r.get('assigned_crew', '-')}"}]
    last_resolved = max((i for i, h in enumerate(history) if h.get("status") == "Resolved"), default=-1)
    for i, h in enumerate(history):
        note = h.get("note", "") or ""
        res = entry.get("resolution")
        if i == last_resolved and res:
            bits = []
            if res.get("repair_cost") not in (None, ""):
                bits.append(f"cost Rs {res['repair_cost']}")
            if res.get("downtime_hours") not in (None, ""):
                bits.append(f"downtime {res['downtime_hours']} h")
            if bits:
                note = (note + " | " if note else "") + ", ".join(bits)
        events.append({"time": h.get("time", ""), "title": f"Status changed to {h.get('status', '?')}", "note": note})

    reached = {"Open": logged}
    for h in history:
        reached.setdefault(h.get("status"), h.get("time", ""))
    cur = STAGES.index(status) if status in STAGES else 0
    stages = []
    for i, name in enumerate(STAGES):
        if status == "Resolved" or i < cur:
            state = "done"
        elif i == cur:
            state = "current"
        else:
            state = "pending"
        stages.append({"name": name, "state": state, "time": reached.get(name, "") if state != "pending" else ""})
    return {"stages": stages, "events": events}


# ---------------------------------------------------------------- endpoints
@router.get("/solutions.js")
def solutions_js():
    return FileResponse(g.BASE_DIR / "solutions.js", media_type="application/javascript; charset=utf-8")


@router.get("/api/workorders/{complaint_id}/detail")
def detail(complaint_id: str):
    df = f._rows()
    if df.empty or complaint_id not in set(df["complaint_id"]):
        raise HTTPException(404, "Unknown complaint_id")
    r = df[df["complaint_id"] == complaint_id].iloc[0].to_dict()
    entry = f._load_status().get(complaint_id, {})
    level = r.get("priority_level", "Unassigned") or "Unassigned"
    est = f._estimate(r.get("issue_type", ""), f._cost_table()) if r.get("matched_asset_id") else None

    sol = _solution(r, level, est)
    tl = _timeline(r, entry)
    status = entry.get("status", "Open")
    due = _parse(sol["due_at"])
    sol["overdue"] = bool(due and status != "Resolved" and datetime.now() > due)

    return {
        "id": complaint_id,
        "asset_id": r.get("matched_asset_id", ""),
        "location": r.get("location", ""),
        "issue_type": r.get("issue_type", ""),
        "priority_level": level,
        "priority_score": r.get("priority_score", ""),
        "status": status,
        "solution": sol,
        "stages": tl["stages"],
        "events": tl["events"],
    }