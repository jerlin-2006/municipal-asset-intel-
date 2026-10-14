import os
import io
import json
import re
import glob
import time
from PIL import Image, ImageOps
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# --- 1. DIRECT ABSOLUTE PATHS ---
PROJECT_DIR = r"C:\Users\SANCHAYAA\OneDrive\Desktop\SmartCityProject"
ASSETS_CSV = os.path.join(PROJECT_DIR, "A_assets.csv")
HISTORY_CSV = os.path.join(PROJECT_DIR, "B_history.csv")
REPORT_CSV = os.path.join(PROJECT_DIR, "C_report.csv")

DOWNLOADS_DIR = os.path.join(os.environ.get("USERPROFILE", r"C:\Users\SANCHAYAA"), "Downloads")
whatsapp_files = glob.glob(os.path.join(DOWNLOADS_DIR, "*WhatsApp*.*"))

if not whatsapp_files:
    print(f"❌ No WhatsApp images found in {DOWNLOADS_DIR}")
    exit(1)

# Point directly to the School Road slip
# Sort all WhatsApp images from newest to oldest
whatsapp_files = sorted(whatsapp_files, key=os.path.getmtime, reverse=True)

print("\n📸 Recent WhatsApp Images Found in Downloads:")
for idx, f in enumerate(whatsapp_files[:6]):
    print(f"  [{idx + 1}] {os.path.basename(f)}")

choice = input("\nEnter image number to test (1 to 6, default 2): ").strip()
choice_idx = int(choice) - 1 if choice.isdigit() and 1 <= int(choice) <= len(whatsapp_files[:6]) else 1

IMAGE_PATH = whatsapp_files[choice_idx]
print(f"👉 Selected Image: {os.path.basename(IMAGE_PATH)}\n")

print("==================================================")
print("🚀 RUNNING PIPELINE DIRECTLY VIA PYTHON")
print(f"📁 Image Path   : {IMAGE_PATH}")
print(f"📁 Assets File  : {ASSETS_CSV}")
print("==================================================")

# --- 2. INITIALIZE GEMINI ---
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    print("❌ GEMINI_API_KEY not found in .env!")
    exit(1)

client = genai.Client(api_key=api_key)

# Open and normalize image
img = Image.open(IMAGE_PATH).convert("RGB")
img = ImageOps.exif_transpose(img)
if max(img.size) > 1600:
    img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)

prompt = """
Examine the handwritten citizen complaint in this image.
The text is written in Tamil (தமிழ்) or English. Read it regardless of orientation.

RULES:
- "வடக்கு" / "வடக்கு தெரு" / "வடக்கு சாலை" -> location: "North Road"
- "பள்ளி" / "பள்ளி சாலை" -> location: "School Road"
- "சந்தை" / "சந்தை சாலை" -> location: "Market Road"
- "கோவில்" / "கோவில் சாலை" -> location: "Temple Road"

- "குடிநீர்" / "குழாய்" -> asset_type: "Water"
- "சாக்கடை" / "வடிகால்" -> asset_type: "Drainage"
- "கசிவு" -> issue: "Water leakage"
- "உடைப்பு" / "உடைந்து" / "வெளியேறி" -> issue: "Pipe burst"
- "அடைப்பு" -> issue: "Blockage"

Output strictly as a valid JSON object:
{
  "transcription": "<Tamil text>",
  "translation": "<English translation>",
  "location": "<Exact matched road name>",
  "asset_type": "<Water or Drainage>",
  "issue": "<Defect type>"
}
"""

# Specifically target the lite endpoint which has vision enabled and available quota
candidate_models = ["gemini-flash-lite-latest", "gemini-flash-latest"]
extracted = None

print("⏳ Calling Gemini Vision API...")

for m in candidate_models:
    for attempt in range(1, 4):
        try:
            print(f"   Trying model: {m} (Attempt {attempt})...")
            resp = client.models.generate_content(
                model=m,
                contents=[img, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            if resp and resp.text:
                cleaned = resp.text.strip()
                cleaned = re.sub(r"^```json\s*", "", cleaned)
                cleaned = re.sub(r"\s*```$", "", cleaned)
                extracted = json.loads(cleaned)
                print(f"✅ Success with {m}!")
                break
        except Exception as e:
            err_msg = str(e)
            print(f"⚠️ {m} attempt {attempt} failed: {err_msg}")
            if "503" in err_msg or "UNAVAILABLE" in err_msg:
                print("   Temporary 503 capacity spike detected. Waiting 3 seconds before retry...")
                time.sleep(3)
            else:
                break
    if extracted:
        break

if not extracted:
    print("\n❌ Could not extract data from image. No candidate models succeeded.")
    exit(1)

print("\n🎯 EXTRACTED OCR RESULT:")
print("Transcription :", extracted.get("transcription"))
print("Translation   :", extracted.get("translation"))
print("Detected Road :", extracted.get("location"))
print("Asset Type    :", extracted.get("asset_type"))
print("Issue         :", extracted.get("issue"))
print("--------------------------------------------------")

# --- 3. PROCESS COMPLAINT DIRECTLY INTO C_report.csv ---
import generate_C

synth_text = f"{extracted.get('asset_type')} issue: {extracted.get('issue')} reported on {extracted.get('location')}."
print(f"📝 Synthesized Narrative: '{synth_text}'")

result = generate_C.process_new_complaint(
    complaint_id="CMP-TEST-999",
    complaint_text=synth_text,
    assets_csv_path=ASSETS_CSV,
    history_csv_path=HISTORY_CSV,
    output_c_path=REPORT_CSV
)

print("\n🎉 PIPELINE EXECUTION COMPLETE!")
print(f"🎯 Matched Asset ID : {result.get('matched_asset_id')}")
print(f"🎯 Extracted Issue   : {result.get('extracted_issue')}")
print(f"🎯 Remaining Life    : {result.get('estimated_remaining_life')}")
print("==================================================")