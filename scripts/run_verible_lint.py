#! /usr/bin/env python3

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


SOURCE_EXTENSIONS = {".sv", ".svh", ".v", ".vh", ".svp", ".vp"}

EXCLUDED_FILES = {
  "common/mgc_vip/ace/mgc_ace_hvl.svh",
  "common/mgc_vip/axi/mgc_axi_hvl.svh",
  "common/mgc_vip/axi4/mgc_axi4_hvl.svh",
  "common/mgc_vip/axi4_v2/mgc_axi4_hvl.svh",
  "common/uvm_co_emulation_utilities/uvm_co-emulation_utilities/utils/reset/reset_ctrl_base.svh",
}

EXCLUDED_DIRS = (
  "common/mgc_vip/",
  "common/utility_packages/qvip_utils_pkg/",
  "common/fli_pkg/",
  "common/uvm_co_emulation_utilities/",
  "templates/qvip_configurator/",
)


def parse_args():
  parser = argparse.ArgumentParser(
    description="Run Verible lint on repository and generated Verilog sources."
  )
  parser.add_argument(
    "--verible-lint",
    default=os.environ.get("VERIBLE_LINT", "verible-verilog-lint"),
    help="Verible lint executable (default: VERIBLE_LINT or verible-verilog-lint)",
  )
  parser.add_argument(
    "--autofix",
    action="store_true",
    help="Apply Verible's safe in-place fixes.",
  )
  parser.add_argument(
    "paths",
    nargs="*",
    metavar="PATH",
    help="File or directory to lint (default: the UVMF repository)",
  )
  return parser.parse_args()


def is_repo_excluded(path,repo_root):
  try:
    relative = path.relative_to(repo_root).as_posix()
  except ValueError:
    return False
  if relative in EXCLUDED_FILES:
    return True
  return any(relative.startswith(directory) for directory in EXCLUDED_DIRS)


def source_files(scan_paths,repo_root):
  candidates = []
  for scan_path in scan_paths:
    if scan_path.is_file():
      candidates.append(scan_path)
    else:
      candidates.extend(scan_path.rglob("*"))

  seen = set()
  for path in sorted(candidates):
    if not path.is_file() or path.suffix.lower() not in SOURCE_EXTENSIONS:
      continue
    resolved = path.resolve()
    if resolved in seen:
      continue
    seen.add(resolved)
    if is_repo_excluded(resolved,repo_root):
      continue
    yield resolved


def main():
  args = parse_args()
  repo_root = Path(__file__).resolve().parent.parent
  rules_config = repo_root / ".rules.verible_lint"
  executable = shutil.which(str(Path(args.verible_lint).expanduser()))

  scan_paths = [Path(path).expanduser().resolve() for path in args.paths]
  if not scan_paths:
    scan_paths = [repo_root]
  missing_paths = [str(path) for path in scan_paths if not path.exists()]
  if missing_paths:
    print("ERROR: lint path does not exist: {}".format(", ".join(missing_paths)),file=sys.stderr)
    return 2

  if executable is None:
    print(
      "ERROR: verible-verilog-lint was not found. Add it to PATH, set "
      "VERIBLE_LINT, or pass --verible-lint.",
      file=sys.stderr,
    )
    return 2

  command = [
    executable,
    "--rules_config={}".format(rules_config),
    "--parse_fatal=false",
  ]
  if args.autofix:
    command.append("--autofix=inplace")

  files = list(source_files(scan_paths,repo_root))
  if not files:
    print("ERROR: no Verilog or SystemVerilog files found in requested paths.",file=sys.stderr)
    return 2
  failed = []
  for path in files:
    result = subprocess.run(command + [str(path)], check=False)
    if result.returncode != 0:
      failed.append(str(path))

  if failed:
    print("Verible lint failed for {} file(s).".format(len(failed)), file=sys.stderr)
    return 1

  print("Verible lint passed for {} files.".format(len(files)))
  return 0


if __name__ == "__main__":
  sys.exit(main())
