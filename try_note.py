"""Send one or more operator notes to the API and print how each was interpreted.

Usage:
  python try_note.py "Do not charge the battery between 2 PM and 4 PM."
  python try_note.py "note one" "note two"            (up to 3 notes)
  python try_note.py --local "your note"              (test http://localhost:8080 instead)
"""
import json
import sys
import urllib.request

LIVE = "https://biddut-bivrat.vercel.app"
LOCAL = "http://localhost:8080"
SAMPLES = "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"

args = sys.argv[1:]
base = LOCAL if "--local" in args else LIVE
notes = [a for a in args if a != "--local"]
if not 1 <= len(notes) <= 3:
    sys.exit(__doc__)

# Reuse the scenario (24 hours + battery) from the first public sample; only the notes change.
scenario = json.load(open(SAMPLES, encoding="utf-8"))["cases"][0]["input"]
body = dict(scenario, scenario_id="TRY-NOTE", operator_notes=notes)

req = urllib.request.Request(
    base + "/optimize-energy",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=40) as resp:
    out = json.loads(resp.read())

print(f"Sent to {base}\n")
for note, d in zip(notes, out["directive_interpretation"]):
    print(f"NOTE : {note}")
    print(f"  ->  {d['directive_type']}  {d['structured_adjustment']}\n")
print(f"Total cost: {out['total_cost_bdt']} BDT")
