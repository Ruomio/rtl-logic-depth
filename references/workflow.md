# Commands, generation, and Chisel mapping

## Portable invocation

Replace paths below with actual values. `PYTHON` is the caller's Python 3.9+ executable from its own environment. The analyzer requires no virtual environment or dependency installation.

```bash
PYTHON=python3
SKILL_DIR=/path/to/rtl-logic-depth
OUT_DIR=/path/to/analysis-output
RTL_DIR=/path/to/selected-release-rtl
TOP=SelectedTopModule
mkdir -p "$OUT_DIR"
"$PYTHON" "$SKILL_DIR/scripts/rtl_depth.py" index \
  --rtl "$RTL_DIR" --output "$OUT_DIR/rtl-index.json"
"$PYTHON" "$SKILL_DIR/scripts/rtl_depth.py" list \
  --index "$OUT_DIR/rtl-index.json" --top "$TOP" --match 'ready|valid'
"$PYTHON" "$SKILL_DIR/scripts/rtl_depth.py" list \
  --index "$OUT_DIR/rtl-index.json" --top "$TOP" --instance "$TOP/child"
"$PYTHON" "$SKILL_DIR/scripts/rtl_depth.py" trace \
  --index "$OUT_DIR/rtl-index.json" --top "$TOP" \
  --signal 'child/internal_wire' --signal 'child/state_register' \
  --json "$OUT_DIR/depth.json" --markdown "$OUT_DIR/depth.md"
```

Top-relative paths accept `/` or ordinary `.` hierarchy separators. `list` shows child instance names and signal widths/kinds. Escaped identifiers containing hierarchy separators are not supported by CLI naming.

For a generated `.f` list, replace `--rtl` with `--file-list "$RELEASE_DIR/generated_top.f"`. Relative paths resolve against the list's parent unless `--file-list-base` is supplied. Nested `-f`/`-F` lists are accepted. `+incdir+` does not expand includes; active includes become diagnostics. Tool options such as `+define+` are rejected: use repeated `--define`. Duplicate modules fail explicitly. Select exactly one coherent source snapshot.

## XiangShan generation

Prefer an existing verified Release snapshot. The user's supplied recipe is:

```bash
cd "$PROJECT_ROOT"
export NOOP_HOME=$(pwd)
make sim-verilog CONFIGIG=DefaultConfig RELEASE=1
# Activate the user's own Python environment if extraction needs it.
bash tmp/generate_all.sh
```

The result resembles `bosc_XXX-Release-DefaultConfig-<date>`. Treat that name as a hint, not a hardcoded input path. Inspect the script's selected modules/configuration and file lists. The extracted top may have a `bosc_` prefix while child module names remain unchanged.

`CONFIGIG` above preserves the supplied command. In the examined XiangShan Makefile the actual variable is `CONFIG ?= DefaultConfig`; `CONFIGIG` has no configuration effect there and leaves the default selected. Check the current Makefile and use `CONFIG=...` for an explicit configuration. Do not claim `CONFIGIG` selected a nondefault configuration.

For a configurable invocation, set the project root and configuration explicitly. This is an optional build step, independent of the analyzer:

```bash
PROJECT_ROOT=/path/to/XiangShan
XS_CONFIG=DefaultConfig
cd "$PROJECT_ROOT"
export NOOP_HOME="$PROJECT_ROOT"
make sim-verilog CONFIG="$XS_CONFIG" RELEASE=1
```

The examined `tmp/generate_all.sh` itself fixes extraction to `L2Top`, `DefaultConfig`, `bosc_` and `python3`. Changing only the Makefile configuration does not update those extraction arguments. For another configuration/module or a chosen Python executable, inspect the project's parser interface and invoke it explicitly in a fresh output directory:

```bash
EXTRACT_PYTHON=/path/to/user-environment/bin/python
EXTRACT_MODULE=L2Top
EXTRACT_PREFIX=bosc_
EXTRACT_OUT=/path/to/new-extraction-directory
mkdir -p "$EXTRACT_OUT"
cd "$EXTRACT_OUT"
"$EXTRACT_PYTHON" "$PROJECT_ROOT/scripts/parser.py" "$EXTRACT_MODULE" \
  --config "$XS_CONFIG" --prefix "$EXTRACT_PREFIX" --sram-replace \
  --xs-home "$PROJECT_ROOT"
```

Artifact discovery can be done without building: `rg --files --hidden --no-ignore "$PROJECT_ROOT" -g '*.f' -g '*.fir'`. Inspect the candidate file lists and choose a matching Release snapshot explicitly with `--file-list`; use `--file-list-base` if its relative paths use a different base. `--rtl`, `--top`, `--firrtl` and output paths are also caller parameters. The helper never activates an environment or launches hardware generation automatically.

FIRRTL generation is `make sim-chirrtl`. Record revision/submodule revisions, config/top, Release flags, firtool version, and whether artifacts predate edits. Old `build_verilator` and current `build/rtl` may represent different snapshots. A folder name or timestamp alone does not establish equivalence. Release RTL may omit source locations while FIRRTL preserves them.

## Chisel changes to Release signals

```bash
git -C "$PROJECT_ROOT" diff --unified=0 -- path/to/Changed.scala > "$OUT_DIR/chisel.diff"
"$PYTHON" "$SKILL_DIR/scripts/rtl_depth.py" locate \
  --firrtl "$FIRRTL_FILE" --diff "$OUT_DIR/chisel.diff" \
  --output "$OUT_DIR/chisel-locators.json"
"$PYTHON" "$SKILL_DIR/scripts/rtl_depth.py" locate \
  --firrtl "$FIRRTL_FILE" --source 'CustomL1Hint.scala' --limit 100
"$PYTHON" "$SKILL_DIR/scripts/rtl_depth.py" locate \
  --firrtl "$FIRRTL_FILE" --match 'node .*canFlow|reg .*valid'
```

`locate` streams large FIRRTL files and returns locator candidates. Diff matching uses added/new source lines against `@[path line:column]`. Pure deletions need old FIRRTL and explicit source lookup; zero hits do not prove optimization removed a change. Paths must agree; use a stable source suffix with `--source` if prefixes differ. `--line-slop` allows explicit nearby matches; `--limit` bounds results and marks truncation.

Inspect each candidate's module, declaration, bundle field, instance chain and locator. Confirm generated Release modules with names, port shapes and neighboring logic. Deduplication, aggregate lowering, temporary renaming and optimization can make mappings many-to-one, one-to-many or absent. Keep evidence: Chisel file/line → FIRRTL file/line/module/declaration → verified Release hierarchy/signal/source line, with confidence and unmapped cases. The helper does not prove semantic equivalence.

Trace the verified Release signal both ways and read witness RTL lines to confirm precedence, widths, enable/reset muxes and port joins. JSON retains operand widths and mux input roles. For a register query JSON contains incoming/outgoing operator lists while Markdown emphasizes the incoming witness.

## Comparing a change

Use coherent before/after Release snapshots and separate indexes. Verify signal mapping on each side. Compare depth, boundary types, mux/arithmetic/compare composition, widths and new unknown cuts with identical metric/generation settings. A smaller operator count does not prove timing improvement when a narrow logic chain becomes a wide adder or high-fanout select. Include partial coverage explicitly and keep original evidence files.
