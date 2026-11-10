#!/usr/bin/env python3
"""Behavioral self-checks using temporary RTL; no hardware build or dependencies."""
import contextlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import rtl_depth as rd


class DepthChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def index(self, source):
        rtl = self.root / "input.sv"
        rtl.write_text(source)
        index = self.root / "index.json"
        with contextlib.redirect_stdout(io.StringIO()):
            rd.make_index(Namespace(rtl=[str(rtl)], file_list=None, file_list_base=None, define=None, output=str(index)))
        return index

    def trace(self, index, signals, top="Top", **kwargs):
        output = self.root / "report.json"
        args = Namespace(index=str(index), top=top, signal=signals, allow_stale=False, max_nodes=10000,
                         json=str(output), markdown=str(self.root / "report.md"), require_complete=True)
        for key, value in kwargs.items():
            setattr(args, key, value)
        code = rd.trace(args)
        return code, json.loads(output.read_text())

    def test_cross_module_bidirectional_depth_and_width(self):
        idx = self.index("""
module Child(input [7:0] a, input [7:0] b, output [7:0] y);
  wire [8:0] sum = a + b;
  wire cmp = sum > 9'd7;
  assign y = cmp ? sum[7:0] : b;
endmodule
module Top(input clock, input [7:0] in, output [7:0] out);
  reg [7:0] q;
  reg [7:0] sink;
  wire [7:0] w;
  Child u(.a(q), .b(in), .y(w));
  always @(posedge clock) begin q <= in; sink <= w ^ 8'h5a; end
  assign out = sink;
endmodule
""")
        code, data = self.trace(idx, ["w", "u.sum", "sink"])
        self.assertEqual(code, 0)
        wire, summed, reg = data["reports"]
        self.assertEqual((wire["upstream"]["depth"], wire["downstream"]["depth"]), (3, 1))
        self.assertEqual(wire["through_signal"]["depth"], 4)
        self.assertEqual((summed["upstream"]["depth"], summed["downstream"]["depth"]), (1, 3))
        self.assertEqual(reg["upstream"]["depth"], 4)
        self.assertIsNone(reg["through_signal"])
        self.assertEqual(wire["through_signal"]["composition"], {"arithmetic": 1, "comparison": 1, "mux": 1, "logic": 1})
        adds = [n for n in wire["through_signal"]["operators"] if n["operator"] == "+"]
        self.assertEqual(adds[0]["result_width"], 9)
        self.assertEqual(wire["through_signal"]["destination"], "Top/sink$D")

    def test_register_enable_and_sync_reset(self):
        idx = self.index("""
module Top(input clock, input reset, input en, input [3:0] a, output [3:0] out);
reg [3:0] q;
always @(posedge clock) begin if(reset) q <= 4'd0; else if(en) q <= a + 4'd1; end
assign out=q;
endmodule
""")
        code, data = self.trace(idx, ["q"])
        self.assertEqual(code, 0)
        r = data["reports"][0]
        self.assertEqual(r["upstream"]["depth"], 3)
        self.assertIn("reg_q", {b["kind"] for b in r["upstream"]["terminals"]})

    def test_async_reset_is_flop_control(self):
        idx = self.index("""
module Top(input clock, input reset, input a, output reg q);
always @(posedge clock or posedge reset) begin if(reset) q <= 1'b0; else q <= ~a; end
endmodule
""")
        code, data = self.trace(idx, ["q"])
        self.assertEqual(code, 0)
        self.assertEqual(data["reports"][0]["upstream"]["depth"], 1)

    def test_opaque_function_and_memory_are_partial(self):
        idx = self.index("""
module Top(input [3:0] a, input addr, output [3:0] f, output [3:0] m);
reg [3:0] Memory[0:1];
assign f = opaque(a);
assign m = Memory[addr];
endmodule
""")
        code, data = self.trace(idx, ["f", "m", "a"])
        self.assertEqual(code, 2)
        self.assertTrue(all(r["status"] == "partial" for r in data["reports"]))

    def test_case_does_not_silently_drop_register_driver(self):
        idx = self.index("""
module Top(input clock, input sel, input a, output reg q);
always @(posedge clock) begin case(sel) 1'b0: q <= a; default: q <= ~a; endcase end
endmodule
""")
        code, data = self.trace(idx, ["q"])
        self.assertEqual(code, 2)
        self.assertTrue(data["reports"][0]["upstream"]["incomplete"])

    def test_cycle_and_budget_are_partial(self):
        idx = self.index("""
module Top(input a, output x, output y);
assign x = a & ~y;
assign y = x;
endmodule
""")
        code, data = self.trace(idx, ["x"])
        self.assertEqual(code, 2)
        self.assertTrue(data["reports"][0]["upstream"]["cycles"])
        code, data = self.trace(idx, ["x"], max_nodes=2)
        self.assertEqual(code, 2)
        self.assertTrue(data["reports"][0]["upstream"]["node_limit_reached"])

    def test_synthesis_preprocessing_and_stale_input(self):
        idx = self.index("""
module Top(input a, output y);
`ifndef SYNTHESIS
unsupported_simulation_stuff;
`endif
assign y=~a;
endmodule
""")
        code, data = self.trace(idx, ["y"])
        self.assertEqual(code, 0)
        self.assertEqual(data["reports"][0]["upstream"]["depth"], 1)
        (self.root / "input.sv").write_text("changed")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.trace(idx, ["y"])

    def test_firrtl_locator_diff(self):
        fir = self.root / "in.fir"
        fir.write_text("FIRRTL version 7.0.0\ncircuit Top :\n  module Top :\n    node y = and(a,b) @[src/Thing.scala 12:3]\n")
        diff = self.root / "change.diff"
        diff.write_text("--- a/src/Thing.scala\n+++ b/src/Thing.scala\n@@ -12 +12 @@\n-old\n+new\n")
        output = self.root / "locations.json"
        rd.locate(Namespace(firrtl=[str(fir)], source=None, match=None, diff=str(diff), line_slop=0, limit=10, output=str(output)))
        data = json.loads(output.read_text())
        self.assertEqual(len(data["hits"]), 1)
        self.assertEqual(data["hits"][0]["module"], "Top")

    def test_release_size_cast_and_packed_selection(self):
        idx = self.index("""
module Top(input clock, input [1:0] addr, input [3:0] a, input [3:0] b, output [3:0] out);
wire [3:0][3:0] table = '{4'h0, a, b, 4'h3};
wire [3:0] selected = table[addr];
reg [3:0] q;
always @(posedge clock) q <= 4'(selected + 4'h1);
assign out = q;
endmodule
""")
        code, data = self.trace(idx, ["q", "table", "selected"])
        self.assertEqual(code, 0)
        reg, table, selected = data["reports"]
        self.assertEqual((reg["upstream"]["depth"], table["width"], selected["width"]), (2, 16, 4))
        self.assertEqual(reg["upstream"]["path"]["composition"], {"select": 1, "arithmetic": 1})
        self.assertEqual([x["result_width"] for x in reg["upstream"]["path"]["operators"]], [4, 4])

    def test_forward_partial_write_and_memory_inputs(self):
        idx = self.index("""
module Top(input clock, input en, input addr, input [3:0] a, output reg [3:0] q);
reg [3:0] Memory[0:1];
always @(posedge clock) begin
  if(en) Memory[addr][0 +: 4] <= a + 4'd1;
  q[0] <= a[0];
end
endmodule
""")
        code, data = self.trace(idx, ["a", "addr", "en"])
        self.assertEqual(code, 2)
        for r in data["reports"]:
            self.assertEqual(r["status"], "partial")
            self.assertIn("memory", {b["kind"] for b in r["downstream"]["terminals"]})
        self.assertEqual(data["reports"][0]["downstream"]["depth"], 1)
        self.assertFalse(any("unsupported expression" in w["message"] for w in data["coverage"]["warnings"]))

    def test_port_context_comparison_and_markdown_pipe(self):
        idx = self.index("""
module Child(input [8:0] a, output y);
assign y = (a | 9'h1) != 9'd0;
endmodule
module Top(input [3:0] a, input [3:0] b, output out);
Child u(.a(a+b), .y(out));
endmodule
""")
        code, data = self.trace(idx, ["out"])
        self.assertEqual(code, 0)
        r = data["reports"][0]
        self.assertEqual(r["upstream"]["depth"], 3)
        self.assertEqual([x["result_width"] for x in r["upstream"]["path"]["operators"]], [9, 9, 1])
        self.assertIn("`\\|`", (self.root / "report.md").read_text())

    def test_deletion_only_diff_is_not_all_firrtl(self):
        fir = self.root / "in.fir"
        fir.write_text("circuit Top :\n  module Top :\n    node y = and(a,b) @[src/Thing.scala 12:3]\n")
        diff = self.root / "change.diff"
        diff.write_text("--- a/src/Thing.scala\n+++ b/src/Thing.scala\n@@ -12 +11,0 @@\n-old\n")
        output = self.root / "locations.json"
        rd.locate(Namespace(firrtl=[str(fir)], source=None, match=None, diff=str(diff), line_slop=0, limit=10, output=str(output)))
        self.assertEqual(json.loads(output.read_text())["hits"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
