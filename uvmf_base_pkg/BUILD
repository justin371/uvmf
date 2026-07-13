load("@rules_verilog//verilog:defs.bzl", "verilog_dv_library")

package(default_visibility = ["//visibility:public"])

verilog_dv_library(
    name = "pkg",
    srcs = glob([
        "*.sv*",
        "src/*.sv*",
    ]),
    in_flist = glob(["*_pkg_hdl.sv"]) + glob(["*_pkg.sv"]),
)
