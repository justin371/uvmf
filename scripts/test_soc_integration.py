#! /usr/bin/env python3

from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(REPO_ROOT / "scripts"))
sys.path.insert(0,str(REPO_ROOT / "templates" / "python"))
sys.path.insert(0,str(REPO_ROOT / "templates" / "python" / "python3"))

from uvmf_gen import (
  SVH_EXTERNAL_KEEP_NAMES,
  SVH_KEEP_SUFFIXES,
  UVMFCommandLineParser,
  UserError,
)
from yaml2uvmf import DataClass


BASE_YAML = """\
uvmf:
  interfaces:
    bus:
      clock: clk
      reset: rst
      transaction_vars:
        - {name: data, type: bit, isrand: "False", iscompare: "True"}
  environments:
    ip:
      agents:
        - name: agent0
          type: bus
          initiator_responder: INITIATOR
    soc:
      subenvs:
        - name: ip0
          type: ip
  benches:
    soc:
      top_env: soc
      active_passive:
        - path: environment.ip0.agent0
          value: PASSIVE
"""


class SocIntegrationTest(unittest.TestCase):
  def test_verilog_comment_style_has_no_repeated_or_empty_comments(self):
    suffixes = {".sv",".svh",".v",".TMPL"}
    for root_name in ("common","uvmf_base_pkg","uvmf_template_output","templates"):
      for path in (REPO_ROOT / root_name).rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
          continue
        content = path.read_text(encoding="utf-8")
        self.assertNotRegex(content,r"/{4,}",str(path))
        self.assertNotRegex(content,r"(?m)^\s*//\s*$",str(path))

  def assert_allowed_template_svh(self,token,path):
    basename = Path(token).name.lower()
    self.assertTrue(
      basename in SVH_EXTERNAL_KEEP_NAMES
      or basename.endswith(SVH_KEEP_SUFFIXES)
      or basename.startswith("dpi_link_"),
      "{0}: unexpected .svh token {1}".format(path,token),
    )

  def data_object(self):
    return DataClass(UVMFCommandLineParser())

  def run_generator(self,yaml_file,outdir,*args):
    command = [
      sys.executable,
      str(REPO_ROOT / "scripts" / "yaml2uvmf.py"),
      "-q",
      "-d",
      str(outdir),
    ]
    command.extend(args)
    command.append(str(yaml_file))
    return subprocess.run(command,text=True,capture_output=True,check=False)

  def test_duplicate_component_definition_reports_both_files(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      first = root / "first.yaml"
      second = root / "second.yaml"
      first.write_text("uvmf:\n  environments:\n    ip: {}\n",encoding="utf-8")
      second.write_text("uvmf:\n  environments:\n    ip: {}\n",encoding="utf-8")
      data = self.data_object()
      data.parseFile(str(first))
      with self.assertRaisesRegex(UserError,"Duplicate environments definition") as caught:
        data.parseFile(str(second))
      self.assertIn(str(first),str(caught.exception))
      self.assertIn(str(second),str(caught.exception))

  def test_environment_cycle_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      config = Path(tmp) / "cycle.yaml"
      config.write_text(
        "uvmf:\n"
        "  environments:\n"
        "    a:\n"
        "      subenvs:\n"
        "        - {name: b0, type: b}\n"
        "    b:\n"
        "      subenvs:\n"
        "        - {name: a0, type: a}\n",
        encoding="utf-8",
      )
      data = self.data_object()
      data.parseFile(str(config))
      with self.assertRaisesRegex(UserError,"a -> b -> a"):
        data.validate()

  def test_path_selector_and_typed_generate(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(BASE_YAML,encoding="utf-8")

      result = self.run_generator(config,output,"-g","bench:soc")
      self.assertEqual(result.returncode,0,result.stderr)
      test_top = output / "project_benches" / "soc" / "tb" / "tests" / "src" / "test_top.sv"
      self.assertIn("PASSIVE",test_top.read_text(encoding="utf-8"))
      self.assertFalse((output / "verification_ip").exists())

  def test_legacy_bfm_name_selector_remains_supported(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(
        BASE_YAML.replace("path: environment.ip0.agent0","bfm_name: ip0_agent0"),
        encoding="utf-8",
      )
      result = self.run_generator(config,output,"-g","soc")
      self.assertEqual(result.returncode,0,result.stderr)
      test_top = output / "project_benches" / "soc" / "tb" / "tests" / "src" / "test_top.sv"
      self.assertIn("PASSIVE",test_top.read_text(encoding="utf-8"))

  def test_flattened_bfm_name_collision_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "collision.yaml"
      output = root / "output"
      config.write_text(
        "uvmf:\n"
        "  interfaces:\n"
        "    bus: {clock: clk, reset: rst}\n"
        "  environments:\n"
        "    left:\n"
        "      agents:\n"
        "        - {name: c, type: bus}\n"
        "    right:\n"
        "      agents:\n"
        "        - {name: b_c, type: bus}\n"
        "    soc:\n"
        "      subenvs:\n"
        "        - {name: a_b, type: left}\n"
        "        - {name: a, type: right}\n"
        "  benches:\n"
        "    soc: {top_env: soc}\n",
        encoding="utf-8",
      )
      result = self.run_generator(config,output,"-g","bench:soc")
      self.assertNotEqual(result.returncode,0)
      self.assertIn("BFM name collision",result.stderr+result.stdout)

  def test_mixed_path_and_legacy_selector_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(
        BASE_YAML.replace(
          "          value: PASSIVE\n",
          "          value: PASSIVE\n"
          "        - bfm_name: ip0_agent0\n"
          "          value: ACTIVE\n",
        ),
        encoding="utf-8",
      )
      result = self.run_generator(config,output,"-g","bench:soc")
      self.assertNotEqual(result.returncode,0)
      self.assertIn("both path and bfm_name selectors",result.stderr+result.stdout)

  def test_environment_base_sequence_has_no_implicit_traffic(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(BASE_YAML,encoding="utf-8")
      result = self.run_generator(config,output,"-g","environment:ip")
      self.assertEqual(result.returncode,0,result.stderr)
      sequence = output / "verification_ip" / "environment_packages" / "ip_env_pkg" / "src" / "ip_env_sequence_base.sv"
      content = sequence.read_text(encoding="utf-8")
      self.assertNotIn("repeat (25)",content)
      self.assertNotIn("_rand_seq",content)

  def test_empty_yaml_lists_are_normalized(self):
    with tempfile.TemporaryDirectory() as tmp:
      config = Path(tmp) / "empty_lists.yaml"
      config.write_text(
        "uvmf:\n"
        "  environments:\n"
        "    sys:\n"
        "      agents:\n"
        "      subenvs:\n"
        "      analysis_ports:\n",
        encoding="utf-8",
      )
      data = self.data_object()
      data.parseFile(str(config))
      data.validate()
      self.assertEqual(data.data["environments"]["sys"]["agents"],[])
      self.assertEqual(data.data["environments"]["sys"]["subenvs"],[])
      self.assertEqual(data.data["environments"]["sys"]["analysis_ports"],[])

  def test_environment_package_declares_sequences_before_environment(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(BASE_YAML,encoding="utf-8")
      result = self.run_generator(config,output,"-g","environment:ip")
      self.assertEqual(result.returncode,0,result.stderr)

      package = (
        output / "verification_ip" / "environment_packages" /
        "ip_env_pkg" / "ip_env_pkg.sv"
      ).read_text(encoding="utf-8")
      config_include = '`include "src/ip_env_configuration.sv"'
      sequence_include = '`include "src/ip_env_sequence_base.sv"'
      environment_include = '`include "src/ip_environment.sv"'
      self.assertLess(package.index(config_include),package.index(sequence_include))
      self.assertLess(package.index(sequence_include),package.index("package_item_additional begin"))
      self.assertLess(package.index("package_item_additional end"),package.index(environment_include))
      self.assertLess(package.index(environment_include),package.index("package_item_after_environment begin"))

  def test_environment_generates_minimal_bazel_build(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(BASE_YAML,encoding="utf-8")
      result = self.run_generator(
        config,output,"-g","interface:bus","-g","environment:ip","-g","environment:soc"
      )
      self.assertEqual(result.returncode,0,result.stderr)

      ip_build = output / "verification_ip" / "environment_packages" / "ip_env_pkg" / "BUILD"
      soc_build = output / "verification_ip" / "environment_packages" / "soc_env_pkg" / "BUILD"
      bus_build = output / "verification_ip" / "interface_packages" / "bus_pkg" / "BUILD"
      self.assertIn('name = "pkg"',ip_build.read_text(encoding="utf-8"))
      bus_build_content = bus_build.read_text(encoding="utf-8")
      self.assertIn('name = "pkg"',bus_build_content)
      self.assertIn('"@vip_vcs_svt_pkg//:pkg"',bus_build_content)
      self.assertIn(
        '"//hw/dv/verification_ip/interface_packages/bus_pkg:pkg"',
        ip_build.read_text(encoding="utf-8"),
      )
      ip_build_content = ip_build.read_text(encoding="utf-8")
      in_flist = ip_build_content.split("in_flist =",1)[1].split("deps =",1)[0]
      self.assertIn('"src/ip_env_typedefs.svh"',in_flist)
      self.assertIn("pragma uvmf custom in_flist_prepend begin",in_flist)
      self.assertIn('"@dv_common//cmn:pkg"',ip_build_content)
      self.assertIn('"@cluelib_pkg//:pkg"',ip_build_content)
      self.assertIn('"@svlib_pkg//:pkg"',ip_build_content)
      self.assertIn(
        '"//hw/dv/verification_ip/environment_packages/ip_env_pkg:pkg"',
        soc_build.read_text(encoding="utf-8"),
      )
      self.assertIn("pragma uvmf custom deps_additional begin",soc_build.read_text(encoding="utf-8"))
      for sv_file in output.rglob("*.sv"):
        self.assertNotIn("import bus_pkg_hdl::*;",sv_file.read_text(encoding="utf-8"),str(sv_file))

  def test_bench_generates_minimal_bazel_builds(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(BASE_YAML,encoding="utf-8")
      result = self.run_generator(config,output,"-g","bench:soc")
      self.assertEqual(result.returncode,0,result.stderr)

      tb = output / "project_benches" / "soc" / "tb"
      tb_build = (tb / "BUILD").read_text(encoding="utf-8")
      self.assertIn('name = "soc_tb"',tb_build)
      self.assertIn('tb_warning_waivers = [',tb_build)
      self.assertIn('"SYNOPSYS_SV": ""',tb_build)
      self.assertIn('"-timescale=1ns/1ps"',tb_build)
      self.assertIn('"-top hdl_top -top hvl_top"',tb_build)
      self.assertIn('"//hw/dv/verification_ip/environment_packages/soc_env_pkg:pkg"',tb_build)
      self.assertIn('"//hw/dv/project_benches/soc/tb/testbench:hdl"',tb_build)
      self.assertIn('"//hw/dv/project_benches/soc/tb/tests:tests"',tb_build)
      self.assertIn('top_deps = base_deps + rtl_deps + tb_deps',tb_build)
      self.assertNotIn("coverage.ccf",tb_build)
      self.assertNotIn("soc_tb_cfg.sv",tb_build)
      self.assertIn('simulator = "VCS"',tb_build)
      self.assertIn("pragma uvmf custom tb_attributes begin",tb_build)
      self.assertIn("pragma uvmf custom additional_tbs begin",tb_build)
      self.assertIn('name = "pkg"',(tb / "parameters" / "BUILD").read_text(encoding="utf-8"))
      testbench_build = (tb / "testbench" / "BUILD").read_text(encoding="utf-8")
      self.assertIn('exports_files(glob(["*.svh"]))',testbench_build)
      self.assertNotIn('"hdl_interconnect_macros.sv"',testbench_build)
      self.assertLess(testbench_build.index('"hdl_top.sv"'),testbench_build.index('"hvl_top.sv"'))
      tests_build = (tb / "tests" / "BUILD").read_text(encoding="utf-8")
      self.assertIn('load(":demo_tests.bzl", "demo_test_configs")',tests_build)
      self.assertIn('name = "base"',tests_build)
      self.assertNotIn('"//hw/dv/project_benches/soc/tb/testbench:tb_defines.svh"',tests_build)
      self.assertIn("pragma uvmf custom in_flist_prepend begin",tests_build)
      self.assertIn('"+wdog=": "1000000"',tests_build)
      self.assertIn("pragma uvmf custom test_bzl_loads begin",tests_build)
      self.assertIn("pragma uvmf custom sim_opts begin",tests_build)
      self.assertIn("pragma uvmf custom additional_test_cfgs begin",tests_build)
      self.assertIn("demo_test_configs()",tests_build)
      self.assertIn('tb = "//hw/dv/project_benches/soc/tb:soc_tb"',tests_build)
      demo_tests = (tb / "tests" / "demo_tests.bzl").read_text(encoding="utf-8")
      self.assertIn('inherits = [":base"]',demo_tests)
      self.assertIn('uvm_testname = "test_top"',demo_tests)
      tests_pkg = (tb / "tests" / "soc_tests_pkg.sv").read_text(encoding="utf-8")
      self.assertNotIn("import bus_pkg::*;",tests_pkg)
      self.assertNotIn("import bus_pkg_hdl::*;",tests_pkg)
      self.assertFalse((tb / "sequences" / "BUILD").exists())

  def test_overwrite_existing_output_requires_merge(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      sentinel = output / "custom.sv"
      config.write_text(BASE_YAML,encoding="utf-8")
      output.mkdir()
      sentinel.write_text("user content\n",encoding="utf-8")

      result = self.run_generator(config,output,"-o")

      self.assertNotEqual(result.returncode,0)
      self.assertIn("Refusing -o/--overwrite on non-empty output",result.stderr+result.stdout)
      self.assertEqual(sentinel.read_text(encoding="utf-8"),"user content\n")

  def test_unknown_target_profile_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      config.write_text(BASE_YAML,encoding="utf-8")
      result = self.run_generator(
        config,root / "output","--target_profile=vcs_xcelium_typo"
      )
      self.assertNotEqual(result.returncode,0)
      self.assertIn("Unknown target profile",result.stderr+result.stdout)

  def test_merge_rejects_a_different_destination(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      source = root / "source"
      config.write_text(BASE_YAML,encoding="utf-8")
      source.mkdir()

      result = self.run_generator(
        config,root / "different","--merge_source="+str(source)
      )

      self.assertNotEqual(result.returncode,0)
      self.assertIn("must match --merge_source",result.stderr+result.stdout)

  def test_merge_defaults_destination_to_source(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      source = root / "source"
      config.write_text(BASE_YAML,encoding="utf-8")
      self.assertEqual(self.run_generator(config,source,"-g","bench:soc").returncode,0)

      result = subprocess.run(
        [sys.executable,str(REPO_ROOT / "scripts" / "yaml2uvmf.py"),"-q","-g","bench:soc","--merge_source="+str(source),str(config)],
        cwd=root,text=True,capture_output=True,check=False,
      )

      self.assertEqual(result.returncode,0,result.stderr)
      self.assertTrue(Path(str(source)+"_bak_0").is_dir())

  def test_merge_preserves_custom_blocks_and_project_files(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(BASE_YAML,encoding="utf-8")
      first = self.run_generator(config,output,"-g","bench:soc")
      self.assertEqual(first.returncode,0,first.stderr)

      build = output / "project_benches" / "soc" / "tb" / "BUILD"
      content = build.read_text(encoding="utf-8").replace(
        "    # pragma uvmf custom tb_deps end",
        '    "//hw/dv/custom:pkg",\n'
        "    # pragma uvmf custom tb_deps end",
      )
      build.write_text("# outside custom block\n"+content,encoding="utf-8")
      tests_build = output / "project_benches" / "soc" / "tb" / "tests" / "BUILD"
      content = tests_build.read_text(encoding="utf-8")
      content = content.replace(
        "# pragma uvmf custom test_bzl_loads end",
        'load(":custom_tests.bzl", "custom_test_configs")\n'
        "# pragma uvmf custom test_bzl_loads end",
      ).replace(
        "        # pragma uvmf custom sim_opts end",
        '        "+custom=": "1",\n'
        "        # pragma uvmf custom sim_opts end",
      ).replace(
        "# pragma uvmf custom additional_test_cfgs end",
        'verilog_dv_test_cfg(name = "custom", inherits = [":base"])\n'
        "# pragma uvmf custom additional_test_cfgs end",
      ).replace(
        "# pragma uvmf custom test_configs end",
        "custom_test_configs()\n"
        "# pragma uvmf custom test_configs end",
      )
      tests_build.write_text(content,encoding="utf-8")
      content = build.read_text(encoding="utf-8").replace(
        "    # pragma uvmf custom tb_attributes end",
        '    tags = ["custom"],\n'
        "    # pragma uvmf custom tb_attributes end",
      ).replace(
        "# pragma uvmf custom additional_tbs end",
        'verilog_dv_tb(name = "extra_tb", deps = top_deps)\n'
        "# pragma uvmf custom additional_tbs end",
      )
      build.write_text(content,encoding="utf-8")
      project_file = output / "project_benches" / "soc" / "tb" / "user_owned.sv"
      project_file.write_text("module user_owned; endmodule\n",encoding="utf-8")
      obsolete_file = output / "verification_ip" / "legacy.compile"
      obsolete_file.parent.mkdir(parents=True,exist_ok=True)
      obsolete_file.write_text("obsolete\n",encoding="utf-8")
      obsolete_dir = output / "project_benches" / "soc" / "sim"
      obsolete_dir.mkdir()
      (obsolete_dir / "old.sv").write_text("obsolete\n",encoding="utf-8")

      merged = self.run_generator(
        config,output,"-g","bench:soc","--merge_source="+str(output)
      )

      self.assertEqual(merged.returncode,0,merged.stderr)
      self.assertIn("//hw/dv/custom:pkg",build.read_text(encoding="utf-8"))
      self.assertIn('tags = ["custom"]',build.read_text(encoding="utf-8"))
      self.assertIn('name = "extra_tb"',build.read_text(encoding="utf-8"))
      self.assertIn('load(":custom_tests.bzl", "custom_test_configs")',tests_build.read_text(encoding="utf-8"))
      self.assertIn('"+custom=": "1"',tests_build.read_text(encoding="utf-8"))
      self.assertIn('name = "custom"',tests_build.read_text(encoding="utf-8"))
      self.assertIn("custom_test_configs()",tests_build.read_text(encoding="utf-8"))
      self.assertEqual(project_file.read_text(encoding="utf-8"),"module user_owned; endmodule\n")
      self.assertFalse(obsolete_file.exists())
      self.assertFalse(obsolete_dir.exists())
      backup = Path(str(output)+"_bak_0")
      self.assertTrue(backup.is_dir())
      self.assertTrue((backup / "verification_ip" / "legacy.compile").is_file())
      self.assertTrue((backup / "project_benches" / "soc" / "sim" / "old.sv").is_file())
      backup_build = backup / "project_benches" / "soc" / "tb" / "BUILD"
      self.assertIn("# outside custom block",backup_build.read_text(encoding="utf-8"))

  def test_merge_preserves_exported_tb_defines_used_by_environment_build(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(BASE_YAML,encoding="utf-8")
      first = self.run_generator(
        config,output,"-g","environment:soc","-g","bench:soc"
      )
      self.assertEqual(first.returncode,0,first.stderr)

      tb_defines = output / "project_benches" / "soc" / "tb" / "testbench" / "tb_defines.svh"
      tb_defines.write_text("`define SOC_TB_DEFINE 1\n",encoding="utf-8")
      env_build = output / "verification_ip" / "environment_packages" / "soc_env_pkg" / "BUILD"
      env_build.write_text(
        env_build.read_text(encoding="utf-8").replace(
          "        # pragma uvmf custom in_flist_prepend end",
          '        "//hw/dv/project_benches/soc/tb/testbench:tb_defines.svh",\n'
          "        # pragma uvmf custom in_flist_prepend end",
        ),
        encoding="utf-8",
      )

      merged = self.run_generator(
        config,output,"-g","environment:soc","-g","bench:soc","--merge_source="+str(output)
      )

      self.assertEqual(merged.returncode,0,merged.stderr)
      self.assertEqual(tb_defines.read_text(encoding="utf-8"),"`define SOC_TB_DEFINE 1\n")
      self.assertIn(
        "//hw/dv/project_benches/soc/tb/testbench:tb_defines.svh",
        env_build.read_text(encoding="utf-8"),
      )
      backup = Path(str(output)+"_bak_0")
      self.assertTrue((backup / "project_benches" / "soc" / "tb" / "testbench" / "tb_defines.svh").is_file())

  def test_virtual_sequence_base_fails_fast(self):
    content = (
      REPO_ROOT / "uvmf_base_pkg" / "src" / "uvmf_virtual_sequence_base.svh"
    ).read_text(encoding="utf-8")
    self.assertGreaterEqual(content.count('`uvm_fatal("VSQR"'),3)

  def test_counted_subenvironments_expand_to_independent_instances(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(
        "uvmf:\n"
        "  environments:\n"
        "    svt_apb: {}\n"
        "    soc:\n"
        "      subenvs:\n"
        "        - {name: ucie_apb_env, type: svt_apb, count: 6}\n",
        encoding="utf-8",
      )
      result = self.run_generator(config,output,"-g","environment:soc")
      self.assertEqual(result.returncode,0,result.stderr)
      environment = output / "verification_ip" / "environment_packages" / "soc_env_pkg" / "src" / "soc_environment.sv"
      content = environment.read_text(encoding="utf-8")
      for index in range(6):
        self.assertIn("ucie_apb_env_{0}_t ucie_apb_env_{0};".format(index),content)
      self.assertNotIn("ucie_apb_env[",content)

  def test_counted_subenvironment_rejects_shared_register_block_name(self):
    with tempfile.TemporaryDirectory() as tmp:
      config = Path(tmp) / "soc.yaml"
      config.write_text(
        "uvmf:\n"
        "  environments:\n"
        "    ip: {}\n"
        "    soc:\n"
        "      subenvs:\n"
        "        - name: ip_env\n"
        "          type: ip\n"
        "          count: 2\n"
        "          reg_block_instance_name: shared_rm\n",
        encoding="utf-8",
      )
      data = self.data_object()
      data.parseFile(str(config))
      with self.assertRaisesRegex(UserError,r"requires \{index\} in reg_block_instance_name"):
        data.validate()

  def test_counted_subenvironment_expands_register_names_and_addresses(self):
    with tempfile.TemporaryDirectory() as tmp:
      config = Path(tmp) / "soc.yaml"
      config.write_text(
        "uvmf:\n"
        "  environments:\n"
        "    ip:\n"
        "      register_model:\n"
        "        reg_model_package: ip_reg_pkg\n"
        "        reg_block_class: ip_reg_block\n"
        "    soc:\n"
        "      subenvs:\n"
        "        - name: ip\n"
        "          type: ip\n"
        "          count: 2\n"
        "          use_register_model: true\n"
        "          reg_block_instance_name: ip_{index}_rm\n"
        "          base_address: BASE_ADDR + {index} * IP_STRIDE\n"
        "      register_model:\n"
        "        use_adapter: false\n"
        "        use_explicit_prediction: false\n",
        encoding="utf-8",
      )
      output = Path(tmp) / "output"
      result = self.run_generator(config,output,"-g","environment:soc")
      self.assertEqual(result.returncode,0,result.stderr)
      model = output / "verification_ip" / "environment_packages" / "soc_env_pkg" / "registers" / "soc_reg_model.sv"
      content = model.read_text(encoding="utf-8")
      self.assertIn('default_map = create_map("default_map"',content)
      self.assertIn("ip_0_rm.configure(this);",content)
      self.assertIn("ip_1_rm.configure(this);",content)
      self.assertNotIn("add_block(",content)
      self.assertNotIn("//package",content)
      self.assertNotIn("example_reg",content)
      self.assertNotRegex(content,r"(?m)^\s*///+")
      self.assertNotRegex(content,r"(?m)^\s*//\s*Function:")
      self.assertIn("default_map.add_submap(ip_0_rm.default_map, BASE_ADDR + 0 * IP_STRIDE);",content)
      self.assertIn("default_map.add_submap(ip_1_rm.default_map, BASE_ADDR + 1 * IP_STRIDE);",content)

  def test_subenvironment_base_address_requires_parent_register_model(self):
    with tempfile.TemporaryDirectory() as tmp:
      config = Path(tmp) / "soc.yaml"
      config.write_text(
        "uvmf:\n"
        "  environments:\n"
        "    ip:\n"
        "      register_model: {}\n"
        "    soc:\n"
        "      subenvs:\n"
        "        - {name: ip0, type: ip, use_register_model: true, base_address: 4096}\n",
        encoding="utf-8",
      )
      data = self.data_object()
      data.parseFile(str(config))
      with self.assertRaisesRegex(UserError,"requires register_model"):
        data.validate()

  def test_legacy_qvip_yaml_keys_are_normalized_to_vip(self):
    with tempfile.TemporaryDirectory() as tmp:
      config = Path(tmp) / "legacy.yaml"
      config.write_text(
        "uvmf:\n"
        "  qvip_environments:\n"
        "    legacy_vip:\n"
        "      agents:\n"
        "        - name: vip0\n"
        "          imports: [legacy_pkg]\n"
        "  environments:\n"
        "    soc:\n"
        "      qvip_subenvs:\n"
        "        - {name: vip_env0, type: legacy_vip}\n",
        encoding="utf-8",
      )
      data = self.data_object()
      data.parseFile(str(config))
      self.assertIn("legacy_vip",data.data["vip_environments"])
      self.assertIn("vip_subenvs",data.data["environments"]["soc"])
      self.assertNotIn("qvip_subenvs",data.data["environments"]["soc"])
      with self.assertRaisesRegex(UserError,"Legacy VIP Configurator"):
        data.validate()
      data.validate("legacy")

  def test_synopsys_vip_uses_regular_external_subenvironment(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(
        "uvmf:\n"
        "  environments:\n"
        "    svt_apb:\n"
        "      existing_library_component: true\n"
        "    soc:\n"
        "      subenvs:\n"
        "        - {name: apb0, type: svt_apb}\n",
        encoding="utf-8",
      )
      result = self.run_generator(config,output,"-g","environment:soc")
      self.assertEqual(result.returncode,0,result.stderr)
      environment = output / "verification_ip" / "environment_packages" / "soc_env_pkg" / "src" / "soc_environment.sv"
      content = environment.read_text(encoding="utf-8")
      self.assertIn("svt_apb_environment apb0",content)
      self.assertNotIn("mvc_sequencer",content)
      self.assertNotIn("qvip",content.lower())

  def test_generated_sv_has_guards_and_named_terminators(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(BASE_YAML,encoding="utf-8")
      result = self.run_generator(config,output,"-g","environment:soc","-g","bench:soc")
      self.assertEqual(result.returncode,0,result.stderr)
      for path in list(output.rglob("*.sv"))+list(output.rglob("*.svh")):
        suffix = path.suffix[1:].upper()
        guard = "_{}__{}__".format(path.stem.upper(),suffix)
        content = path.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("`ifndef {}\n`define {}\n".format(guard,guard)),path)
        self.assertTrue(content.rstrip().endswith("`endif // {}".format(guard)),path)
        self.assertNotRegex(content,r"(?m)^\s*end(?:class|function|task|package|module|interface|group)\s*(?://.*)?$")
        self.assertNotRegex(content,r"(?m)^\s*//\s*(?:FUNCTION|TASK)\s*:")
        if path.suffix == ".svh":
          self.assertTrue(path.name.lower().endswith(SVH_KEEP_SUFFIXES),path)

  def test_templates_keep_only_allowed_svh_references(self):
    template_root = REPO_ROOT / "templates" / "python" / "template_files"
    for path in template_root.rglob("*.TMPL"):
      content = path.read_text(encoding="utf-8")
      for token in re.findall(r"[A-Za-z0-9_./{}-]+\.svh\b",content):
        self.assert_allowed_template_svh(token,path)

  def test_virtual_sequence_exposes_typed_environment(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(BASE_YAML,encoding="utf-8")
      result = self.run_generator(config,output,"-g","environment:soc","-g","bench:soc")
      self.assertEqual(result.returncode,0,result.stderr)
      package = output / "verification_ip" / "environment_packages" / "soc_env_pkg"
      sequence = (package / "src" / "soc_env_sequence_base.sv").read_text(encoding="utf-8")
      environment = (package / "src" / "soc_environment.sv").read_text(encoding="utf-8")
      test_top = (output / "project_benches" / "soc" / "tb" / "tests" / "src" / "test_top.sv").read_text(encoding="utf-8")
      self.assertIn("type ENV_T = uvm_env",sequence)
      self.assertIn("env.ip0.vsqr",sequence)
      self.assertIn("vsqr.set_env(this)",environment)
      self.assertIn(".ENV_T(soc_environment_t)",test_top)
      self.assertIn("top_level_sequence.start(environment.vsqr)",test_top)
      tests_pkg = (output / "project_benches" / "soc" / "tb" / "tests" / "soc_tests_pkg.sv").read_text(encoding="utf-8")
      self.assertNotIn("soc_sequences_pkg",tests_pkg)
      self.assertNotIn("soc_bench_sequence_base",tests_pkg)

  def test_parent_register_model_requires_explicit_subenvironment_selection(self):
    base = (
      "uvmf:\n"
      "  environments:\n"
      "    ip:\n"
      "      register_model:\n"
      "        reg_model_package: ip_reg_pkg\n"
      "        reg_block_class: ip_reg_block\n"
      "        use_adapter: 'False'\n"
      "        use_explicit_prediction: 'False'\n"
      "    soc:\n"
      "      subenvs:\n"
      "        - name: ip0\n"
      "          type: ip\n"
      "{selection}"
      "      register_model:\n"
      "        use_adapter: 'False'\n"
      "        use_explicit_prediction: 'False'\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(base.format(selection=""),encoding="utf-8")
      result = self.run_generator(config,output,"-g","environment:soc")
      self.assertEqual(result.returncode,0,result.stderr)
      model = output / "verification_ip" / "environment_packages" / "soc_env_pkg" / "registers" / "soc_reg_model.sv"
      self.assertNotIn("ip_reg_block",model.read_text(encoding="utf-8"))

      config.write_text(base.format(selection="          use_register_model: 'True'\n"),encoding="utf-8")
      output = root / "updated_output"
      result = self.run_generator(config,output,"-g","environment:soc")
      self.assertEqual(result.returncode,0,result.stderr)
      model = output / "verification_ip" / "environment_packages" / "soc_env_pkg" / "registers" / "soc_reg_model.sv"
      self.assertIn("ip_reg_block ip0_rm",model.read_text(encoding="utf-8"))
      self.assertNotIn("import uvm_pkg::*",model.read_text(encoding="utf-8"))
      self.assertNotIn('`include "uvm_macros.svh"',model.read_text(encoding="utf-8"))
      archive = output / "verification_ip" / "environment_packages" / "soc_env_pkg" / "yaml" / "soc_environment.yaml"
      self.assertIn("use_register_model: true",archive.read_text(encoding="utf-8"))

  def test_check_mode_is_read_only_and_reports_stale_output(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "soc.yaml"
      output = root / "output"
      config.write_text(BASE_YAML,encoding="utf-8")
      clean = self.run_generator(config,output,"--check")
      self.assertEqual(clean.returncode,0,clean.stderr)
      self.assertFalse(output.exists())

      stale_dir = output / "verification_ip" / "environment_packages" / "soc_env_pkg" / "src"
      stale_dir.mkdir(parents=True)
      stale = stale_dir / "soc_environment.svh"
      stale.write_text("// Created with uvmf_gen version 2023.4_2\n",encoding="utf-8")
      (stale_dir / "soc_environment.sv").write_text("class soc_environment; endclass\n",encoding="utf-8")
      before = stale.read_text(encoding="utf-8")
      result = self.run_generator(config,output,"--check")
      self.assertNotEqual(result.returncode,0)
      self.assertIn("soc_environment.svh",result.stdout+result.stderr)
      self.assertEqual(stale.read_text(encoding="utf-8"),before)


if __name__ == "__main__":
  unittest.main()
