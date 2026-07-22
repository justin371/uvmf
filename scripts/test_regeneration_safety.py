#! /usr/bin/env python3

from pathlib import Path
from types import SimpleNamespace
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(REPO_ROOT / "scripts"))
sys.path.insert(0,str(REPO_ROOT / "templates" / "python"))
sys.path.insert(0,str(REPO_ROOT / "templates" / "python" / "python3"))

from uvmf_gen import BaseGeneratorClass, UserError, UVMFCommandLineParser
from uvmf_yaml.backup import backup
from uvmf_yaml.obsolete import remove_obsolete_outputs
from uvmf_yaml.regen import Merge, Parse
from yaml2uvmf import DataClass


class RegenerationSafetyTest(unittest.TestCase):
  def make_generator(self,root):
    generator = BaseGeneratorClass("soc","bench")
    generator.root = str(root)
    generator.bench_location = "project_benches"
    generator.options = SimpleNamespace(quiet=True)
    return generator

  def test_template_custom_blocks_do_not_deindent_content(self):
    pragma = re.compile(
      r'^(\s*)(?://+|#+) pragma uvmf custom (\w+) (begin|end)'
    )
    template_root = (
      REPO_ROOT / "templates" / "python" / "template_files"
    )

    for path in template_root.rglob("*.TMPL"):
      stack = []
      lines = path.read_text(encoding="utf-8").splitlines()
      for line_number,line in enumerate(lines,1):
        match = pragma.match(line)
        if match:
          indent = len(match.group(1))
          label = match.group(2)
          if match.group(3) == "begin":
            stack.append((label,indent,line_number))
          else:
            self.assertTrue(stack,"Unmatched end in {}:{}".format(path,line_number))
            begin_label,begin_indent,begin_line = stack.pop()
            self.assertEqual(begin_label,label,"Mismatched block in {}:{}".format(path,line_number))
            self.assertEqual(begin_indent,indent,"Mismatched marker indentation in {}:{}".format(path,line_number))
          continue

        if not stack or not line.strip() or line.lstrip().startswith(("{%","{#")):
          continue
        self.assertGreaterEqual(
          len(line)-len(line.lstrip()),
          stack[-1][1],
          "Custom block content deindents before its marker in {}:{}".format(
            path,line_number
          ),
        )

      self.assertFalse(stack,"Unclosed custom block in {}".format(path))

  def test_clean_requires_generator_proof_and_preserves_custom_content(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      bench = root / "project_benches" / "soc"
      keep_files = [
        bench / "tb" / "BUILD",
        bench / "tb" / "tests" / "src" / "soc_base_test.svh",
        bench / "tb" / "custom" / "user_sequence.svh",
        root / "verification_ip" / "environment_packages" / "soc_env_pkg" / "BUILD",
        root / "verification_ip" / "custom" / "keep.sv",
        bench / "tb" / "tests" / "src" / "register_test.sv",
        root / "verification_ip" / "legacy.f",
        root / "verification_ip" / "compile.do",
        root / "verification_ip" / "interface_packages" / "foo_pkg" / "Makefile",
        root / ".project",
        bench / "tb" / "tests" / "demo_tests.bzl",
      ]
      generated_obsolete = [
        bench / "tb" / "tests" / "src" / "example_derived_test.sv",
        root / "verification_ip" / "generated.compile",
        root / "verification_ip" / "environment_packages" / "bar_env_pkg" / "Makefile",
      ]
      keep_dirs = [
        bench / "sim",
        bench / "rtl",
        bench / "docs",
        bench / "tb" / "sequences",
      ]

      for path in keep_files:
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text("sentinel\n",encoding="utf-8")
      for path in generated_obsolete:
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(
          "// Created with uvmf_gen version 2023.4_2\n",encoding="utf-8"
        )
      custom_generated = root / "verification_ip" / "custom.compile"
      custom_generated.write_text(
        "// Created with uvmf_gen version 2023.4_2\n"
        "# pragma uvmf custom additional begin\n"
        "hand_setting = 1\n"
        "# pragma uvmf custom additional end\n",
        encoding="utf-8",
      )
      for path in keep_dirs:
        path.mkdir(parents=True,exist_ok=True)
        (path / "old_generated_file.sv").write_text("obsolete\n",encoding="utf-8")

      self.make_generator(root).cleanupApprovedOutputs()

      self.assertTrue(all(path.is_file() for path in keep_files))
      self.assertTrue(all(not path.exists() for path in generated_obsolete))
      self.assertTrue(custom_generated.is_file())
      self.assertTrue(all(path.is_dir() for path in keep_dirs))

  def test_atomic_output_replaces_complete_file(self):
    with tempfile.TemporaryDirectory() as tmp:
      output = Path(tmp) / "generated.sv"
      output.write_text("old content\n",encoding="utf-8")
      os.chmod(output,stat.S_IRUSR | stat.S_IWUSR)

      self.make_generator(Path(tmp)).writeOutputAtomically(
        str(output),"new content\n"
      )

      self.assertEqual(output.read_text(encoding="utf-8"),"new content\n")

  def test_clean_rejects_path_outside_destination(self):
    with tempfile.TemporaryDirectory() as tmp:
      parent = Path(tmp)
      root = parent / "output"
      outside = parent / "outside" / "sim"
      root.mkdir()
      outside.mkdir(parents=True)
      sentinel = outside / "keep.sv"
      sentinel.write_text("must remain\n",encoding="utf-8")

      generator = self.make_generator(root)
      generator.bench_location = ".."
      generator.name = "outside"

      with self.assertRaises(UserError):
        generator.cleanupApprovedOutputs()
      self.assertEqual(sentinel.read_text(encoding="utf-8"),"must remain\n")

  def test_cleanup_api_rejects_absolute_bench_root_outside_merge_root(self):
    with tempfile.TemporaryDirectory() as tmp:
      parent = Path(tmp)
      root = parent / "output"
      outside = parent / "outside"
      root.mkdir()
      outside.mkdir()
      sentinel = outside / "legacy.compile"
      sentinel.write_text(
        "// Created with uvmf_gen version 2023.4_2\n",encoding="utf-8"
      )

      with self.assertRaisesRegex(UserError,"outside merge root"):
        remove_obsolete_outputs(str(root),[str(outside)],quiet=True)
      self.assertTrue(sentinel.is_file())

  def test_backup_with_trailing_separator_is_a_sibling(self):
    with tempfile.TemporaryDirectory() as tmp:
      parent = Path(tmp)
      source = parent / "source"
      source.mkdir()
      (source / "keep.txt").write_text("content\n",encoding="utf-8")

      destination = Path(backup(str(source)+os.sep))

      self.assertEqual(destination,parent / "source_bak_0")
      self.assertFalse(str(destination).startswith(str(source)+os.sep))
      self.assertEqual(
        (destination / "keep.txt").read_text(encoding="utf-8"),"content\n"
      )

  def test_default_profile_skips_only_generated_makefiles(self):
    generator = self.make_generator(Path.cwd())
    generator.options = SimpleNamespace(
      quiet=True,
    )
    generator.gen_type = "environment"

    self.assertTrue(generator.skipTemplateOutput("verification_ip/environment_packages/foo_env_pkg/Makefile"))
    self.assertTrue(generator.skipTemplateOutput("verification_ip/interface_packages/foo_pkg/Makefile"))
    self.assertFalse(generator.skipTemplateOutput("verification_ip/environment_packages/foo_env_pkg/src/foo_env_configuration.sv"))
    self.assertFalse(generator.skipTemplateOutput("verification_ip/environment_packages/foo_env_pkg/BUILD"))
    generator.gen_type = "bench"
    self.assertFalse(generator.skipTemplateOutput("project_benches/soc/tb/BUILD"))

  def test_clean_removes_only_stale_generated_svh_replaced_by_sv(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      old_src = root / "verification_ip" / "environment_packages" / "soc_env_pkg" / "src"
      old_src.mkdir(parents=True,exist_ok=True)
      stale = old_src / "soc_env_configuration.svh"
      replacement = old_src / "soc_env_configuration.sv"
      header = old_src / "soc_env_typedefs.svh"
      user_file = old_src / "custom_logic.svh"
      stale.write_text("`ifndef _SOC_ENV_CONFIGURATION__SVH__\n// Created with uvmf_gen version 2023.4_2\n`endif // _SOC_ENV_CONFIGURATION__SVH__\n",encoding="utf-8")
      replacement.write_text("module dummy; endmodule\n",encoding="utf-8")
      header.write_text("`ifndef _SOC_ENV_TYPEDEFS__SVH__\n// Created with uvmf_gen version 2023.4_2\n`endif // _SOC_ENV_TYPEDEFS__SVH__\n",encoding="utf-8")
      user_file.write_text("// user-owned file\n",encoding="utf-8")

      self.make_generator(root).cleanupApprovedOutputs()

      self.assertFalse(stale.exists())
      self.assertTrue(replacement.exists())
      self.assertTrue(header.exists())
      self.assertTrue(user_file.exists())

  def test_generated_svh_whitelist_is_explicit(self):
    generator = self.make_generator(Path.cwd())
    self.assertTrue(generator.keepGeneratedSvh("src/foo_macros.svh"))
    self.assertTrue(generator.keepGeneratedSvh("src/foo_typedefs.svh"))
    self.assertTrue(generator.keepGeneratedSvh("src/foo_env_typedefs.svh"))
    self.assertFalse(generator.keepGeneratedSvh("src/foo_sequence_base.svh"))
    self.assertFalse(generator.keepGeneratedSvh("src/foo_driver.svh"))

  def test_failed_merge_preserves_original_file(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      old_root = root / "old"
      new_root = root / "new"
      old_root.mkdir()
      new_root.mkdir()
      old_file = old_root / "test.svh"
      new_file = new_root / "test.svh"
      old_file.write_text("original content\n",encoding="utf-8")
      new_file.write_text("generated content\n",encoding="utf-8")

      merge = Merge(
        outdir=str(old_root),
        skip_missing_blocks=False,
        new_root=str(new_root),
        old_root=str(old_root),
        quiet=True,
      )
      merge.rd = {
        str(old_file.resolve()): {
          "custom": {
            "content": "user content\n",
            "begin_line": 1,
            "end_line": 3,
          }
        }
      }

      self.assertTrue(merge.file_begin(str(new_file.resolve())))
      merge.ofs.write("incomplete merged content\n")
      with self.assertRaises(UserError):
        merge.file_end(str(new_file.resolve()))

      self.assertEqual(old_file.read_text(encoding="utf-8"),"original content\n")
      self.assertEqual(list(old_root.glob("*.uvmf_merge_tmp")),[])

  def test_parser_accepts_legacy_multi_slash_pragma(self):
    with tempfile.TemporaryDirectory() as tmp:
      source = Path(tmp) / "register_model.sv"
      source.write_text(
        "//// pragma uvmf custom additional_imports begin\n"
        "import custom_pkg::*;\n"
        "// pragma uvmf custom additional_imports end\n",
        encoding="utf-8",
      )
      parser = Parse(root=tmp,quiet=True)
      parser.parse_file(str(source))
      self.assertEqual(
        parser.data[str(source)]["additional_imports"]["content"],
        "import custom_pkg::*;\n",
      )

  def test_old_file_decode_error_fails_closed(self):
    with tempfile.TemporaryDirectory() as tmp:
      source = Path(tmp) / "invalid.sv"
      source.write_bytes(b"// pragma uvmf custom keep begin\n\xff\n")

      parser = Parse(root=tmp,quiet=True)
      with self.assertRaisesRegex(UserError,"Unable to decode file as text"):
        parser.parse_file(str(source))

  def test_duplicate_custom_labels_fail_closed(self):
    with tempfile.TemporaryDirectory() as tmp:
      source = Path(tmp) / "duplicate.sv"
      source.write_text(
        "// pragma uvmf custom repeated begin\n"
        "first\n"
        "// pragma uvmf custom repeated end\n"
        "// pragma uvmf custom repeated begin\n"
        "second\n"
        "// pragma uvmf custom repeated end\n",
        encoding="utf-8",
      )

      parser = Parse(root=tmp,quiet=True)
      with self.assertRaisesRegex(UserError,'Duplicate custom block label') as error:
        parser.parse_file(str(source))
      self.assertIn('Label: "repeated"',str(error.exception))

  def test_failed_directory_merge_keeps_all_original_files(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      old_root = root / "old"
      new_root = root / "new"
      old_root.mkdir()
      new_root.mkdir()
      old_a = old_root / "a.sv"
      old_z = old_root / "z.sv"
      new_a = new_root / "a.sv"
      new_z = new_root / "z.sv"
      old_a.write_text("old a\n// pragma uvmf custom keep begin\nuser a\n// pragma uvmf custom keep end\n",encoding="utf-8")
      old_z.write_text("old z\n// pragma uvmf custom lost begin\nuser z\n// pragma uvmf custom lost end\n",encoding="utf-8")
      new_a.write_text("new a\n// pragma uvmf custom keep begin\n// pragma uvmf custom keep end\n",encoding="utf-8")
      new_z.write_text("new z\n",encoding="utf-8")

      merge = Merge(
        outdir=str(old_root),skip_missing_blocks=False,
        new_root=str(new_root),old_root=str(old_root),quiet=True,
      )
      merge.rd = {
        str(old_a.resolve()): {"keep": {"content": "user a\n","begin_line": 2,"end_line": 4}},
        str(old_z.resolve()): {"lost": {"content": "user z\n","begin_line": 2,"end_line": 4}},
      }

      with self.assertRaises(UserError) as error:
        merge.traverse_dir(str(new_root))

      self.assertIsNone(error.exception.__context__)
      self.assertIn("new output does not contain this custom block",str(error.exception))
      self.assertTrue(old_a.read_text(encoding="utf-8").startswith("old a\n"))
      self.assertTrue(old_z.read_text(encoding="utf-8").startswith("old z\n"))
      self.assertEqual(list(old_root.glob("*.uvmf_merge_tmp")),[])

  def test_replace_failure_rolls_back_every_merged_file(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      old_root = root / "old"
      new_root = root / "new"
      old_root.mkdir()
      new_root.mkdir()
      for name in ("a.sv","b.sv"):
        (old_root / name).write_text("old "+name+"\n",encoding="utf-8")
        (new_root / name).write_text("new "+name+"\n",encoding="utf-8")

      parser = Parse(root=str(old_root),quiet=True)
      parser.traverse_dir(str(old_root))
      merge = Merge(
        outdir=str(old_root),skip_missing_blocks=False,
        new_root=str(new_root),old_root=str(old_root),quiet=True,
      )
      merge.load_data(parser.data)
      real_replace = os.replace
      replace_count = 0

      def fail_second_replace(source,destination):
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
          raise OSError("injected replace failure")
        return real_replace(source,destination)

      with mock.patch("uvmf_yaml.regen.os.replace",side_effect=fail_second_replace):
        with self.assertRaisesRegex(OSError,"injected replace failure"):
          merge.traverse_dir(str(new_root))

      self.assertEqual((old_root / "a.sv").read_text(encoding="utf-8"),"old a.sv\n")
      self.assertEqual((old_root / "b.sv").read_text(encoding="utf-8"),"old b.sv\n")
      self.assertEqual(list(root.glob(".uvmf_merge_backup_*")),[])

  def test_copy_failure_leaves_no_partial_new_tree(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      old_root = root / "old"
      new_root = root / "new"
      old_root.mkdir()
      (new_root / "nested").mkdir(parents=True)
      for name in ("a.sv","b.sv"):
        (new_root / "nested" / name).write_text(
          "new "+name+"\n",encoding="utf-8"
        )

      merge = Merge(
        outdir=str(old_root),skip_missing_blocks=False,
        new_root=str(new_root),old_root=str(old_root),quiet=True,
      )
      merge.load_data({})
      copy_count = 0

      def fail_second_copy(source,destination):
        nonlocal copy_count
        copy_count += 1
        if copy_count == 2:
          raise OSError("injected copy failure")
        return shutil.copyfile(source,destination)

      with mock.patch("uvmf_yaml.regen.copyfile",side_effect=fail_second_copy):
        with self.assertRaisesRegex(OSError,"injected copy failure"):
          merge.traverse_dir(str(new_root))

      self.assertFalse((old_root / "nested").exists())
      self.assertEqual(list(old_root.rglob("*.uvmf_merge_tmp")),[])

  def test_cleanup_failure_restores_every_deleted_file(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      files = [root / "a.compile",root / "b.compile"]
      for path in files:
        path.write_text(
          "// Created with uvmf_gen version 2023.4_2\n",encoding="utf-8"
        )
      real_remove = os.remove
      remove_count = 0

      def fail_second_remove(path):
        nonlocal remove_count
        remove_count += 1
        if remove_count == 2:
          raise OSError("injected cleanup failure")
        return real_remove(path)

      with mock.patch("uvmf_yaml.obsolete.os.remove",side_effect=fail_second_remove):
        with self.assertRaisesRegex(OSError,"injected cleanup failure"):
          remove_obsolete_outputs(str(root),quiet=True)

      self.assertTrue(all(path.is_file() for path in files))
      self.assertEqual(list(Path(tmp).glob(".uvmf_cleanup_backup_*")),[])

  def test_skip_missing_block_applies_new_file(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      old_root = root / "old"
      new_root = root / "new"
      old_root.mkdir()
      new_root.mkdir()
      old_file = old_root / "hdl_top.sv"
      new_file = new_root / "hdl_top.sv"
      old_file.write_text("old\n// pragma uvmf custom removed begin\nuser code\n// pragma uvmf custom removed end\n",encoding="utf-8")
      new_file.write_text("new\n",encoding="utf-8")

      merge = Merge(
        outdir=str(old_root),skip_missing_blocks=True,
        new_root=str(new_root),old_root=str(old_root),quiet=True,
      )
      merge.rd = {
        str(old_file.resolve()): {"removed": {"content": "user code\n","begin_line": 2,"end_line": 4}},
      }
      merge.traverse_dir(str(new_root))

      self.assertEqual(old_file.read_text(encoding="utf-8"),"new\n")
      self.assertEqual(merge.missing_blocks,{str(old_file.resolve()): ["removed"]})

  def test_empty_legacy_tb_attributes_uses_new_defaults(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      old_root = root / "old"
      new_root = root / "new"
      old_root.mkdir()
      new_root.mkdir()
      old_file = old_root / "BUILD"
      new_file = new_root / "BUILD"
      old_file.write_text("verilog_dv_tb(\n    name = \"old\",\n    # pragma uvmf custom tb_attributes begin\n    # pragma uvmf custom tb_attributes end\n)\n",encoding="utf-8")
      new_file.write_text("verilog_dv_tb(\n    # pragma uvmf custom tb_attributes begin\n    name = \"new\",\n    simulator = \"VCS\",\n    # pragma uvmf custom tb_attributes end\n)\n",encoding="utf-8")

      parser = Parse(root=str(old_root),quiet=True)
      parser.parse_file(str(old_file))
      merge = Merge(
        outdir=str(old_root),skip_missing_blocks=False,
        new_root=str(new_root),old_root=str(old_root),quiet=True,
      )
      merge.load_data(parser.data)
      merge.traverse_dir(str(new_root))

      self.assertIn('name = "new"',old_file.read_text(encoding="utf-8"))
      self.assertIn('simulator = "VCS"',old_file.read_text(encoding="utf-8"))

  def test_testbench_build_merge_preserves_hand_dependencies_and_deduplicates_exact_generated_ones(self):
    for env_dependency in (
      '        "//hw/dv/verification_ip/environment_packages/soc_env_pkg:pkg",\n',
      '        #"//hw/dv/verification_ip/environment_packages/soc_env_pkg:pkg",\n',
    ):
      with self.subTest(env_dependency=env_dependency), tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        relative = Path("project_benches") / "soc" / "tb" / "testbench" / "BUILD"
        old_file = root / "old" / relative
        new_file = root / "new" / relative
        old_file.parent.mkdir(parents=True)
        new_file.parent.mkdir(parents=True)
        old_file.write_text(
          "deps = [\n"
          "        # pragma uvmf custom deps_additional begin\n"
          "        \"@uvmf//uvmf_base_pkg:pkg\",\n"
          "        \"@vip_vcs_svt_pkg//:pkg\",\n"
          "        \"//hw/dv/project_benches/soc/tb/parameters:pkg\",\n"
          "        \"//hw/dv/project_benches/soc/tb/tests:tests\",\n"
          + env_dependency +
          "        # pragma uvmf custom deps_additional end\n"
          "]\n",
          encoding="utf-8",
        )
        new_file.write_text(
          "deps = [\n"
          "        \"@uvmf//uvmf_base_pkg:pkg\",\n"
          "        # pragma uvmf custom deps_additional begin\n"
          "        # pragma uvmf custom deps_additional end\n"
          "        \"//hw/dv/project_benches/soc/tb/parameters:pkg\",\n"
          "        \"//hw/dv/project_benches/soc/tb/tests\",\n"
          "]\n",
          encoding="utf-8",
        )

        parser = Parse(root=str(root / "old"),quiet=True)
        parser.parse_file(str(old_file))
        merge = Merge(
          outdir=str(root / "old"),skip_missing_blocks=False,
          new_root=str(root / "new"),old_root=str(root / "old"),quiet=True,
        )
        merge.load_data(parser.data)
        merge.traverse_dir(str(root / "new"))

        merged = old_file.read_text(encoding="utf-8")
        self.assertEqual(merged.count('"@uvmf//uvmf_base_pkg:pkg"'),1)
        self.assertEqual(merged.count('"@vip_vcs_svt_pkg//:pkg"'),1)
        self.assertEqual(merged.count('"//hw/dv/project_benches/soc/tb/parameters:pkg"'),1)
        self.assertEqual(merged.count('"//hw/dv/project_benches/soc/tb/tests"'),1)
        self.assertIn('"//hw/dv/project_benches/soc/tb/tests:tests"',merged)
        self.assertIn("//hw/dv/verification_ip/environment_packages/soc_env_pkg:pkg",merged)
        self.assertLess(
          merged.index('"@uvmf//uvmf_base_pkg:pkg"'),
          merged.index('"@vip_vcs_svt_pkg//:pkg"'),
        )
        self.assertLess(
          merged.index('"@vip_vcs_svt_pkg//:pkg"'),
          merged.index('"//hw/dv/project_benches/soc/tb/parameters:pkg"'),
        )

  def test_tests_build_merge_keeps_nonduplicated_hand_dependencies(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      relative = Path("project_benches") / "soc" / "tb" / "tests" / "BUILD"
      old_file = root / "old" / relative
      new_file = root / "new" / relative
      old_file.parent.mkdir(parents=True)
      new_file.parent.mkdir(parents=True)
      old_file.write_text(
        "deps = [\n"
        "        # pragma uvmf custom deps_additional begin\n"
        "        \"@uvmf//uvmf_base_pkg:pkg\",\n"
        "        \"@vip_vcs_svt_pkg//:pkg\",\n"
        "        \"//hw/dv/project_benches/soc/tb/parameters:pkg\",\n"
        "        #\"//hw/dv/verification_ip/environment_packages/soc_env_pkg:pkg\",\n"
        "        # pragma uvmf custom deps_additional end\n"
        "]\n",
        encoding="utf-8",
      )
      new_file.write_text(
        "deps = [\n"
        "        \"//hw/dv/project_benches/soc/tb/parameters:pkg\",\n"
        "        # pragma uvmf custom deps_additional begin\n"
        "        # pragma uvmf custom deps_additional end\n"
        "]\n",
        encoding="utf-8",
      )

      parser = Parse(root=str(root / "old"),quiet=True)
      parser.parse_file(str(old_file))
      merge = Merge(
        outdir=str(root / "old"),skip_missing_blocks=False,
        new_root=str(root / "new"),old_root=str(root / "old"),quiet=True,
      )
      merge.load_data(parser.data)
      merge.traverse_dir(str(root / "new"))

      merged = old_file.read_text(encoding="utf-8")
      self.assertEqual(merged.count('"@uvmf//uvmf_base_pkg:pkg"'),1)
      self.assertEqual(merged.count('"@vip_vcs_svt_pkg//:pkg"'),1)
      self.assertEqual(merged.count('"//hw/dv/project_benches/soc/tb/parameters:pkg"'),1)
      self.assertIn("//hw/dv/verification_ip/environment_packages/soc_env_pkg:pkg",merged)
      self.assertLess(
        merged.index('"//hw/dv/project_benches/soc/tb/parameters:pkg"'),
        merged.index('"@vip_vcs_svt_pkg//:pkg"'),
      )

  def test_environment_build_merge_only_deduplicates_exact_active_dependencies(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      relative = (
        Path("verification_ip") / "environment_packages" /
        "soc_env_pkg" / "BUILD"
      )
      old_file = root / "old" / relative
      new_file = root / "new" / relative
      old_file.parent.mkdir(parents=True)
      new_file.parent.mkdir(parents=True)
      old_file.write_text(
        "deps = [\n"
        "        \"@uvmf//uvmf_base_pkg:pkg\",\n"
        "        # pragma uvmf custom deps_before_generated begin\n"
        "        \"@early_custom//:pkg\",\n"
        "        # pragma uvmf custom deps_before_generated end\n"
        "        # pragma uvmf custom deps_additional begin\n"
        "        \"@dv_common//cmn:pkg\",\n"
        "        \"@cluelib_pkg//:pkg\",\n"
        "        \"@svlib_pkg//:pkg\",\n"
        "        \"@vip_vcs_svt_pkg//:pkg\",\n"
        "        \"//hw/dv/verification_ip/interface_packages/bus_pkg:pkg\",\n"
        "        #\"//hw/dv/verification_ip/environment_packages/removed_env_pkg:pkg\",\n"
        "        \"//custom/pkg:pkg\",\n"
        "        # pragma uvmf custom deps_additional end\n"
        "]\n",
        encoding="utf-8",
      )
      new_file.write_text(
        "deps = [\n"
        "        \"@uvmf//uvmf_base_pkg:pkg\",\n"
        "        \"@dv_common//cmn:pkg\",\n"
        "        \"@cluelib_pkg//:pkg\",\n"
        "        \"@svlib_pkg//:pkg\",\n"
        "        # pragma uvmf custom deps_before_generated begin\n"
        "        # pragma uvmf custom deps_before_generated end\n"
        "        \"//hw/dv/verification_ip/interface_packages/bus_pkg:pkg\",\n"
        "        # pragma uvmf custom deps_additional begin\n"
        "        # pragma uvmf custom deps_additional end\n"
        "]\n",
        encoding="utf-8",
      )

      parser = Parse(root=str(root / "old"),quiet=True)
      parser.parse_file(str(old_file))
      merge = Merge(
        outdir=str(root / "old"),skip_missing_blocks=False,
        new_root=str(root / "new"),old_root=str(root / "old"),quiet=True,
      )
      merge.load_data(parser.data)
      merge.traverse_dir(str(root / "new"))

      merged = old_file.read_text(encoding="utf-8")
      for dependency in (
        '"@uvmf//uvmf_base_pkg:pkg"',
        '"@dv_common//cmn:pkg"',
        '"@cluelib_pkg//:pkg"',
        '"@svlib_pkg//:pkg"',
        '"//hw/dv/verification_ip/interface_packages/bus_pkg:pkg"',
      ):
        self.assertEqual(merged.count(dependency),1,dependency)
      self.assertIn("removed_env_pkg:pkg",merged)
      self.assertEqual(merged.count('"@early_custom//:pkg"'),1)
      self.assertEqual(merged.count('"@vip_vcs_svt_pkg//:pkg"'),1)
      self.assertEqual(merged.count('"//custom/pkg:pkg"'),1)
      self.assertLess(
        merged.index('"@early_custom//:pkg"'),
        merged.index('"//hw/dv/verification_ip/interface_packages/bus_pkg:pkg"'),
      )

  def test_check_in_place_audits_merge_source(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      source = root / "source"
      source.mkdir()
      stale = source / "legacy.compile"
      stale.write_text(
        "// Created with uvmf_gen version 2023.4_2\n",encoding="utf-8"
      )
      config = root / "config.yaml"
      config.write_text(
        "uvmf:\n"
        "  environments:\n"
        "    soc: {}\n",
        encoding="utf-8",
      )

      result = subprocess.run(
        [
          sys.executable,str(REPO_ROOT / "scripts" / "yaml2uvmf.py"),
          "-q","--check","--merge_source="+str(source),str(config),
        ],
        cwd=str(root),text=True,capture_output=True,check=False,
      )

      self.assertNotEqual(result.returncode,0)
      self.assertIn("legacy.compile",result.stdout+result.stderr)
      self.assertTrue(stale.is_file())
      self.assertFalse(Path(str(source)+"_tmp").exists())

  def test_empty_top_level_yaml_section_is_user_error(self):
    with tempfile.TemporaryDirectory() as tmp:
      config = Path(tmp) / "empty.yaml"
      config.write_text("uvmf:\n  interfaces:\n",encoding="utf-8")
      data = DataClass(UVMFCommandLineParser())

      with self.assertRaisesRegex(
        UserError,'Top-level section "interfaces".*non-empty mapping'
      ):
        data.parseFile(str(config))


if __name__ == "__main__":
  unittest.main()
