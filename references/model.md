# Metric and frontend contract

## What is counted

The graph follows the supplied optimized RTL expression structure. Each mux, binary arithmetic operation, comparison, bitwise/logical operation, unary inversion/negation/reduction, shift, or dynamic bit/part selection has cost 1. A vector operation is one operator regardless of width. For `y = (a + b) == c ? d : e`, the maximum operator depth is 3 (add → compare → mux). Mux select and data dependencies are both traversed.

Aliases, module ports, static slices, concatenation, packed positional assignment patterns, replication, and size/signedness casts cost 0. No arithmetic delay weighting or gate decomposition is inferred. Width and operator composition accompany depth so a 64-bit add is visibly different from a 1-bit AND. Assignment and input-port context propagate sizing through supported expression operators. The operand list gives each dependency expression's width before implicit extension; result widths include the supported operation context. Sizes are inferred from declarations and expression rules, not verified signed elaboration. `null` is an unknown width. Signed arithmetic and unsized-literal corner cases require checking the RTL. Declared signal widths are the strongest width evidence.

Dependencies are at whole-signal granularity. A constant slice preserves the dependency on the entire input vector. This can overestimate paths through disjoint bus bits. The tool does not prove Boolean sensitization, account for false paths, or optimize tautologies/constants beyond what already happened in the supplied RTL. Constant-only operators still count if they remain in the text.

## Boundaries and paths

An edge-triggered procedural assignment creates separate Q and D nodes. Upstream traversal stops at Q; downstream traversal stops at D. A `reg` declaration alone is not proof of sequential storage. Conditional register updates become explicit muxes with old Q as hold input. Synchronous resets count as muxes. A canonical outer async reset condition matching a secondary event control is excluded from the D graph and logged. Noncanonical asynchronous control is reported and retained as textual mux logic; inspect it before making a timing interpretation. Clock/event expressions are not data dependencies.

Top inputs, output leaves, constants, memory accesses, missing modules, opaque functions, unsupported syntax, undriven nets, and multiply driven nets are visible boundaries. Memory is deliberately unknown, including unpacked register arrays: the tool does not guess SRAM latency, read-during-write behavior, address decoding, or macro timing. Write data, address and conditional enable dependencies end at an opaque memory-write boundary. Packed vectors/arrays are ordinary combinational signals with their declared dimensions retained. A top output with internal fanout remains traversable to downstream storage/output leaves.

For a wire, the longest upstream and downstream paths join at the query to form a structural path through it. For a register, incoming-D and outgoing-Q paths remain separate; summing them crosses a clock boundary. Register hold feedback is legal because Q and D are cut. A combinational cycle produces a partial result. Longest paths use dynamic programming rather than path enumeration; ties select one deterministic witness. JSON includes all reached terminal boundaries and one maximum witness per direction, not every possible path.

## Supported generated RTL

The dependency-free frontend targets firtool-style, elaborated, hierarchy-preserving Release RTL:

- Literal packed ranges (including multiple packed dimensions), ANSI and ordinary input/output declarations, wire declaration initializers, continuous assignments.
- Standard unary/binary expressions, ternary muxes, concatenations, packed positional `'{...}` patterns with declaration context, literal replication, static/dynamic selections, literal size casts such as `4'(x + 4'h1)`, `$signed` and `$unsigned`.
- Named module ports and hierarchy traversal, with module templates reused across instances and queries.
- Edge-triggered and combinational blocks with whole-signal assignments and nested `if/else`. Sequential assignments must be nonblocking. Combinational processes reading temporaries assigned in that same process need SSA and are marked unknown.
- Conditional preprocessing with `SYNTHESIS` defined plus explicit `--define NAME` flags. This excludes simulation initializers and guarded assertions. `initial`/`final` are skipped. The define set is recorded.

Parameterized/generate elaboration, loops/case statements, partial procedural assignments, arbitrary functions, interfaces, positional/concatenated output connections, active macros/includes, and multiple drivers are not fully modeled. These are diagnosed; affected drivers or module outputs become unknown. A structural coverage gap in a visited module conservatively makes a query partial even if it may not affect the chosen witness. Active-file unexpanded preprocessor constructs also make it partial. Missing fanout edges cannot justify a complete forward maximum.

The index records source hashes, size/mtime, diagnostics, unknown/memory counts, and schema version. Queries reject changed size/mtime by default. `--allow-stale` explicitly reads the cached snapshot and records stale paths. An edit preserving size and mtime requires rebuilding or manual hash comparison. Matching module names do not establish a matching snapshot.

## Scale and accuracy

Indexing parses each definition once. Queries load JSON and materialize only touched nodes and hierarchy contexts; they do not flatten the chip. Batch signals in one invocation to share materialized nodes. The per-direction `--max-nodes` limit returns a partial result when reached. The JSON index can be large and is not part of the skill package.

Check status, unknown boundaries, module/preprocessor diagnostics, input identity, hierarchy joins, and a readable critical witness before claiming a maximum for the supported cone model. Use a mature elaborating frontend when the needed syntax falls outside this subset. Source operator count is neither mapped gate depth nor STA delay.
