#! /usr/bin/env python3

import argparse
from pathlib import Path
import re
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_VERSION = "0.1.0"
PROJECT_RELEASE = "v{}".format(PROJECT_VERSION)
UPSTREAM_UVMF_RELEASE = "2023.4_2"


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


def check_release_metadata():
  release_info = {}
  for line in (REPO_ROOT / "release.INFO").read_text(encoding="utf-8").splitlines():
    if not line or line.startswith("#"):
      continue
    if "=" not in line:
      raise ValueError("release.INFO contains an invalid line: {!r}".format(line))
    key, value = line.split("=", 1)
    release_info[key] = value

  expected = {
    "project_release": PROJECT_RELEASE,
    "upstream_uvmf_release": UPSTREAM_UVMF_RELEASE,
    "release_tag": PROJECT_RELEASE,
  }
  for key, value in expected.items():
    if release_info.get(key) != value:
      raise ValueError("release.INFO {} must be {!r}".format(key, value))

  python_version = (REPO_ROOT / "templates" / "python" / "uvmf_version.py").read_text(encoding="utf-8")
  required_python_assignments = (
    'project_version = "{}"'.format(PROJECT_VERSION),
    'upstream_uvmf_version = "{}"'.format(UPSTREAM_UVMF_RELEASE),
    "version = upstream_uvmf_version",
  )
  for assignment in required_python_assignments:
    if assignment not in python_version:
      raise ValueError("uvmf_version.py is missing {!r}".format(assignment))

  sv_version = (REPO_ROOT / "uvmf_base_pkg" / "src" / "uvmf_version.svh").read_text(encoding="utf-8")
  required_sv_defines = (
    '`define UVMF_UPSTREAM_VERSION "{}"'.format(UPSTREAM_UVMF_RELEASE),
    '`define UVMF_PROJECT_RELEASE "{}"'.format(PROJECT_VERSION),
  )
  for define in required_sv_defines:
    if define not in sv_version:
      raise ValueError("uvmf_version.svh is missing {!r}".format(define))

  commit = release_info.get("release_commit", "")
  if not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise ValueError("release.INFO release_commit must be a full Git commit ID")
  tag_commit = subprocess.run(
    ["git", "rev-list", "-n", "1", release_info["release_tag"]],
    cwd=REPO_ROOT,
    text=True,
    capture_output=True,
    check=False,
  )
  if tag_commit.returncode != 0 or tag_commit.stdout.strip() != commit:
    raise ValueError("release.INFO tag and commit do not resolve to the same revision")
  tag_date = subprocess.run(
    ["git", "show", "-s", "--format=%cs", release_info["release_tag"]],
    cwd=REPO_ROOT,
    text=True,
    capture_output=True,
    check=False,
  )
  if tag_date.returncode != 0 or tag_date.stdout.strip() != release_info.get("release_date"):
    raise ValueError("release.INFO release_date does not match the tagged commit date")


def check_no_stale_sv_backups():
  stale_files = sorted(REPO_ROOT.rglob("*.sv.new"))
  if stale_files:
    relative = [str(path.relative_to(REPO_ROOT)) for path in stale_files]
    raise ValueError("stale .sv.new files are not allowed: {}".format(", ".join(relative)))


def main():
  args = parse_args()
  try:
    check_release_metadata()
    check_no_stale_sv_backups()
  except (OSError, ValueError) as error:
    print("Smoke check failed: {}".format(error), file=sys.stderr)
    return 1

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
