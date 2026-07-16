#! /usr/bin/env python3

from pathlib import Path
from types import SimpleNamespace
import os
import stat
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(REPO_ROOT / "templates" / "python"))
sys.path.insert(0,str(REPO_ROOT / "templates" / "python" / "python3"))

from uvmf_gen import BaseGeneratorClass, UserError
from uvmf_yaml.regen import Merge, Parse


class RegenerationSafetyTest(unittest.TestCase):
  def make_generator(self,root):
    generator = BaseGeneratorClass("soc","bench")
    generator.root = str(root)
    generator.bench_location = "project_benches"
    generator.options = SimpleNamespace(quiet=True)
    return generator

  def test_clean_removes_only_approved_outputs(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      bench = root / "project_benches" / "soc"
      keep_files = [
        bench / "tb" / "BUILD",
        bench / "tb" / "tests" / "src" / "soc_base_test.svh",
        bench / "tb" / "custom" / "user_sequence.svh",
        root / "verification_ip" / "environment_packages" / "soc_env_pkg" / "BUILD",
        root / "verification_ip" / "custom" / "keep.sv",
      ]
      obsolete_files = [
        bench / "tb" / "tests" / "src" / "register_test.sv",
        bench / "tb" / "tests" / "src" / "example_derived_test.sv",
        root / "verification_ip" / "legacy.f",
        root / "verification_ip" / "compile.do",
        root / "verification_ip" / "interface_packages" / "foo_pkg" / "Makefile",
        root / "verification_ip" / "environment_packages" / "bar_env_pkg" / "Makefile",
        root / ".project",
        bench / "tb" / "tests" / "demo_tests.bzl",
      ]
      obsolete_dirs = [
        bench / "sim",
        bench / "rtl",
        bench / "docs",
        bench / "tb" / "sequences",
      ]

      for path in keep_files + obsolete_files:
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text("sentinel\n",encoding="utf-8")
      for path in obsolete_dirs:
        path.mkdir(parents=True,exist_ok=True)
        (path / "old_generated_file.sv").write_text("obsolete\n",encoding="utf-8")

      self.make_generator(root).cleanupApprovedOutputs()

      self.assertTrue(all(path.is_file() for path in keep_files))
      self.assertTrue(all(not path.exists() for path in obsolete_files))
      self.assertTrue(all(not path.exists() for path in obsolete_dirs))

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

  def test_testbench_build_merge_keeps_only_custom_dependencies(self):
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
        self.assertNotIn('"//hw/dv/project_benches/soc/tb/tests:tests"',merged)
        self.assertNotIn("//hw/dv/verification_ip/environment_packages/soc_env_pkg:pkg",merged)
        self.assertLess(
          merged.index('"@uvmf//uvmf_base_pkg:pkg"'),
          merged.index('"@vip_vcs_svt_pkg//:pkg"'),
        )
        self.assertLess(
          merged.index('"@vip_vcs_svt_pkg//:pkg"'),
          merged.index('"//hw/dv/project_benches/soc/tb/parameters:pkg"'),
        )

  def test_tests_build_merge_keeps_only_custom_dependencies(self):
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
      self.assertNotIn('"@uvmf//uvmf_base_pkg:pkg"',merged)
      self.assertEqual(merged.count('"@vip_vcs_svt_pkg//:pkg"'),1)
      self.assertEqual(merged.count('"//hw/dv/project_benches/soc/tb/parameters:pkg"'),1)
      self.assertNotIn("//hw/dv/verification_ip/environment_packages/soc_env_pkg:pkg",merged)
      self.assertLess(
        merged.index('"//hw/dv/project_benches/soc/tb/parameters:pkg"'),
        merged.index('"@vip_vcs_svt_pkg//:pkg"'),
      )

  def test_environment_build_merge_keeps_only_custom_dependencies(self):
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
      self.assertNotIn("removed_env_pkg:pkg",merged)
      self.assertEqual(merged.count('"@early_custom//:pkg"'),1)
      self.assertEqual(merged.count('"@vip_vcs_svt_pkg//:pkg"'),1)
      self.assertEqual(merged.count('"//custom/pkg:pkg"'),1)
      self.assertLess(
        merged.index('"@early_custom//:pkg"'),
        merged.index('"//hw/dv/verification_ip/interface_packages/bus_pkg:pkg"'),
      )


if __name__ == "__main__":
  unittest.main()
