r"""Build an isolated VEDA demo database without touching the working database.

Usage:
  .venv\Scripts\python.exe tools\build_demo_database.py
  .venv\Scripts\python.exe tools\build_demo_database.py --reset
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = (ROOT / "data" / "demo-v040").resolve()
LIVE_ROOT = (ROOT / "data").resolve()


def safe_target(value: str | None) -> Path:
    target = Path(value).resolve() if value else DEMO_ROOT
    if target == LIVE_ROOT or LIVE_ROOT not in target.parents:
        raise SystemExit("Demo target must be a child of " + str(LIVE_ROOT))
    if not target.name.lower().startswith("demo-"):
        raise SystemExit("Demo directory name must start with 'demo-'")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="isolated directory under data/ named demo-*")
    parser.add_argument("--reset", action="store_true",
                        help="replace only the validated isolated demo directory")
    args = parser.parse_args()
    target = safe_target(args.output)
    if target.exists() and any(target.iterdir()):
        if not args.reset:
            raise SystemExit(str(target) + " already exists; pass --reset to replace only this demo copy")
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["VEDA_DATA_DIR"] = str(target)
    # The builder is reproducible and never consumes any reasoning-provider credit.
    env["VEDA_DETERMINISTIC_ONLY"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    command = [str(ROOT / ".venv" / "Scripts" / "python.exe"),
               str(ROOT / "tools" / "verify_slice.py")]
    result = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if result.returncode:
        print("Demo build stopped with exit code", result.returncode)
        print("The isolated diagnostic database was retained at", target)
        return result.returncode
    seed = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"),
         str(ROOT / "tools" / "seed_demo_execution_proof.py")],
        cwd=ROOT, env=env, check=False)
    if seed.returncode:
        print("Execution-proof demo seeding failed with exit code", seed.returncode)
        print("The isolated diagnostic database was retained at", target)
        return seed.returncode
    with sqlite3.connect(target / "veda.db") as connection:
        activity_count = connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        typed_count = connection.execute(
            "SELECT COUNT(*) FROM files WHERE schema_name IS NOT NULL").fetchone()[0]
        certificate_count = connection.execute(
            "SELECT COUNT(*) FROM actuals_certificates WHERE status='admissible'").fetchone()[0]
    if not activity_count or typed_count < 4 or not certificate_count:
        print("Demo verification failed: activities=", activity_count,
              "typed sources=", typed_count, "admissible certificates=", certificate_count)
        print("The isolated diagnostic database was retained at", target)
        return 2
    (target / ".veda-demo").write_text(
        "VEDA 0.4 isolated demo database\n", encoding="utf-8")
    print("Clean isolated demo database:", target / "veda.db")
    print("Verified:", activity_count, "schedule activities and", typed_count,
          "typed execution sources;", certificate_count, "admissible Actuals Certificate")
    print("PowerShell launch: $env:VEDA_DATA_DIR='" + str(target) + "'; .\\start_veda.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
