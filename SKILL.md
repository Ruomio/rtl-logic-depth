---
name: rtl-logic-depth
description: Analyze register-to-register combinational operator depth in generated Verilog/SystemVerilog, tracing both directions across module ports from a signal or Chisel change. Use Release RTL as the structure authority and FIRRTL locators to find Chisel origins; report operator kinds, widths, evidence, and unsupported boundaries. Not a substitute for synthesis timing or STA.
---

# RTL logic depth

Use the bundled Python 3.9+ standard-library CLI from any shell or coding agent. Resolve `scripts/rtl_depth.py` relative to this skill directory; do not assume a checkout path, virtual environment, CLI vendor, or current working directory. The entire folder is portable.

Read [references/model.md](references/model.md) for the metric, supported syntax, and completeness contract before interpreting a result. Read [references/workflow.md](references/workflow.md) for artifact generation, Chisel mapping, comparisons, and command examples.

## Working procedure

1. Identify the project root, intended Release RTL snapshot, top module, and signal or Chisel diff. Check real paths and repository status. Preserve existing artifacts. Do not silently mix `build`, old simulator builds, and extracted Release directories. Record revision/configuration/tool versions when available; source hashes in the index identify the actual inputs.
2. Reuse a matching existing RTL artifact. Generation is a separate, potentially expensive build and is not needed just to query existing files. User-supplied build commands and environment activation take precedence; never hardcode a Python environment. The XiangShan recipe and the `CONFIGIG`/`CONFIG` spelling caveat are documented in the workflow reference.
3. For a Chisel change, run `locate` on FIRRTL using a source locator or unified diff. Treat hits as candidates: check deduplication, renaming, optimizations, instance ports, and adjacent generated expressions in Release RTL. Do not claim a one-to-one mapping from a name substring. FIRRTL is not the depth authority when the requested metric is the optimized Release structure.
4. Build one reusable JSON module index with `index --rtl ... --output ...`, or use the generated `.f` file. Select a coherent set of sources with one definition per module. Inspect index unknown counts and diagnostics. Use `list` to discover instance and signal names; trace an exact hierarchical signal with `trace`.
5. Trace both directions to Q/D, top I/O, or explicit unknown/memory boundaries. A register query reports incoming D and outgoing Q separately. A wire query also reports the longest structural path through it. Query multiple signals in one invocation to reuse loaded templates and lazy contexts. Keep the index outside the skill and outside tracked source directories unless the user chooses otherwise.
6. Inspect the JSON boundary list and operator evidence, not just the depth number. Review critical-path RTL lines and module-port joins. Unsupported or opaque logic makes the result partial; do not replace unknown depth with zero or describe partial values as a proven maximum. If the needed syntax is unsupported, narrow the input or use an available mature elaborating frontend and preserve this metric and boundary contract.

## Deliverable

Report the input snapshot/top and exact signal; upstream/downstream boundaries and depths; critical-path mux/arithmetic/comparison/logic/shift composition; operator result and operand widths; source lines and hierarchy; coverage gaps; and the FIRRTL-to-Chisel candidate evidence where applicable. Distinguish operator depth from gate depth, fanout, timing slack, and feasible sensitizable paths. For before/after comparisons, use the same metric, top, generation options, boundary handling, and verified signal mapping.

Use `--require-complete` when an automated caller must reject partial results (exit 2). Exit 0 means the query completed under the documented source-level signal model; it does not prove synthesis timing. Exit 1 means invalid inputs or an execution error.

The maintained self-check is `python3 scripts/selftest.py`; it uses temporary files and exercises observable cross-module and boundary behavior. It does not modify the hardware project.
