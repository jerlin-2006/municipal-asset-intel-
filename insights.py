"""
Predictive insights (add-on):
  1. Proactive risk ranking  - every asset in A_assets.csv, not just the ones people complained about
  2. Budget optimizer        - picks the repairs that remove the most risk for a given budget
  3. Map / road hotspots     - data for a Leaflet map (needs latitude/longitude columns in A_assets.csv)

Read-only. It reuses the core risk model (generate_C.calculate_rul_and_risk) and never writes any file.
"""
import math
import os
import re
import traceback

import numpy as np
import pandas as pd
from fastapi import APIRouter
from fastapi.responses import FileResponse

import generate_C as g
import features as f

router = APIRouter()
_cache = {"key": None, "val": None}

LAT_NAMES = ("latitude", "lat")
LON_NAMES = ("longitude", "lon", "lng", "long")


# ---------------- helpers ----------------
def _mtime(path) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _fl(v):
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _first_number(text):
    m = re.search(r"\d+(\.\d+)?", str(text or ""))
    return float(m.group(0)) if m else None


def _geo(asset: dict):
    low = {str(k).strip().lower(): v for k, v in asset.items()}

    def first(names):
        for n in names:
            x = _fl(low.get(n))
            if x is not None:
                return x
        return None

    lat, lon = first(LAT_NAMES), first(LON_NAMES)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def _costs(h: pd.DataFrame):
    """(mean repair cost per asset_id, mean over all maintenance records)."""
    if h.empty or not {"record_type", "repair_cost", "asset_id"}.issubset(h.columns):
        return {}, None
    m = h[h["record_type"].astype(str).str.strip().str.lower() == "maintenance"].copy()
    m["c"] = pd.to_numeric(m["repair_cost"].astype(str).str.replace(r"[^\d.\-]", "", regex=True), errors="coerce")
    m = m.dropna(subset=["c"])
    if m.empty:
        return {}, None
    per = m.groupby(m["asset_id"].astype(str).str.strip().str.upper())["c"].mean().to_dict()
    return per, float(m["c"].mean())


def _reasons(res, cond, rul, crit, live_active):
    out = []
    age, life = res.get("asset_age_years"), res.get("design_life_years")
    if age is not None and life and age / life >= 0.8:
        out.append(f"Near end of design life ({age:g} of {life:g} years)")
    if rul is not None and rul <= 2:
        out.append(f"Remaining life about {rul:g} year(s)")
    if cond is not None and cond <= 50:
        out.append(f"Condition {cond:g}/100")
    if live_active:
        out.append(f"{live_active} open complaint(s)")
    if crit == "High":
        out.append("High criticality asset")
    return out or ["No major risk drivers"]


# ---------------- core computation ----------------
def _compute() -> dict:
    key = tuple(_mtime(p) for p in (g.A_CSV, g.B_CSV, g.C_CSV, str(f.STATUS_FILE)))
    if _cache["key"] == key and _cache["val"] is not None:
        return _cache["val"]

    warnings = []
    try:
        a = pd.read_csv(g.A_CSV)
        a.columns = a.columns.str.strip()
    except Exception as e:
        return {"assets": [], "roads": [], "has_coords": False, "overall_cost": None,
                "warnings": [f"Could not read A_assets.csv (looked in {g.A_CSV}): {e}"]}

    h = f._history()
    c, status = f._rows(), f._load_status()
    per_cost, overall = _costs(h)

    live = {}
    if not c.empty and "matched_asset_id" in c.columns:
        for _, r in c.iterrows():
            aid = str(r.get("matched_asset_id", "")).strip().upper()
            if not aid:
                continue
            st = status.get(r.get("complaint_id", ""), {}).get("status", "Open")
            d = live.setdefault(aid, {"active": 0, "total": 0})
            d["total"] += 1
            if st != "Resolved":
                d["active"] += 1

    assets, skipped = [], 0
    for rec in a.to_dict(orient="records"):
        try:
            aid = str(g.pick(rec, "asset_id", "id", default="")).strip()
            if not aid:
                continue
            res = g.calculate_rul_and_risk(rec, h, False, "", g.C_CSV)
            rul = _first_number(res.get("estimated_remaining_life"))
            cond = _first_number(res.get("current_condition_score"))
            life = max(rul if rul is not None else 5.0, 0.1)
            p12 = 1 - math.exp(-math.log(2) * (1.0 / life) ** 2)        # same curve as the page's 12-month chart
            crit = str(g.pick(rec, "criticality", default="Medium")).strip().title()
            cw = g.CRIT_WEIGHT.get(crit, 60) / 100.0
            lat, lon = _geo(rec)
            lv = live.get(aid.upper(), {"active": 0, "total": 0})

            if aid.upper() in per_cost:
                cost, src = per_cost[aid.upper()], "this asset's repair history"
            elif overall is not None:
                cost, src = overall, "network average repair cost"
            else:
                cost, src = None, "no cost data"

            assets.append({
                "asset_id": aid,
                "asset_type": res.get("asset_type", ""),
                "location": str(g.pick(rec, "location", default="Unknown")).strip(),
                "criticality": crit,
                "age": res.get("asset_age_years"),
                "design_life": res.get("design_life_years"),
                "condition": cond,
                "rul": rul,
                "risk_score": res.get("priority_score", 0),
                "risk_level": res.get("priority_level", "Low"),
                "fail_prob_12m": round(p12 * 100, 1),
                "benefit": round(p12 * cw * 100, 2),
                "active_complaints": lv["active"],
                "total_complaints": lv["total"],
                "est_cost": None if cost is None else int(round(cost)),
                "cost_source": src,
                "lat": lat, "lon": lon,
                "reasons": _reasons(res, cond, rul, crit, lv["active"]),
            })
        except Exception:
            skipped += 1
            traceback.print_exc()

    assets.sort(key=lambda x: (-float(x["risk_score"] or 0), -x["fail_prob_12m"]))

    roads = {}
    for x in assets:
        r = roads.setdefault(x["location"], {"location": x["location"], "assets": 0, "risk_sum": 0.0,
                                             "max_risk": 0.0, "active_complaints": 0, "top_asset": ""})
        r["assets"] += 1
        r["risk_sum"] += float(x["risk_score"] or 0)
        r["active_complaints"] += x["active_complaints"]
        if float(x["risk_score"] or 0) >= r["max_risk"]:
            r["max_risk"], r["top_asset"] = float(x["risk_score"] or 0), x["asset_id"]
    road_list = []
    for r in roads.values():
        road_list.append({"location": r["location"], "assets": r["assets"],
                          "avg_risk": round(r["risk_sum"] / r["assets"], 1), "max_risk": round(r["max_risk"], 1),
                          "top_asset": r["top_asset"], "active_complaints": r["active_complaints"],
                          "level": "High" if r["max_risk"] >= 65 else "Medium" if r["max_risk"] >= 40 else "Low"})
    road_list.sort(key=lambda r: (-r["max_risk"], -r["active_complaints"]))

    with_geo = sum(1 for x in assets if x["lat"] is not None)
    if skipped:
        warnings.append(f"{skipped} asset(s) could not be scored and were skipped (see the server log).")
    if not assets:
        warnings.append("A_assets.csv has no usable assets.")
    if assets and with_geo == 0:
        warnings.append("A_assets.csv has no latitude/longitude columns, so the map shows road hotspots only. "
                        "Add 'latitude' and 'longitude' columns to plot assets.")
    elif assets and with_geo < len(assets):
        warnings.append(f"{len(assets) - with_geo} asset(s) have no valid coordinates and are not on the map.")
    if overall is None:
        warnings.append("No repair cost data in B_history.csv; the budget optimizer uses the fallback cost you enter.")

    out = {"assets": assets, "roads": road_list, "has_coords": with_geo > 0,
           "overall_cost": overall, "warnings": warnings}
    _cache["key"], _cache["val"] = key, out
    return out


def _knapsack(items, budget):
    """0/1 knapsack (vectorised). Costs are rounded UP to a unit, so the real total never exceeds the budget."""
    n = len(items)
    unit = max(100, -(-int(budget) // 20000))
    cap = int(budget) // unit
    w = [max(1, -(-int(round(i["cost"])) // unit)) for i in items]
    v = np.array([i["benefit"] for i in items], dtype=float)
    best = np.zeros(cap + 1)
    keep = np.zeros((n, cap + 1), dtype=bool)
    for i in range(n):
        wi = w[i]
        if wi > cap:
            continue
        cand = best[:cap + 1 - wi] + v[i]          # built from the old row, so each item is used once
        take = cand > best[wi:]
        keep[i, wi:] = take
        best[wi:] = np.where(take, cand, best[wi:])
    chosen, cc = [], cap
    for i in range(n - 1, -1, -1):
        if keep[i, cc]:
            chosen.append(i)
            cc -= w[i]
    return chosen


def _plan(d: dict, budget: float, fallback: float) -> dict:
    items, fb_used = [], 0
    for x in d["assets"]:
        if x["benefit"] <= 0:
            continue
        cost = x["est_cost"] if x["est_cost"] else fallback
        src = x["cost_source"] if x["est_cost"] else "fallback assumption"
        if not x["est_cost"]:
            fb_used += 1
        items.append({**x, "cost": cost, "cost_source": src})

    total_benefit = sum(i["benefit"] for i in items)
    chosen_idx = _knapsack(items, budget) if items else []
    selected = [items[i] for i in chosen_idx]
    selected.sort(key=lambda x: -float(x["risk_score"] or 0))
    spent = sum(i["cost"] for i in selected)
    benefit = sum(i["benefit"] for i in selected)

    # baseline: simply fix the highest-risk assets first until the money runs out
    left, base = budget, 0.0
    for i in sorted(items, key=lambda x: (-float(x["risk_score"] or 0), -x["fail_prob_12m"])):
        if i["cost"] <= left:
            left -= i["cost"]
            base += i["benefit"]

    return {
        "budget": budget, "spent": round(spent), "remaining": round(budget - spent),
        "selected": selected, "selected_count": len(selected), "total_candidates": len(items),
        "deferred_count": len(items) - len(selected),
        "benefit": round(benefit, 1),
        "benefit_pct": round(100 * benefit / total_benefit, 1) if total_benefit else 0,
        "baseline_benefit": round(base, 1),
        "improvement_pct": round(100 * (benefit - base) / base, 1) if base > 0 else None,
        "fallback_used": fb_used,
    }


# ---------------- endpoints ----------------
@router.get("/insights.js")
def insights_js():
    return FileResponse(g.BASE_DIR / "insights.js", media_type="application/javascript; charset=utf-8")


@router.get("/api/insights/risk")
def risk(top: int = 20):
    try:
        d = _compute()
        top = max(1, min(int(top), 200))
        return {"assets": d["assets"][:top], "total_assets": len(d["assets"]), "warnings": d["warnings"]}
    except Exception as e:
        traceback.print_exc()
        return {"assets": [], "total_assets": 0, "warnings": [f"Risk ranking crashed: {type(e).__name__}: {e}"]}


@router.get("/api/insights/budget")
def budget(budget: float = 500000, fallback_cost: float = 15000):
    try:
        if not (budget > 0) or not math.isfinite(budget):
            return {"error": "Budget must be a number above zero."}
        fb = fallback_cost if (fallback_cost and math.isfinite(fallback_cost) and fallback_cost > 0) else 15000
        d = _compute()
        plan = _plan(d, float(budget), float(fb))
        plan["warnings"] = d["warnings"]
        if d["assets"] and plan["selected_count"] == 0:
            plan["warnings"] = plan["warnings"] + ["The budget is smaller than the cheapest repair, so nothing was selected."]
        return plan
    except Exception as e:
        traceback.print_exc()
        return {"error": f"Budget optimizer crashed: {type(e).__name__}: {e}"}


@router.get("/api/insights/map")
def map_data():
    try:
        d = _compute()
        pts = [{k: x[k] for k in ("asset_id", "asset_type", "location", "risk_score", "risk_level",
                                  "fail_prob_12m", "active_complaints", "lat", "lon")}
               for x in d["assets"] if x["lat"] is not None]
        return {"has_coords": d["has_coords"], "points": pts, "roads": d["roads"], "warnings": d["warnings"]}
    except Exception as e:
        traceback.print_exc()
        return {"has_coords": False, "points": [], "roads": [],
                "warnings": [f"Map data crashed: {type(e).__name__}: {e}"]}


# Cheap change-detector the page polls to know when to refresh (keep this at the bottom).
@router.get("/api/insights/version")
def version():
    return {"version": "|".join(str(_mtime(p)) for p in (g.A_CSV, g.B_CSV, g.C_CSV, str(f.STATUS_FILE)))}