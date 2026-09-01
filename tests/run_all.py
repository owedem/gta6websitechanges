"""Run every ultracode test. No network, no Discord, no real state touched.

    python tests/run_all.py

Each test loads ultracode.py with a throwaway STATE_DIR and stubbed HTTP, so it
is safe to run any time — including against a checkout with live snapshots.
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

failed = []
for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
    name = os.path.basename(path)
    print(f"\n=== {name} " + "=" * (60 - len(name)))
    if subprocess.run([sys.executable, path]).returncode != 0:
        failed.append(name)

print("\n" + "=" * 64)
if failed:
    print("FAILED: " + ", ".join(failed))
    sys.exit(1)
print("All suites passed.")
