#! /usr/bin/env python3

import argparse
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_args():
  parser = argparse.ArgumentParser(
    description="Run UVMF repo smoke checks for generation, regeneration safety, and optional Verible lint."
  )
  parser.add_argument(
    "--with-verible",
    action="store_true",
    help="Also run scripts/run_verible_lint.py over the repository.",
  )
  return parser.parse_args()


def run_step(command):
  print("==> {}".format(" ".join(command)))
  result = subprocess.run(command, cwd=REPO_ROOT, check=False)
  if result.returncode != 0:
    return result.returncode
  return 0


def main():
  args = parse_args()
  steps = [
    [sys.executable, "-m", "compileall", "-q", "scripts", "templates/python/uvmf_gen.py", "templates/python/uvmf_yaml"],
    [sys.executable, str(REPO_ROOT / "scripts" / "test_regeneration_safety.py")],
    [sys.executable, str(REPO_ROOT / "scripts" / "test_soc_integration.py")],
    ["git", "diff", "--check"],
  ]
  if args.with_verible:
    steps.append([sys.executable, str(REPO_ROOT / "scripts" / "run_verible_lint.py")])

  for step in steps:
    status = run_step(step)
    if status != 0:
      return status

  print("Smoke checks passed.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
