import os
import re
import shutil
import tempfile

from uvmf_gen import UserError


OBSOLETE_SUFFIXES = ('.F','.vcompile','.vinfo','.compile','.f')
OBSOLETE_NAMES = ('compile.do','.project','.svproject')
OBSOLETE_TESTS = (
  'register_test.svh','register_test.sv',
  'example_derived_test.svh','example_derived_test.sv',
)
GENERATED_SIGNATURE = 'Created with uvmf_gen version'
CUSTOM_PRAGMA_RE = re.compile(
  r'^\s*(?:/{2,}|#+) pragma uvmf custom (\w+) (begin|end)'
)


def _normalized_root(root):
  return os.path.realpath(os.path.abspath(os.path.normpath(root)))


def _assert_within_root(root,path,description='cleanup path'):
  candidate = os.path.realpath(os.path.abspath(os.path.normpath(path)))
  try:
    inside = os.path.commonpath([root,candidate]) == root
  except ValueError:
    inside = False
  if not inside:
    raise UserError(
      'Refusing {0} outside merge root {1}: {2}'.format(
        description,root,path
      )
    )
  return candidate


def _read_generator_owned_text(path):
  if os.path.islink(path) or not os.path.isfile(path):
    return None
  try:
    with open(path,'r',encoding='utf-8') as handle:
      text = handle.read()
  except (OSError,UnicodeDecodeError):
    return None
  if GENERATED_SIGNATURE not in text:
    return None
  return text


def _has_custom_content(text):
  active_label = None
  content = []
  for line in text.splitlines():
    match = CUSTOM_PRAGMA_RE.search(line)
    if not match:
      if active_label is not None:
        content.append(line)
      continue
    label,kind = match.groups()
    if kind == 'begin':
      if active_label is not None:
        return True
      active_label = label
      content = []
    else:
      if active_label != label:
        return True
      if any(line.strip() for line in content):
        return True
      active_label = None
      content = []
  return active_label is not None


def _is_proven_obsolete_file(path,exact_manifest=False):
  text = _read_generator_owned_text(path)
  if text is None or _has_custom_content(text):
    return False
  filename = os.path.basename(path)
  if exact_manifest:
    return True
  if filename.endswith(OBSOLETE_SUFFIXES) or filename in OBSOLETE_NAMES:
    return True
  if filename.endswith('.svh'):
    replacement = os.path.splitext(path)[0]+'.sv'
    return os.path.isfile(replacement)
  if filename == 'Makefile':
    norm_path = path.replace('\\','/')
    return (
      norm_path.endswith('/sim/Makefile')
      or '/verification_ip/interface_packages/' in norm_path
      or '/verification_ip/environment_packages/' in norm_path
    )
  return False


def find_obsolete_outputs(root,bench_roots=None):
  root = _normalized_root(root)
  if not os.path.isdir(root):
    return [],[]

  if bench_roots is None:
    project_benches = os.path.join(root,'project_benches')
    bench_roots = [
      os.path.join(project_benches,name)
      for name in os.listdir(project_benches)
    ] if os.path.isdir(project_benches) else []

  normalized_bench_roots = []
  for bench_root in bench_roots:
    normalized_bench_roots.append(
      _assert_within_root(root,bench_root,'cleanup of bench output')
    )

  candidates = set()
  exact_manifest = set()
  for bench_root in normalized_bench_roots:
    tests_root = os.path.join(bench_root,'tb','tests','src')
    for filename in OBSOLETE_TESTS:
      path = os.path.join(tests_root,filename)
      candidates.add(path)
      exact_manifest.add(os.path.normcase(os.path.abspath(path)))
    path = os.path.join(bench_root,'tb','tests','demo_tests.bzl')
    candidates.add(path)
    exact_manifest.add(os.path.normcase(os.path.abspath(path)))

  for dirpath,dirnames,filenames in os.walk(root):
    # Never traverse a directory symlink during cleanup discovery.
    dirnames[:] = [
      name for name in dirnames
      if not os.path.islink(os.path.join(dirpath,name))
    ]
    for filename in filenames:
      path = os.path.join(dirpath,filename)
      if (
        filename.endswith(OBSOLETE_SUFFIXES)
        or filename in OBSOLETE_NAMES
        or filename == 'Makefile'
        or filename.endswith('.svh')
      ):
        candidates.add(path)

  obsolete_files = []
  for path in candidates:
    path = _assert_within_root(root,path)
    if _is_proven_obsolete_file(
      path,os.path.normcase(os.path.abspath(path)) in exact_manifest
    ):
      obsolete_files.append(path)
  # Directory names alone never prove generator ownership.  Leaving directories
  # in place also guarantees that hand files and custom blocks survive cleanup.
  return sorted(set(obsolete_files)),[]


class CleanupTransaction:
  def __init__(self,root):
    self.root = root
    self.backup_dir = None
    self.backups = []
    self.finished = False

  def stage(self,paths):
    if not paths:
      return
    self.backup_dir = tempfile.mkdtemp(
      prefix='.uvmf_cleanup_backup_',dir=os.path.dirname(self.root)
    )
    for index,path in enumerate(paths):
      path = _assert_within_root(self.root,path)
      backup_path = os.path.join(self.backup_dir,str(index))
      shutil.copy2(path,backup_path)
      self.backups.append((path,backup_path))

  def rollback(self):
    if self.finished:
      return
    failures = []
    for path,backup_path in self.backups:
      try:
        if os.path.exists(backup_path):
          os.makedirs(os.path.dirname(path),exist_ok=True)
          shutil.copy2(backup_path,path)
      except OSError as error:
        failures.append('{0}: {1}'.format(path,error))
    self._discard_backup()
    self.finished = True
    if failures:
      raise UserError('Unable to roll back cleanup:\n  '+'\n  '.join(failures))

  def commit(self):
    if self.finished:
      return
    self._discard_backup()
    self.finished = True

  def _discard_backup(self):
    if self.backup_dir:
      shutil.rmtree(self.backup_dir,ignore_errors=True)
      self.backup_dir = None


def remove_obsolete_outputs(root,bench_roots=None,quiet=False,defer_commit=False):
  root = _normalized_root(root)
  files,dirs = find_obsolete_outputs(root,bench_roots)
  transaction = CleanupTransaction(root)
  transaction.stage(files)
  try:
    for path in files:
      path = _assert_within_root(root,path)
      if not quiet:
        print('Removing proven generated obsolete file '+path)
      os.remove(path)
    # Kept for API compatibility. find_obsolete_outputs deliberately returns no
    # name-based directory candidates.
    for path in sorted(dirs,key=len,reverse=True):
      path = _assert_within_root(root,path)
      if not quiet:
        print('Removing proven generated obsolete directory '+path)
      shutil.rmtree(path)
  except BaseException:
    transaction.rollback()
    raise
  if not defer_commit:
    transaction.commit()
  if not quiet:
    print(
      'Proven generated cleanup removed {0} file(s) and {1} directory tree(s)'.format(
        len(files),len(dirs)
      )
    )
  return transaction
