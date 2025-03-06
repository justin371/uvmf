workspace(name = "UVMF_2023.4_2")

load("@bazel_tools//tools/build_defs/repo:git.bzl", "git_repository")
load("@bazel_tools//tools/build_defs/repo:utils.bzl", "maybe")

maybe(
    name = "rules_verilog",
    repo_rule = git_repository,
    commit = "1917b9d29ad8819fd4840bd394e216f1dc2bf347",
    remote = "git@idc-code1.int.lightelligence.co:rtl_dv_dev/dv_dev/rules_verilog.git",
    shallow_since = "1709271762 +0800",
)
