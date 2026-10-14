"""
Smart-city complaint pipeline:  complaint (text / photo) -> asset match -> RUL + priority -> C_report.csv
"""
import difflib
import io
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")          # works no matter which folder you launch uvicorn from

A_CSV = str(BASE_DIR / "A_assets.csv")
B_CSV = str(BASE_DIR / "B_history.csv")
C_CSV = str(BASE_DIR / "C_report.csv")

# Used only as a starting list; every road found in A_assets.csv is added automatically.
DEFAULT_ROADS = ["Temple Road", "North Road", "Market Road", "School Road", "5th Cross Road"]

# Tried in order. Override with GEMINI_MODELS="a,b,c" in .env
MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-flash-latest,gemini-flash-lite-latest"
).split(",") if m.strip()]

# Tamil keywords -> road (used to rescue OCR when the model returns "Unknown")
TAMIL_ROADS = {
    "North Road": ["வடக்கு"],
    "School Road": ["பள்ளி"],
    "Market Road": ["சந்தை"],
    "Temple Road": ["கோவில்", "கோயில்"],
    "Hospital Road": ["மருத்துவமனை", "ஆஸ்பத்திரி"],
    "Bus Stand Road": ["பேருந்து நிலைய", "பஸ் ஸ்டாண்ட்"],
}
DRAIN_WORDS = ["drain", "sewage", "sewer", "gutter", "silt", "choke", "overflow", "சாக்கடை", "வடிகால்"]
BLOCK_WORDS = ["block", "choke", "silt", "அடைப்பு"]
BURST_WORDS = ["burst", "rupture", "gush", "உடைப்பு", "உடைந்து", "வெளியேறி"]
REPEAT_WORDS = ["repeated", "recurring", "again", "frequent", "மீண்டும்", "அடிக்கடி"]

# Risk-model constants
FAIL_THRESHOLD = 25.0        # condition score at which an asset is considered "failed"
HALF_LIFE_DAYS = 730         # older incidents count less
FALSE_ALARM = {"no defect found", "not confirmed"}
CRIT_WEIGHT = {"High": 100, "Medium": 60, "Low": 30}

CREWS = {
    "Water": {"High": "Water Rapid Response Crew A", "Medium": "Water Maintenance Crew B", "Low": "Water Routine Patrol Crew"},
    "Drainage": {"High": "Drainage Emergency Crew A", "Medium": "Drainage Maintenance Crew B", "Low": "Drainage Routine Patrol Crew"},
}
ACTIONS = {
    "High": "Immediate priority inspection and pipeline reinforcement",
    "Medium": "Schedule preventive servicing within 14 days",
    "Low": "Log complaint; routine monitoring",
}

_client = None
_csv_lock = threading.Lock()


def get_client():
    """Create the Gemini client once and reuse it."""
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        _client = genai.Client(api_key=api_key) if api_key else False
    return _client or None


# ==========================================
# Helpers
# ==========================================
def pick(d: dict, *keys, default=None):
    """First non-empty, non-NaN value among keys (case-insensitive)."""
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip() != "":
            return v
    return default


def find_col(df: pd.DataFrame, names, fallback_idx=None):
    lower = {c.lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    if fallback_idx is not None and len(df.columns) > fallback_idx:
        return df.columns[fallback_idx]
    return None


def known_roads() -> list:
    """Default roads + every location that exists in A_assets.csv."""
    roads = list(DEFAULT_ROADS)
    try:
        df = pd.read_csv(A_CSV)
        df.columns = df.columns.str.strip()
        col = find_col(df, ["location"], 2)
        for r in df[col].dropna().astype(str).str.strip():
            if r and r.lower() not in {x.lower() for x in roads}:
                roads.append(r)
    except Exception:
        pass
    return roads


def normalise_location(loc, roads=None) -> Optional[str]:
    """'north street' / 'North Rd' / 'North road ' -> 'North Road'. Returns None for unknown."""
    if loc is None:
        return None
    s = str(loc).strip()
    if s.lower() in {"", "unknown", "none", "n/a", "null"}:
        return None
    roads = roads or known_roads()
    canon = re.sub(r"\b(street|st\.?|rd\.?)(?=\s|$)", "road", s, flags=re.I)
    canon = re.sub(r"\s+", " ", canon).strip().lower()
    for r in roads:
        if r.lower() == canon:
            return r
    for r in roads:                                   # "North" -> "North Road"
        if r.lower() == f"{canon} road":
            return r
    close = difflib.get_close_matches(canon, [r.lower() for r in roads], n=1, cutoff=0.8)
    if close:
        return next(r for r in roads if r.lower() == close[0])
    return s


def find_road_in_text(text: str, roads=None) -> Optional[str]:
    """Look for a known road (English or Tamil keyword) inside free text."""
    roads = roads or known_roads()
    t = (text or "").lower()
    for r in roads:
        if r.lower() in t:
            return r
    for r in roads:
        base = re.escape(r.lower().replace(" road", ""))
        if re.search(rf"\b{base}\s+(road|street|st\.?|rd\.?|lane)\b", t):
            return r
    for r, words in TAMIL_ROADS.items():
        if any(w in (text or "") for w in words):
            return next((x for x in roads if x.lower() == r.lower()), None)
    return None


def normalise_asset_type(t) -> str:
    return "Drainage" if "drain" in str(t).lower() else "Water"


def parse_install_date(value) -> pd.Timestamp:
    ts = pd.to_datetime(str(value), errors="coerce")
    if pd.notna(ts):
        return ts
    m = re.search(r"\d{4}", str(value))
    return pd.Timestamp(year=int(m.group(0)), month=1, day=1) if m else pd.Timestamp(year=2018, month=1, day=1)


# ==========================================
# 1. Pydantic models
# ==========================================
class ExtractedComplaint(BaseModel):
    location: str = Field(description="Road name from the complaint (e.g. North Road). Use 'Unknown' if not mentioned.")
    asset_type: str = Field(description="Must be strictly 'Water' or 'Drainage'")
    issue_type: str = Field(description="e.g. Water leakage, Blockage, Crack, Pipe burst")
    is_repeated: bool = Field(description="True if words like repeatedly, recurring, or again are present")


class OcrResult(BaseModel):
    transcription: str = Field(description="Text exactly as written in the image")
    translation: str = Field(description="English translation")
    location: str = Field(description="Exact road name from the allowed list, or 'Unknown'")
    asset_type: str = Field(description="'Water' or 'Drainage'")
    issue: str = Field(description="Water leakage, Pipe burst, Blockage, Crack ...")
    is_repeated: bool = Field(description="True if the note says the problem keeps happening")


# ==========================================
# 2. Gemini call with retry / model fallback
# ==========================================
def call_gemini_json(contents, schema):
    """Returns a dict, or None if every model failed."""
    client = get_client()
    if not client:
        return None
    for model in MODELS:
        for attempt in range(2):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=schema,
                        temperature=0,
                    ),
                )
                txt = (resp.text or "").strip()
                if not txt:
                    raise ValueError("empty response (possibly blocked by safety filter)")
                txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
                data = json.loads(txt)
                if isinstance(data, list):
                    data = data[0]
                print(f"   ✅ {model} OK")
                return data
            except Exception as e:
                msg = str(e)
                print(f"   ⚠️ {model} failed (attempt {attempt + 1}): {msg[:200]}")
                transient = any(c in msg for c in ("429", "500", "503", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded"))
                if transient and attempt == 0:
                    time.sleep(2)
                    continue
                break
    return None


# ==========================================
# 3. Text extraction
# ==========================================
def offline_extract(complaint_text: str) -> ExtractedComplaint:
    raw = complaint_text or ""
    text = raw.lower()
    asset_type = "Drainage" if any(w in text or w in raw for w in DRAIN_WORDS) else "Water"
    location = find_road_in_text(raw) or "Unknown"

    issue_type = "Water leakage"
    if any(w in text or w in raw for w in BLOCK_WORDS):
        issue_type = "Blockage"
    elif any(w in text or w in raw for w in BURST_WORDS):
        issue_type = "Pipe burst"
    elif "crack" in text:
        issue_type = "Crack"

    is_rep = any(w in text or w in raw for w in REPEAT_WORDS)
    return ExtractedComplaint(location=location, asset_type=asset_type, issue_type=issue_type, is_repeated=is_rep)


def extract_complaint_info(complaint_text: str) -> ExtractedComplaint:
    roads = known_roads()
    if get_client():
        prompt = f"""
        Extract infrastructure details from this citizen complaint:
        "{complaint_text}"

        Asset type must be strictly "Water" or "Drainage".
        Known roads: {", ".join(roads)}. Use the exact road name; use "Unknown" if none is mentioned.
        """
        data = call_gemini_json(prompt, ExtractedComplaint)
        if data:
            try:
                result = ExtractedComplaint.model_validate(data)
                result.asset_type = normalise_asset_type(result.asset_type)
                result.location = (normalise_location(result.location, roads)
                                   or find_road_in_text(complaint_text, roads) or "Unknown")
                return result
            except Exception as e:
                print(f"   ⚠️ bad model output: {e}")
        print("   [API Bypass] All models failed, using offline parser.")
    else:
        print("   [Offline Parser] No API key, using deterministic rules...")
    return offline_extract(complaint_text)


# ==========================================
# 4. Match extracted data to assets
# ==========================================
def match_asset(extracted: ExtractedComplaint, df_assets: pd.DataFrame):
    roads = known_roads()
    location = normalise_location(extracted.location, roads)
    if location is None:
        return None, "Unmatched (No location)"

    df = df_assets.copy()
    df.columns = df.columns.str.strip()
    loc_col = find_col(df, ["location"], 2) or df.columns[0]
    type_col = find_col(df, ["asset_type", "type"], 1)

    loc_series = df[loc_col].astype(str).str.strip().str.lower()
    type_series = df[type_col].astype(str).str.strip().str.lower()
    want_loc = location.lower()
    want_type = extracted.asset_type.strip().lower()

    matched = df[(loc_series == want_loc) & (type_series == want_type)]
    if len(matched) == 1:
        return matched.iloc[0].to_dict(), "High"
    if len(matched) > 1:
        return matched.iloc[0].to_dict(), "Medium (Multiple Matches Found)"

    partial = df[loc_series.str.contains(want_loc, na=False, regex=False) & (type_series == want_type)]
    if len(partial) > 0:
        return partial.iloc[0].to_dict(), "Medium (Partial location Match)"
    return None, f"Unmatched (no {extracted.asset_type} asset on {location})"


# ==========================================
# 5. C_report helpers
# ==========================================
def _read_c(path: str) -> pd.DataFrame:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            return pd.read_csv(path, dtype=str, keep_default_na=False)
        except (pd.errors.EmptyDataError, pd.errors.ParserError):
            pass
    return pd.DataFrame()


def _find_logged(complaint_id: str, path: str) -> Optional[dict]:
    df = _read_c(path)
    if df.empty or "complaint_id" not in df.columns:
        return None
    hit = df[df["complaint_id"].astype(str) == str(complaint_id)]
    return hit.iloc[0].to_dict() if len(hit) else None


def _append_row(row: dict, path: str):
    new_df = pd.DataFrame([row]).astype(str)
    old = _read_c(path)
    out = pd.concat([old, new_df], ignore_index=True) if not old.empty else new_df
    out.fillna("").to_csv(path, index=False)


def _recent_complaints(asset_id: str, path: str, days: int = 30) -> int:
    df = _read_c(path)
    if df.empty:
        return 0
    col = find_col(df, ["matched_asset_id", "asset_id"])
    if not col:
        return 0
    sub = df[df[col].str.strip() == asset_id]
    if "timestamp" not in sub.columns:
        return len(sub)
    ts = pd.to_datetime(sub["timestamp"], errors="coerce")
    return int((ts >= pd.Timestamp.now() - pd.Timedelta(days=days)).sum())


# ==========================================
# 6. RUL + priority model
# ==========================================
def calculate_rul_and_risk(asset: dict, df_history: pd.DataFrame, is_repeated: bool,
                           issue_type: str = "", output_c_path: str = C_CSV) -> dict:
    asset_id = str(pick(asset, "asset_id", "id", default="")).strip()
    asset_type = normalise_asset_type(pick(asset, "asset_type", "type", default="Water"))
    design_life = float(pick(asset, "design_life_years", "design_lifetime_years", default=30.0))
    initial = float(pick(asset, "initial_condition_score", default=85.0))
    criticality = str(pick(asset, "criticality", default="Medium")).strip().title()
    install = parse_install_date(pick(asset, "installation_date", "install_date", "installation_year", default="2018"))
    now = pd.Timestamp.now()
    age = max(0.0, (now - install).days / 365.25)
    last_maint = pick(asset, "last_maintenance_date", default="N/A")

    # ---- recency-weighted history from B ----
    fault_load = repair_weight = 0.0
    hist_complaints = hist_repairs = severe = confirmed = 0
    if df_history is not None and not df_history.empty:
        h = df_history.copy()
        h.columns = h.columns.str.strip()
        id_col = find_col(h, ["asset_id", "id"], 0)
        rt_col = find_col(h, ["record_type", "type"])
        sev_col = find_col(h, ["severity"])
        ts_col = find_col(h, ["timestamp", "date"])
        out_col = find_col(h, ["verified_outcome", "outcome"])
        h = h[h[id_col].astype(str).str.strip().str.upper() == asset_id.upper()]

        for _, r in h.iterrows():
            rtype = str(r[rt_col]).strip().lower() if rt_col else "complaint"
            if rtype == "complaint":
                hist_complaints += 1
            if rtype == "maintenance":
                hist_repairs += 1
            outcome = str(r[out_col]).strip().lower() if out_col else ""
            if outcome in FALSE_ALARM:               # false alarms don't wear the asset
                continue
            ts = pd.to_datetime(r[ts_col], errors="coerce") if ts_col else pd.NaT
            days = (now - ts).days if pd.notna(ts) else HALF_LIFE_DAYS
            w = 0.5 ** (max(days, 0) / HALF_LIFE_DAYS)
            m = re.search(r"\d+(\.\d+)?", str(r[sev_col])) if sev_col else None
            sev = min(5.0, max(1.0, float(m.group(0)))) if m else 3.0
            confirmed += 1
            fault_load += w * sev / 5.0
            if sev >= 4:
                severe += 1
            if rtype == "maintenance":
                repair_weight += w

    active = _recent_complaints(asset_id, output_c_path)   # complaints already logged in the last 30 days

    # ---- condition score ----
    age_pen = 40.0 * min(1.0, age / design_life)
    hist_pen = min(35.0, 3.0 * fault_load)
    repair_bonus = min(8.0, 2.0 * repair_weight)
    active_pen = min(10.0, 2.5 * active) + (5.0 if is_repeated else 0.0)
    condition = initial - age_pen - hist_pen + repair_bonus
    condition = min(condition, initial) - active_pen
    condition = max(5.0, min(100.0, condition))

    # ---- remaining useful life ----
    nominal_rate = max((initial - FAIL_THRESHOLD) / design_life, 0.5)       # points / year
    observed_rate = (initial - condition) / max(age, 1.0)
    rate = max(nominal_rate, observed_rate)
    rul_by_condition = (condition - FAIL_THRESHOLD) / rate if condition > FAIL_THRESHOLD else 0.0
    rul_by_design = max(0.0, design_life - age)
    rul = max(0.5, min(rul_by_condition, rul_by_design))

    # ---- priority ----
    issue = (issue_type or "").lower()
    score = (0.40 * (100 - condition)
             + 0.35 * 100 * max(0.0, 1 - rul / 15.0)
             + 0.15 * CRIT_WEIGHT.get(criticality, 60)
             + (5 if is_repeated else 0)
             + min(10, 2 * active)
             + (10 if "burst" in issue else 3 if "block" in issue else 0))
    score = int(round(max(0, min(100, score))))
    level = "High" if score >= 65 else "Medium" if score >= 40 else "Low"

    return {
        "asset_type": asset_type,
        "previous_related_complaints": hist_complaints + active,
        "last_maintenance_date": last_maint,
        "current_condition_score": f"{int(round(condition))}/100",
        "estimated_remaining_life": f"{round(rul, 1)} years",
        "priority_score": score,
        "priority_level": level,
        "risk_level": level,
        "recommended_action": ACTIONS[level],
        "assigned_crew": CREWS[asset_type][level],
        "initial_condition_score": round(initial, 1),
        "asset_age_years": round(age, 2),
        "design_life_years": round(design_life, 1),
        "degradation_rate": round(rate, 3),
        "prediction_basis": (f"Age {age:.1f}y of {design_life:.0f}y design life; {confirmed} confirmed faults "
                             f"({severe} severe, recency-weighted); {active + 1} complaint(s) in last 30 days"),
    }


# ==========================================
# 7. Pipeline core
# ==========================================
def _load_data(assets_csv_path=A_CSV, history_csv_path=B_CSV, df_assets=None, df_history=None):
    if df_assets is None:
        df_assets = pd.read_csv(assets_csv_path)
    if df_history is None:
        df_history = pd.read_csv(history_csv_path) if os.path.exists(history_csv_path) else pd.DataFrame()
    return df_assets, df_history


def _run_pipeline(complaint_id, complaint_text, extracted, df_assets, df_history, output_c_path):
    with _csv_lock:
        asset, confidence = match_asset(extracted, df_assets)
        row = {
            "complaint_id": complaint_id,
            "complaint_text": complaint_text,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "extracted_location": extracted.location,
            "extracted_asset_type": extracted.asset_type,
            "issue_type": extracted.issue_type,
            "is_repeated": extracted.is_repeated,
            "match_confidence": confidence,
            "matched_asset_id": "",
            "location": normalise_location(extracted.location) or "Unknown",
        }
        if asset is None:
            row.update({
                "priority_score": 0, "priority_level": "Unassigned", "risk_level": "Unknown",
                "assigned_crew": "Manual review desk",
                "recommended_action": "Manual review: no matching asset",
            })
        else:
            row["matched_asset_id"] = str(pick(asset, "asset_id", "id", default="")).strip()
            row["location"] = str(pick(asset, "location", default=row["location"])).strip()
            row.update(calculate_rul_and_risk(asset, df_history, extracted.is_repeated,
                                              extracted.issue_type, output_c_path))
        _append_row(row, output_c_path)

    print(f"✅ {complaint_id}: {confidence} | asset={row['matched_asset_id'] or '-'} | "
          f"priority={row.get('priority_score')} {row.get('priority_level')}")
    return row


def process_new_complaint(complaint_id: str, complaint_text: str,
                          assets_csv_path=A_CSV, history_csv_path=B_CSV, output_c_path=C_CSV,
                          df_assets=None, df_history=None):
    dup = _find_logged(complaint_id, output_c_path)
    if dup:
        print(f"⏭️  {complaint_id} already in {output_c_path}, returning existing row.")
        return dup

    df_assets, df_history = _load_data(assets_csv_path, history_csv_path, df_assets, df_history)
    extracted = extract_complaint_info(complaint_text)
    return _run_pipeline(complaint_id, complaint_text, extracted, df_assets, df_history, output_c_path)


def _ocr_prompt(roads, note):
    extra = f'\nThe citizen also typed this note: "{note}"\n' if note else ""
    return f"""
Examine the citizen complaint in this image (handwritten or printed). The text is Tamil (தமிழ்) and/or English.
{extra}
Allowed road names (use EXACTLY one of these, or "Unknown"): {", ".join(roads)}
"தெரு" (street) and "சாலை" (road) both mean "Road" in the list above.

Tamil hints:
- "வடக்கு" -> North Road
- "பள்ளி" -> School Road
- "சந்தை" -> Market Road
- "கோவில்" / "கோயில்" -> Temple Road
- "மருத்துவமனை" -> Hospital Road
- "பேருந்து நிலையம்" / "பஸ் ஸ்டாண்ட்" -> Bus Stand Road
- "குடிநீர்" / "குழாய்" / "தண்ணீர்" -> asset_type "Water"
- "சாக்கடை" / "வடிகால்" -> asset_type "Drainage"
- "கசிவு" -> issue "Water leakage"
- "உடைப்பு" / "உடைந்து" / "வெளியேறி" -> issue "Pipe burst"
- "அடைப்பு" -> issue "Blockage"
- "மீண்டும்" / "அடிக்கடி" -> is_repeated true

Fill: transcription (original text), translation (English), location, asset_type ("Water" or "Drainage"),
issue, is_repeated.
"""


def process_image_complaint(complaint_id: str, image_bytes: bytes, complaint_text: Optional[str] = None,
                            assets_csv_path=A_CSV, history_csv_path=B_CSV, output_c_path=C_CSV):
    dup = _find_logged(complaint_id, output_c_path)
    if dup:
        print(f"⏭️  {complaint_id} already in {output_c_path}, returning existing row.")
        return dup

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img).convert("RGB")      # WhatsApp photos are often rotated
    except Exception as e:
        raise ValueError(f"Could not read the uploaded image: {e}")
    if max(img.size) > 1600:
        img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)

    roads = known_roads()
    note = (complaint_text or "").strip()
    print("🔄 Running OCR...")
    data = call_gemini_json([img, _ocr_prompt(roads, note)], OcrResult) if get_client() else None
    df_assets, df_history = _load_data()

    if not data:
        # Never guess a location/asset: log it for manual review instead.
        print("⚠️ OCR failed on all models. Logging for manual review.")
        fallback = offline_extract(note) if note else ExtractedComplaint(
            location="Unknown", asset_type="Water", issue_type="Unknown", is_repeated=False)
        label = f"[image complaint - OCR failed] {note}".strip()
        return _run_pipeline(complaint_id, label, fallback, df_assets, df_history, output_c_path)

    try:
        ocr = OcrResult.model_validate(data)
    except Exception:
        ocr = OcrResult(transcription=str(data.get("transcription", "")), translation=str(data.get("translation", "")),
                        location=str(data.get("location", "Unknown")), asset_type=str(data.get("asset_type", "Water")),
                        issue=str(data.get("issue", "Water leakage")), is_repeated=bool(data.get("is_repeated", False)))

    blob = f"{ocr.transcription} {ocr.translation} {note}"
    location = normalise_location(ocr.location, roads) or find_road_in_text(blob, roads) or "Unknown"
    extracted = ExtractedComplaint(
        location=location,
        asset_type=normalise_asset_type(ocr.asset_type),
        issue_type=ocr.issue.strip() or "Water leakage",
        is_repeated=ocr.is_repeated or any(w in blob.lower() or w in blob for w in REPEAT_WORDS),
    )
    text = ocr.translation or ocr.transcription or note or \
        f"{extracted.asset_type} issue: {extracted.issue_type} reported on {extracted.location}."
    print(f"📝 Complaint text: '{text}'")
    return _run_pipeline(complaint_id, text, extracted, df_assets, df_history, output_c_path)


# ==========================================
# Test suite
# ==========================================
if __name__ == "__main__":
    tests = [
        ("CMP-0101", "Water has been leaking repeatedly near North Road for the past two days."),
        ("CMP-0102", "Low water pressure and continuous underground pipe seepage noticed along Market Road."),
        ("CMP-0103", "Severe pipe burst near School Road, fresh drinking water gushing onto the sidewalk."),
        ("CMP-0104", "Minor water leakage around the distribution line near Temple Road."),
        ("CMP-0105", "The main drainage line on North Road is completely choked and overflowing onto the street."),
        ("CMP-0106", "Heavy silt accumulation and severe sewage blockage in the drain line near Market Road."),
        ("CMP-0107", "Pipe leaking again near Hospital Road."),
        ("CMP-0108", "Burst pipe on Bus Stand Street."),
    ]
    print("\n==========================================\nRUNNING PIPELINE TEST\n==========================================\n")
    a, b = _load_data()
    for cid, txt in tests:
        process_new_complaint(cid, txt, df_assets=a, df_history=b)
