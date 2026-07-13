import os
import shutil


OBSOLETE_SUFFIXES = ('.F','.vcompile','.vinfo','.compile','.f')
OBSOLETE_NAMES = ('compile.do','.project','.svproject')
OBSOLETE_TESTS = (
  'register_test.svh','register_test.sv',
  'example_derived_test.svh','example_derived_test.sv',
)


def find_obsolete_outputs(root,bench_roots=None):
  root = os.path.abspath(os.path.normpath(root))
  if not os.path.isdir(root):
    return [],[]

  if bench_roots is None:
    project_benches = os.path.join(root,'project_benches')
    bench_roots = [os.path.join(project_benches,name) for name in os.listdir(project_benches)] if os.path.isdir(project_benches) else []

  obsolete_dirs = set()
  obsolete_files = set()
  for bench_root in bench_roots:
    for relative in ('sim','rtl','docs',os.path.join('tb','sequences')):
      path = os.path.join(bench_root,relative)
      if os.path.isdir(path):
        obsolete_dirs.add(os.path.abspath(path))
    tests_root = os.path.join(bench_root,'tb','tests','src')
    for filename in OBSOLETE_TESTS:
      path = os.path.join(tests_root,filename)
      if os.path.isfile(path) or os.path.islink(path):
        obsolete_files.add(os.path.abspath(path))

  from uvmf_gen import BaseGeneratorClass
  helper = BaseGeneratorClass('audit','audit')
  for dirpath,dirnames,filenames in os.walk(root):
    dirnames[:] = [name for name in dirnames if os.path.abspath(os.path.join(dirpath,name)) not in obsolete_dirs]
    for filename in filenames:
      path = os.path.join(dirpath,filename)
      norm_path = path.replace('\\','/')
      if filename.endswith(OBSOLETE_SUFFIXES) or filename in OBSOLETE_NAMES or helper.isApprovedObsoleteGeneratedMakefile(norm_path) or helper.isApprovedObsoleteGeneratedSvh(path):
        obsolete_files.add(os.path.abspath(path))
  return sorted(obsolete_files),sorted(obsolete_dirs)


def remove_obsolete_outputs(root,bench_roots=None,quiet=False):
  files,dirs = find_obsolete_outputs(root,bench_roots)
  for path in files:
    if not quiet:
      print("Removing approved obsolete file "+path)
    os.remove(path)
  for path in sorted(dirs,key=len,reverse=True):
    if not quiet:
      print("Removing approved obsolete directory "+path)
    shutil.rmtree(path)
  if not quiet:
    print("Approved cleanup removed {0} file(s) and {1} directory tree(s)".format(len(files),len(dirs)))
