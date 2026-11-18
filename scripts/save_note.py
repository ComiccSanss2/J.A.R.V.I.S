"""Example local script: appends a timestamped note to data/notes.txt."""
import sys
from datetime import datetime
from pathlib import Path

note = " ".join(sys.argv[1:]).strip()
if not note:
    print("No note provided", file=sys.stderr)
    sys.exit(1)

path = Path(__file__).resolve().parent.parent / "data" / "notes.txt"
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as f:
    f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] {note}\n")
print(f"Note saved ({len(note)} characters)")
