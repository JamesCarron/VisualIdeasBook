import time
from pathlib import Path
from models import EntryStore
from dedup import deduplicate
from latex_gen import generate_pdf

s = EntryStore(Path("data/entries.json"))
kept, _ = deduplicate(s.all_entries())

print("=== First run (should compile all) ===")
t0 = time.time()
generate_pdf(kept, Path("output/archive.pdf"), combined=False, store=s)
elapsed1 = time.time() - t0
print(f"Took {elapsed1:.1f}s")

print("\n=== Second run (should skip all, no changes) ===")
t0 = time.time()
generate_pdf(kept, Path("output/archive.pdf"), combined=False, store=s)
elapsed2 = time.time() - t0
print(f"Took {elapsed2:.1f}s")
if elapsed2 < elapsed1 / 2:
    print(f"Speedup: {elapsed1/elapsed2:.1f}x faster — change detection works!")
else:
    print("Warning: second run wasn't significantly faster; check logs")
