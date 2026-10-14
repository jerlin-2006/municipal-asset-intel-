"""
Usage:  python test_ocr.py "path\\to\\photo.jpeg"
Step 1 lists the models your key can call. Step 2 runs the REAL pipeline OCR (same code the website uses)
and writes to C_test.csv so your real C_report.csv is untouched.
"""
import os
import sys
from pathlib import Path

import generate_C as g

key = os.getenv("GEMINI_API_KEY")
print("API key loaded:", bool(key))
if not key:
    sys.exit("Put GEMINI_API_KEY=... in a .env file next to these scripts.")

client = g.get_client()

# 1. Which models can YOUR key actually call?
available = []
for m in client.models.list():
    if "generateContent" in (getattr(m, "supported_actions", None) or []):
        available.append(m.name.replace("models/", ""))
print("\nFlash models available to your key:")
print("\n".join(f"  {n}" for n in available if "flash" in n) or "  (none found)")
missing = [m for m in g.MODELS if m not in available]
if missing:
    print(f"\n⚠️ These configured models are NOT available to your key: {missing}\n"
          f"   Set GEMINI_MODELS=<name1>,<name2> in .env using names from the list above.")

# 2. Real pipeline OCR
path = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\SANCHAYAA\Downloads\WhatsApp Image 2026-10-13 at 10.26.48 PM.jpeg"
if not Path(path).exists():
    sys.exit(f"Image not found: {path}")

out = str(Path(g.BASE_DIR) / "C_test.csv")
if os.path.exists(out):
    os.remove(out)

row = g.process_image_complaint("TEST-OCR", Path(path).read_bytes(), output_c_path=out)
print("\n----- RESULT -----")
for k in ("complaint_text", "extracted_location", "extracted_asset_type", "issue_type",
          "match_confidence", "matched_asset_id", "estimated_remaining_life", "priority_score", "assigned_crew"):
    print(f"{k:26}: {row.get(k)}")
