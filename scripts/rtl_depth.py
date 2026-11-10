#!/usr/bin/env python3
"""Portable, conservative operator-depth analysis for flattened-expression RTL.

No synthesis libraries are required. This is deliberately a source-structure
metric, not a cell delay estimator. See references/model.md for its contract.
"""
import argparse
import collections
import hashlib
import json
import re
import sys
from pathlib import Path

VERSION = 2
TOKEN = re.compile(r"\s+|//[^\n]*|/\*[\s\S]*?\*/|\\[^\s]+|(?:\d+)?'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+|'[01xXzZ]|\d+|[A-Za-z_$][\w$]*|\"(?:\\.|[^\"\\])*\"|===|!==|>>>|<<<|\*\*|==|!=|<=|>=|&&|\|\||<<|>>|~\^|\^~|~&|~\||\+:|-:|.")
IDENT = re.compile(r"^(?:[A-Za-z_$][\w$]*|\\\S+)$")
PREC = {"||": 1, "&&": 2, "|": 3, "^": 4, "~^": 4, "^~": 4, "&": 5,
        "==": 6, "!=": 6, "===": 6, "!==": 6, "<": 7, ">": 7, "<=": 7, ">=": 7,
        "<<": 8, ">>": 8, "<<<": 8, ">>>": 8, "+": 9, "-": 9,
        "*": 10, "/": 10, "%": 10, "**": 11}
UNARY = {"!", "~", "&", "~&", "|", "~|", "^", "~^", "^~", "+", "-"}


def number(s):
    s = s.replace("_", "")
    if s in ("'0", "'1"):
        return int(s[1])
    m = re.fullmatch(r"(\d*)'[sS]?([bBoOdDhH])([\da-fA-F]+)", s)
    if m:
        try:
            return int(m[3], {"b": 2, "o": 8, "d": 10, "h": 16}[m[2].lower()])
        except ValueError:
            return None
    return int(s) if re.fullmatch(r"-?\d+", s) else None


def literal_width(s):
    m = re.match(r"(\d+)'", s)
    return int(m[1]) if m else (1 if s.startswith("'") else 32)


def category(op):
    if op == "mux":
        return "mux"
    if op in ("+", "-", "*", "/", "%", "**", "u+", "u-"):
        return "arithmetic"
    if op in ("==", "!=", "===", "!==", "<", ">", "<=", ">="):
        return "comparison"
    if op in ("<<", ">>", "<<<", ">>>"):
        return "shift"
    if op == "dynamic_select":
        return "select"
    if op.startswith("u") and op[1:] in ("&", "|", "^", "~&", "~|", "~^", "^~"):
        return "reduction"
    if op in ("&", "|", "^", "~^", "^~", "u~", "u!", "&&", "||"):
        return "logic"
    return "wiring"


def preprocess(text, defines):
    """Preserve line numbers; evaluate ifdef using explicit synthesis defines."""
    defs = set(defines)
    stack, active, result, warnings = [], True, [], []
    for line_no, line in enumerate(text.splitlines(True), 1):
        m = re.match(r"\s*`(\w+)\s*(.*)", line)
        if not m:
            result.append(line if active else "\n")
            continue
        op, rest = m.groups()
        arg = rest.split()[0] if rest.split() else ""
        if op in ("ifdef", "ifndef"):
            cond = (arg in defs) == (op == "ifdef")
            stack.append([active, cond])
            active = active and cond
        elif op in ("else", "elsif") and stack:
            parent, seen = stack[-1]
            cond = not seen and (op == "else" or arg in defs)
            stack[-1][1] = seen or cond
            active = parent and cond
        elif op == "endif" and stack:
            active = stack.pop()[0]
        elif op == "define" and active:
            defs.add(arg.split("(")[0])
        elif op == "undef" and active:
            defs.discard(arg)
        elif op in ("timescale", "default_nettype", "resetall", "celldefine", "endcelldefine"):
            pass
        elif active:
            warnings.append({"line": line_no, "message": "unexpanded directive/macro: " + op})
        result.append("\n")
    if stack:
        warnings.append({"line": 1, "message": "unbalanced preprocessor conditional"})
    return "".join(result), warnings


def tokenize(text):
    out = []
    line = 1
    for m in TOKEN.finditer(text):
        s = m[0]
        if not s.isspace() and not s.startswith(("//", "/*")):
            out.append((s, line))
        line += s.count("\n")
    return out


class Stream:
    def __init__(self, tokens):
        self.t = tokens
        self.i = 0

    def peek(self, n=0):
        return self.t[self.i + n][0] if self.i + n < len(self.t) else ""

    def pop(self, expected=None):
        if self.i >= len(self.t):
            raise ValueError("unexpected end")
        t = self.t[self.i][0]
        if expected is not None and t != expected:
            raise ValueError("expected %s, got %s" % (expected, t))
        self.i += 1
        return t

    def line(self):
        return self.t[min(self.i, len(self.t) - 1)][1] if self.t else 1

    def balanced(self, left="(", right=")"):
        self.pop(left)
        depth, start = 1, self.i
        while self.peek():
            x = self.pop()
            depth += (x == left) - (x == right)
            if depth == 0:
                return self.t[start:self.i - 1]
        raise ValueError("unclosed " + left)

    def until(self, stop):
        start, nesting = self.i, []
        while self.peek():
            x = self.peek()
            if not nesting and x in stop:
                break
            self.pop()
            if x in ("(", "[", "{"):
                nesting.append(x)
            elif x in (")", "]", "}") and nesting:
                nesting.pop()
        return self.t[start:self.i]


def split_tokens(tokens, delimiter=","):
    s, out = Stream(tokens), []
    while s.peek():
        out.append(s.until({delimiter}))
        if s.peek():
            s.pop()
    return out


class Module:
    def __init__(self, name, path, line, header, body, signatures):
        self.name, self.path, self.line = name, path, line
        self.nodes, self.ports, self.instances, self.regs = {}, {}, {}, set()
        self.warnings, self.next_id, self.driven = [], 0, set()
        self.signatures = signatures
        self.header = header
        self.body = body

    def warn(self, message, line=0):
        self.warnings.append({"line": line or self.line, "message": message})

    def node(self, op, deps=(), width=None, line=0, **extra):
        key = "%" + str(self.next_id)
        self.next_id += 1
        self.nodes[key] = dict(op=op, deps=list(deps), width=width, line=line or self.line, **extra)
        return key

    def signal(self, name, width=None, direction=None, memory=False, packed_shape=None):
        if name not in self.nodes:
            self.nodes[name] = dict(op="unresolved", deps=[], width=width, line=self.line)
        if width is not None:
            self.nodes[name]["width"] = width
        if packed_shape is not None:
            self.nodes[name]["packed_shape"] = packed_shape
        if memory:
            self.nodes[name]["memory"] = True
        if direction:
            self.ports[name] = direction
            self.nodes[name]["direction"] = direction
            if direction == "input":
                self.nodes[name]["op"] = "input"
        return name

    def width(self, key):
        return self.nodes.get(key, {}).get("width")

    def expr(self, tokens, context=None, packed_shape=None):
        if not tokens:
            return self.node("unknown", reason="empty expression")
        try:
            parser = Expr(self, tokens)
            key = parser.parse()
            if parser.s.peek():
                raise ValueError("trailing expression token " + parser.s.peek())
            # Verilog assignment context propagates through context-determined
            # arithmetic/bitwise/shift/mux operands; comparisons keep self width.
            if context is not None:
                self.context(key, context, packed_shape)
            elif self.nodes[key]["op"] == "pattern":
                raise ValueError("assignment pattern requires declaration context")
            return key
        except (ValueError, RecursionError) as e:
            self.warn("unsupported expression: " + str(e), tokens[0][1])
            refs = list(dict.fromkeys(t for t, _ in tokens if IDENT.match(t) and not t.startswith("$")))
            for x in refs:
                self.signal(x)
            return self.node("unknown", refs, context, tokens[0][1], reason=str(e), text=" ".join(x for x, _ in tokens))

    def context(self, key, width, packed_shape=None):
        n = self.nodes[key]
        if n["op"] == "pattern":
            shape = packed_shape or [width]
            if shape[0] != len(n["deps"]) or None in shape:
                n.update(op="unknown", reason="assignment pattern shape/context mismatch")
                self.warn("unsupported assignment pattern context", n["line"])
                return
            element_width = width // shape[0]
            n.update(width=width, packed_shape=shape)
            for dep in n["deps"]:
                self.context(dep, element_width, shape[1:])
        elif n["op"] in ("+", "-", "*", "/", "%", "**", "&", "|", "^", "~^", "^~", "u~", "u+", "u-", "<<", ">>", "<<<", ">>>", "mux"):
            n["width"] = max(width, n["width"] or 0)
            deps = n["deps"][1:] if n["op"] == "mux" else n["deps"]
            if n["op"] in ("<<", ">>", "<<<", ">>>", "**"):
                deps = deps[:1]
            for dep in deps:
                self.context(dep, n["width"])

    def drive(self, target, expr, line, sequential=False):
        self.signal(target)
        if self.nodes[target].get("memory"):
            return
        key = target + "$D" if sequential else target
        if sequential:
            self.regs.add(target)
            self.nodes[target]["op"] = "reg_q"
            self.nodes[target]["deps"] = []
            self.nodes[target]["line"] = line
        if key in self.driven:
            self.warn("multiple drivers: " + target, line)
            self.nodes[key] = dict(op="unknown", deps=[], width=self.width(target), line=line, reason="multiple drivers")
        else:
            self.nodes[key] = dict(op="reg_d" if sequential else "alias", deps=[expr], width=self.width(target), line=line,
                                   packed_shape=self.nodes[target].get("packed_shape"))
            self.driven.add(key)

    def declaration(self, tokens, direction=None):
        s = Stream(tokens)
        if s.peek() in ("input", "output", "inout"):
            direction = s.pop()
        while s.peek() in ("wire", "reg", "logic", "signed", "unsigned", "tri", "integer", "bit"):
            s.pop()
        width, packed_shape = 1, []
        while s.peek() == "[":
            bounds = split_tokens(s.balanced("[", "]"), ":")
            try:
                vals = [number("".join(t for t, _ in b)) for b in bounds]
                size = abs(vals[0] - vals[1]) + 1
                packed_shape.append(size)
                width = width * size if width is not None else None
            except (IndexError, TypeError):
                width = None
                packed_shape.append(None)
                self.warn("nonliteral declaration width", tokens[0][1])
        for part in split_tokens(s.t[s.i:]):
            p = Stream(part)
            if not p.peek():
                continue
            name, line = p.pop(), part[0][1]
            if not IDENT.match(name):
                self.warn("unsupported declaration", line)
                continue
            memory = p.peek() == "["
            self.signal(name, width, direction, memory, packed_shape or [1])
            self.nodes[name]["line"] = line
            if memory:
                self.nodes[name]["op"] = "memory"
                self.nodes[name]["reason"] = "unmodeled memory array: " + name
                p.balanced("[", "]")
            if p.peek() == "=":
                p.pop()
                self.drive(name, self.expr(p.t[p.i:], width, packed_shape), line)
            elif p.peek():
                self.warn("unsupported declaration tail for " + name, line)
        return direction, width, packed_shape or [1]

    def ports_parse(self):
        direction, width, packed_shape = None, 1, [1]
        for part in split_tokens(self.header):
            if not part:
                continue
            if part[0][0] in ("input", "output", "inout"):
                direction, width, packed_shape = self.declaration(part)
            else:
                if direction:
                    # ANSI declarations sharing direction/packed range.
                    name = part[0][0]
                    self.signal(name, width, direction, packed_shape=packed_shape)
                else:
                    self.signal(part[0][0])

    def statement(self, s):
        line = s.line()
        if s.peek() == "begin":
            s.pop()
            if s.peek() == ":":
                s.pop(); s.pop()
            out = []
            while s.peek() and s.peek() != "end":
                out.append(self.statement(s))
            s.pop("end")
            return ("block", out, line)
        if s.peek() == "if":
            s.pop()
            cond = s.balanced()
            yes = self.statement(s)
            no = ("block", [], line)
            if s.peek() == "else":
                s.pop()
                no = self.statement(s)
            return ("if", cond, yes, no, line)
        if s.peek() in ("case", "casez", "casex"):
            # Retain affected targets; unsupported control poisons their drivers.
            start, depth = s.i, 0
            while s.peek():
                x = s.pop()
                depth += (x in ("case", "casez", "casex")) - (x == "endcase")
                if depth == 0:
                    break
            return ("unknown", s.t[start:s.i], line)
        if s.peek() in ("for", "while", "repeat", "forever"):
            start = s.i
            s.pop()
            if s.peek() == "(":
                s.balanced()
            self.statement(s)
            return ("unknown", s.t[start:s.i], line)
        part = s.until({";", "end", "else"})
        if s.peek() == ";":
            s.pop()
        if not part:
            raise ValueError("unsupported statement at " + s.peek())
        for i, (x, _) in enumerate(part):
            if x in ("<=", "="):
                lhs, rhs = part[:i], part[i + 1:]
                if lhs and IDENT.match(lhs[0][0]):
                    return ("assign", lhs[0][0], rhs, line, len(lhs) != 1, x, lhs)
        if part[0][0] in ("$display", "$write", "$fwrite", "$fatal", "$error", "$finish", "assert", "assume", "cover"):
            return ("block", [], line)
        return ("unknown", part, line)

    def targets(self, ast):
        tag = ast[0]
        if tag == "assign":
            return {ast[1]}
        if tag == "block":
            return set().union(*(self.targets(a) for a in ast[1])) if ast[1] else set()
        if tag == "if":
            return self.targets(ast[2]) | self.targets(ast[3])
        toks = ast[1]
        targets = set()
        for i, (x, _) in enumerate(toks):
            if IDENT.match(x):
                j = i + 1
                if j < len(toks) and toks[j][0] == "[":
                    while j < len(toks) and toks[j][0] != "]":
                        j += 1
                    j += 1
                if j < len(toks) and toks[j][0] in ("=", "<="):
                    targets.add(x)
        return targets

    def flow(self, ast, env, sequential, controls=()):
        tag = ast[0]
        if tag == "block":
            for a in ast[1]:
                env = self.flow(a, env, sequential, controls)
            return env
        if tag == "assign":
            _, target, expr, line, partial, assign_op, lhs = ast
            if self.nodes.get(target, {}).get("memory"):
                # Preserve data/address/enable fanout to an opaque write port.
                deps = [self.expr(expr, self.width(target))] + list(controls)
                indices = Stream(lhs[1:])
                while indices.peek() == "[":
                    index = Stream(indices.balanced("[", "]"))
                    while index.peek():
                        part = index.until({":", "+:", "-:"})
                        if part:
                            deps.append(self.expr(part))
                        if index.peek():
                            index.pop()
                self.node("memory", deps, self.width(target), line, reason="memory write: " + target)
                return env
            if partial or (sequential and assign_op == "="):
                env[target] = self.node("unknown", line=line, width=self.width(target), reason="partial write or blocking assignment in edge process")
                self.warn("partial write or blocking sequential assignment: " + target, line)
            else:
                env[target] = self.expr(expr, self.width(target), self.nodes[target].get("packed_shape"))
            return env
        if tag == "if":
            _, cond, yes, no, line = ast
            cond = self.expr(cond)
            a = self.flow(yes, dict(env), sequential, controls + (cond,))
            b = self.flow(no, dict(env), sequential, controls + (cond,))
            for key in set(a) | set(b):
                if key not in env:
                    env[key] = key if sequential else self.node("unknown", width=self.width(key), line=line, reason="incomplete combinational assignment/latch")
                av, bv = a.get(key, env[key]), b.get(key, env[key])
                env[key] = av if av == bv else self.node("mux", [cond, av, bv], self.width(key), line, roles=["select", "true", "false"])
            return env
        self.warn("unsupported procedural control", ast[-1])
        for target in self.targets(ast):
            if not self.nodes.get(target, {}).get("memory"):
                env[target] = self.node("unknown", width=self.width(target), line=ast[-1], reason="unsupported procedural control")
        return env

    def instance(self, module, name, ports, line, parameterized=False):
        self.instances[name] = module
        sig = self.signatures.get(module)
        if parameterized:
            self.warn("parameterized instance unsupported: " + name, line)
            sig = None
        for part in split_tokens(ports):
            p = Stream(part)
            if not part:
                continue
            if p.peek() != ".":
                self.warn("positional instance ports unsupported: " + name, line)
                for tok, _ in part:
                    if IDENT.match(tok):
                        self.signal(tok)
                        self.drive(tok, self.node("unknown", reason="positional instance connection", line=line), line)
                continue
            p.pop(".")
            port = p.pop()
            expr = p.balanced()
            virtual = "@" + name + ":" + port
            port_info = sig.get(port, {}) if sig else {}
            direction = port_info.get("direction")
            if direction == "input":
                value = self.expr(expr, port_info.get("width"), port_info.get("packed_shape")) if expr else self.node("unknown", reason="unconnected input", line=line)
                self.nodes[virtual] = dict(op="alias", deps=[value], width=port_info.get("width"), line=line, child_input=[name, port])
            elif direction == "output":
                self.nodes[virtual] = dict(op="alias", deps=["!" + name + ":" + port], width=None, line=line)
                if expr:
                    if len(expr) == 1 and IDENT.match(expr[0][0]):
                        self.drive(expr[0][0], virtual, line)
                    else:
                        self.warn("partial/concatenated output connection: " + name + "." + port, line)
                        for tok, _ in expr:
                            if IDENT.match(tok):
                                self.signal(tok)
                                self.drive(tok, self.node("unknown", [virtual], line=line, reason="partial output connection"), line)
            else:
                self.warn("unknown/inout port: " + name + "." + port, line)
                for tok, _ in expr:
                    if IDENT.match(tok):
                        self.signal(tok)
                        self.drive(tok, self.node("unknown", reason="opaque instance " + module, line=line), line)

    def parse(self):
        self.ports_parse()
        s = Stream(self.body)
        while s.peek():
            start, line = s.i, s.line()
            token = s.pop()
            try:
                if token in ("wire", "reg", "logic", "input", "output", "inout", "tri"):
                    part = s.until({";"}); s.pop(";")
                    self.declaration([(token, line)] + part)
                elif token == "assign":
                    part = s.until({";"}); s.pop(";")
                    eq = next(i for i, (x, _) in enumerate(part) if x == "=")
                    lhs, rhs = part[:eq], part[eq + 1:]
                    if len(lhs) == 1 and IDENT.match(lhs[0][0]):
                        target = lhs[0][0]
                        self.drive(target, self.expr(rhs, self.width(target), self.nodes.get(target, {}).get("packed_shape")), line)
                    else:
                        self.warn("partial/concatenated continuous assignment", line)
                        for target, _ in lhs:
                            if IDENT.match(target):
                                self.signal(target)
                                self.drive(target, self.node("unknown", line=line, reason="partial continuous assignment"), line)
                elif token in ("always", "always_ff", "always_comb", "always_latch"):
                    sensitivity = []
                    if s.peek() == "@":
                        s.pop()
                        sensitivity = s.balanced() if s.peek() == "(" else [(s.pop(), line)]
                    ast = self.statement(s)
                    sequential = any(x in ("posedge", "negedge") for x, _ in sensitivity)
                    # Asynchronous reset/set is a flop control pin, not a D mux.
                    edge_terms = [(x, sensitivity[i + 1][0]) for i, (x, _) in enumerate(sensitivity[:-1]) if x in ("posedge", "negedge")]
                    edges = [name for _, name in edge_terms]
                    inner = ast
                    if inner[0] == "block" and len(inner[1]) == 1:
                        inner = inner[1][0]
                    if len(edges) > 1 and inner[0] == "if":
                        cond_tokens = [x for x, _ in inner[1]]
                        canonical = any(cond_tokens == ([name] if edge == "posedge" else ["!", name])
                                        or (edge == "negedge" and cond_tokens == ["~", name])
                                        for edge, name in edge_terms[1:])
                        if canonical:
                            # Only generated canonical `if (reset) reset-values; else data`.
                            ast = inner[3]
                            self.warn("asynchronous control excluded from D graph: " + ",".join(edges[1:]), line)
                        else:
                            self.warn("noncanonical asynchronous process; control counted as mux", line)
                    targets = self.targets(ast)
                    for target in targets:
                        self.signal(target)
                    env = self.flow(ast, {}, sequential)
                    # Generated FIRRTL combinational processes do not read
                    # blocking temporaries assigned in the same process. Those
                    # require procedural SSA, which this frontend does not do.
                    if not sequential:
                        def reads(a):
                            if a[0] == "assign":
                                return {x for x, _ in a[2] if IDENT.match(x)}
                            if a[0] == "if":
                                return {x for x, _ in a[1] if IDENT.match(x)} | reads(a[2]) | reads(a[3])
                            if a[0] == "block":
                                return set().union(*(reads(c) for c in a[1])) if a[1] else set()
                            return set()
                        if reads(ast) & targets:
                            self.warn("procedural temporary dependencies require SSA", line)
                            env = {t: self.node("unknown", width=self.width(t), line=line, reason="procedural temporary dependencies") for t in targets}
                    for target, value in env.items():
                        self.drive(target, value, line, sequential)
                elif token in ("initial", "final"):
                    self.statement(s)
                elif token in ("parameter", "localparam", "genvar", "integer"):
                    s.until({";"}); s.pop(";")
                    self.warn("parameter/localparam/generate declarations are not elaborated", line)
                elif token == ";":
                    pass
                elif IDENT.match(token) and s.peek():
                    parameterized = s.peek() == "#"
                    if parameterized:
                        s.pop(); s.balanced()
                    name = s.pop()
                    if s.peek() != "(":
                        raise ValueError("unsupported module-level construct " + token)
                    ports = s.balanced(); s.pop(";")
                    self.instance(token, name, ports, line, parameterized)
                else:
                    raise ValueError("unsupported module-level token " + token)
            except (ValueError, StopIteration, IndexError, RecursionError) as e:
                self.warn("module parse recovery: " + str(e), line)
                # Stop instead of silently interpreting the rest in a wrong scope.
                # Poison every declared output and sequential sink in this module.
                for name, n in list(self.nodes.items()):
                    if not name.startswith(("%", "@")) and self.ports.get(name) != "input" and n["op"] != "reg_q":
                        self.nodes[name] = dict(op="unknown", deps=[], width=n.get("width"), line=line, reason="module parse recovery")
                self.warn("module body incomplete after line " + str(line), line)
                break
            if s.i <= start:
                raise RuntimeError("parser made no progress")
        # A declaration alone never proves a register; only edge-process writes do.
        for name, n in self.nodes.items():
            if n["op"] == "unresolved":
                n["op"] = "unknown"
                n["reason"] = "undriven, unsupported, or implicit signal"
            if n["op"] == "inout":
                n["op"] = "unknown"
                n["reason"] = "inout"
        reverse = collections.defaultdict(list)
        for name, n in self.nodes.items():
            for dep in set(n["deps"]):
                if not dep.startswith("!"):
                    reverse[dep].append(name)
        return dict(path=self.path, line=self.line, nodes=self.nodes, users=dict(reverse), ports=self.ports,
                    instances=self.instances, registers=sorted(self.regs), warnings=self.warnings)


class Expr:
    def __init__(self, module, tokens):
        self.m, self.s = module, Stream(tokens)

    def parse(self, minimum=0):
        m, s = self.m, self.s
        line, tok = s.line(), s.pop()
        if tok in UNARY:
            dep = self.parse(12)
            w = 1 if tok in ("!", "&", "|", "^", "~&", "~|", "~^", "^~") else m.width(dep)
            key = m.node("u" + tok, [dep], w, line)
        elif tok == "(":
            key = self.parse()
            s.pop(")")
        elif tok == "'" and s.peek() == "{":
            s.pop("{")
            parts = [self.parse()]
            while s.peek() == ",":
                s.pop(); parts.append(self.parse())
            s.pop("}")
            key = m.node("pattern", parts, line=line)
        elif tok == "{":
            first = self.parse()
            if s.peek() == "{":
                s.pop()
                parts = [self.parse()]
                while s.peek() == ",":
                    s.pop(); parts.append(self.parse())
                s.pop("}"); s.pop("}")
                repeat = number(m.nodes[first].get("value", ""))
                width = sum(m.width(p) or 0 for p in parts) * repeat if repeat is not None and all(m.width(p) is not None for p in parts) else None
                key = m.node("replicate", parts, width, line, repeat=repeat)
            else:
                parts = [first]
                while s.peek() == ",":
                    s.pop(); parts.append(self.parse())
                s.pop("}")
                width = sum(m.width(p) for p in parts) if all(m.width(p) is not None for p in parts) else None
                key = m.node("concat", parts, width, line)
        elif tok.isdigit() and s.peek() == "'":
            s.pop("'"); s.pop("(")
            dep = self.parse(); s.pop(")")
            width = int(tok)
            if width <= 0:
                raise ValueError("nonpositive size cast")
            m.context(dep, width)
            key = m.node("cast", [dep], width, line, size_cast=width)
        elif re.match(r"^(?:\d|'[01xXzZ])", tok):
            key = m.node("constant", width=literal_width(tok), line=line, value=tok)
        elif IDENT.match(tok):
            if s.peek() == "(":
                args = split_tokens(s.balanced())
                deps = [m.expr(a) for a in args if a]
                if tok in ("$signed", "$unsigned") and len(deps) == 1:
                    key = m.node("cast", deps, m.width(deps[0]), line, signed=tok == "$signed")
                else:
                    key = m.node("unknown", deps, line=line, reason="unmodeled function " + tok)
                    m.warn("unmodeled function " + tok, line)
            else:
                key = m.signal(tok)
        else:
            raise ValueError("unexpected expression token " + tok)
        while s.peek() == "[":
            s.pop()
            shape = m.nodes[key].get("packed_shape") or [m.width(key)]
            element_width = (m.width(key) // shape[0]) if m.width(key) is not None and shape[0] else None
            idx = self.parse()
            idxnum = number(m.nodes[idx].get("value", ""))
            if s.peek() in (":", "+:", "-:"):
                select = s.pop()
                end = self.parse()
                endnum = number(m.nodes[end].get("value", ""))
                s.pop("]")
                count = abs(idxnum - endnum) + 1 if select == ":" and idxnum is not None and endnum is not None else (endnum if select != ":" else None)
                width = count * element_width if count is not None and element_width is not None else None
                op = "slice" if idxnum is not None and endnum is not None else "dynamic_select"
                key = m.node(op, [key] if op == "slice" else [key, idx], width, line, packed_shape=[count] + shape[1:])
            else:
                s.pop("]")
                key = m.node("slice" if idxnum is not None else "dynamic_select", [key] if idxnum is not None else [key, idx], element_width, line, packed_shape=shape[1:] or [1])
        while s.peek():
            op = s.peek()
            if op == "?" and minimum <= 0:
                s.pop()
                yes = self.parse(); s.pop(":"); no = self.parse()
                widths = [m.width(yes), m.width(no)]
                width = max(widths) if None not in widths else None
                key = m.node("mux", [key, yes, no], width, line, roles=["select", "true", "false"])
                if width is not None:
                    m.context(key, width)
            elif op in PREC and PREC[op] >= minimum:
                s.pop()
                rhs = self.parse(PREC[op] + (0 if op == "**" else 1))
                widths = [m.width(key), m.width(rhs)]
                width = max(widths) if None not in widths else None
                operand_width = width
                if category(op) == "comparison" or op in ("&&", "||"):
                    width = 1
                elif category(op) == "shift" or op == "**":
                    width = m.width(key)
                key = m.node(op, [key, rhs], width, line)
                if category(op) == "comparison" and operand_width is not None:
                    for dep in m.nodes[key]["deps"]:
                        m.context(dep, operand_width)
                elif width is not None:
                    m.context(key, width)
            else:
                break
        return key


def sources_from_args(args):
    found = []
    for source in args.rtl or []:
        path = Path(source).expanduser().resolve()
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in (".v", ".sv")))
        else:
            found.append(path)
    seen_lists = set()
    def filelist(path):
        path = Path(path).expanduser().resolve()
        if path in seen_lists:
            return
        seen_lists.add(path)
        base = Path(args.file_list_base).expanduser().resolve() if args.file_list_base else path.parent
        # Generated file lists use one whitespace-free path per line. Reject
        # tool options rather than silently indexing a different design.
        for line in path.read_text().splitlines():
            line = line.split("//")[0].strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("-f ", "-F ")):
                nested = Path(line[3:].strip())
                filelist(nested if nested.is_absolute() else base / nested)
            elif line.startswith("+incdir+"):
                continue
            elif line.startswith(("+", "-")):
                raise ValueError("unsupported file-list option; use --define explicitly: " + line)
            else:
                p = Path(line)
                found.append((p if p.is_absolute() else base / p).resolve())
    for path in args.file_list or []:
        filelist(path)
    result = sorted(set(found))
    if not result:
        raise ValueError("provide --rtl or --file-list")
    return result


def make_index(args):
    paths = sources_from_args(args)
    raw, files, global_warnings = [], [], []
    defines = ["SYNTHESIS"] + (args.define or [])
    for path in paths:
        data = path.read_bytes()
        stat = path.stat()
        files.append(dict(path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=hashlib.sha256(data).hexdigest()))
        source, warnings = preprocess(data.decode("utf-8", errors="replace"), defines)
        global_warnings.extend(dict(path=str(path), **w) for w in warnings)
        toks = tokenize(source)
        s = Stream(toks)
        while s.peek():
            if s.pop() != "module":
                continue
            line, name = s.line(), s.pop()
            params = False
            if s.peek() == "#":
                s.pop(); s.balanced(); params = True
            header = s.balanced() if s.peek() == "(" else []
            s.pop(";")
            start = s.i
            while s.peek() and s.peek() != "endmodule":
                s.pop()
            body = s.t[start:s.i]
            s.pop("endmodule")
            raw.append((name, str(path), line, header, body, params))
    signatures = {}
    for name, path, line, header, body, params in raw:
        if name in signatures:
            raise ValueError("duplicate module " + name + "; select one consistent RTL snapshot")
        m = Module(name, path, line, header, [], {})
        m.ports_parse()
        # Old-style declarations are supported for literal-width ports.
        for match in re.finditer(r"\b(input|output|inout)\b([^;]*);", " ".join(x for x, _ in body)):
            m.declaration(tokenize(match[0][:-1]))
        signatures[name] = {p: dict(direction=d, width=m.width(p), packed_shape=m.nodes[p].get("packed_shape")) for p, d in m.ports.items()}
    modules = {}
    for name, path, line, header, body, params in raw:
        m = Module(name, path, line, header, body, signatures)
        modules[name] = m.parse()
        if params:
            modules[name]["warnings"].append(dict(line=line, message="parameterized module: parameter elaboration unsupported"))
            for port, direction in modules[name]["ports"].items():
                if direction == "output":
                    modules[name]["nodes"][port] = dict(op="unknown", deps=[], width=None, line=line, reason="parameterized module")
    index = dict(version=VERSION, metric="unit operator depth; signal-granularity dependencies", defines=defines,
                 sources=files, warnings=global_warnings, modules=modules)
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(index, separators=(",", ":")))
    stats = collections.Counter(n["op"] for m in modules.values() for n in m["nodes"].values())
    print(json.dumps(dict(index=str(output.resolve()), files=len(files), modules=len(modules), nodes=sum(stats.values()),
                          registers=sum(len(m["registers"]) for m in modules.values()),
                          unknown_nodes=stats["unknown"], memory_nodes=stats["memory"],
                          warnings=len(global_warnings) + sum(len(m["warnings"]) for m in modules.values())), indent=2))


class Design:
    def __init__(self, index, top):
        self.index, self.modules, self.top = index, index["modules"], top
        if top not in self.modules:
            raise ValueError("top module not in index: " + top)
        self.contexts = {top: top}
        self.cache = {}

    def context(self, path):
        if path not in self.contexts:
            parent, instance = path.rsplit("/", 1)
            mod = self.module(parent)
            if instance not in mod["instances"]:
                raise ValueError("unknown instance " + path)
            self.contexts[path] = mod["instances"][instance]
        return self.contexts[path]

    def module(self, path):
        name = self.context(path)
        if name not in self.modules:
            return dict(nodes={}, users={}, ports={}, instances={}, path="", warnings=[])
        return self.modules[name]

    def node(self, key):
        if key in self.cache:
            return self.cache[key]
        path, local = key.rsplit("/", 1)
        module = self.module(path)
        if local not in module["nodes"]:
            result = dict(op="unknown", deps=[], width=None, path=module["path"], line=0, reason="missing module/port/signal " + key)
        else:
            result = dict(module["nodes"][local], path=module["path"])
            result["deps"] = [path + "/" + d if not d.startswith("!") else path + "/" + d[1:].replace(":", "/", 1) for d in result["deps"]]
            if result["op"] == "input":
                if path == self.top:
                    result["op"] = "top_input"
                else:
                    parent, instance = path.rsplit("/", 1)
                    virtual = parent + "/@" + instance + ":" + local
                    result["op"] = "alias"
                    result["deps"] = [virtual]
            if not result.get("width") and local.startswith("@"):
                inst, port = local[1:].split(":", 1)
                child = self.module(path + "/" + inst)
                result["width"] = child["nodes"].get(port, {}).get("width")
        self.cache[key] = result
        return result

    def users(self, key):
        path, local = key.rsplit("/", 1)
        module, node = self.module(path), self.node(key)
        result = [path + "/" + n for n in module["users"].get(local, [])]
        if node.get("child_input"):
            inst, port = node["child_input"]
            result.append(path + "/" + inst + "/" + port)
        if module["ports"].get(local) == "output" and path != self.top:
            parent, inst = path.rsplit("/", 1)
            virtual = parent + "/@" + inst + ":" + local
            if virtual.rsplit("/", 1)[1] in self.module(parent)["nodes"]:
                result.append(virtual)
        return list(dict.fromkeys(result))

    def resolve(self, text):
        text = text.replace(".", "/")
        key = text if text.startswith(self.top + "/") else self.top + "/" + text
        path, local = key.rsplit("/", 1)
        if local not in self.module(path)["nodes"]:
            raise ValueError("signal not found: " + key + "; use list --match to inspect names")
        return key


BOUNDARY = {"reg_q", "top_input", "constant", "unknown", "memory"}
ZERO = {"alias", "reg_d", "slice", "concat", "pattern", "replicate", "cast", "reg_q", "top_input", "constant", "unknown", "memory"}


def cost(node):
    return 0 if node["op"] in ZERO else 1


def walk(design, start, direction, limit):
    """Memoized longest path in each reachable DAG, preserving boundary evidence."""
    memo, visiting, order = {}, set(), []
    # Iterative DFS avoids Python's recursion limit on long source-level cones.
    stack = [(start, False)]
    cycles, limited = set(), False
    adjacency = {}
    while stack:
        key, done = stack.pop()
        if key in memo:
            continue
        node = design.node(key)
        if done:
            visiting.discard(key)
            neighbors = adjacency.get(key, [])
            options = [(memo[n][0] + (cost(design.node(n)) if direction == "downstream" else 0), n) for n in neighbors if n in memo]
            best = max(options, default=(0, None))
            memo[key] = (best[0] + (cost(node) if direction == "upstream" else 0), best[1])
            order.append(key)
            continue
        if key in visiting:
            cycles.add(key)
            continue
        if len(visiting) + len(memo) >= limit:
            limited = True
            continue
        visiting.add(key)
        stack.append((key, True))
        if direction == "upstream":
            neighbors = [] if node["op"] in BOUNDARY else node["deps"]
        else:
            # Never cross the next state boundary, even when a D node has users.
            neighbors = [] if node["op"] in ("reg_d", "unknown", "memory") else design.users(key)
        adjacency[key] = neighbors
        for neighbor in reversed(neighbors):
            if neighbor in visiting:
                cycles.add(neighbor)
            elif neighbor not in memo:
                stack.append((neighbor, False))
    witness, key, seen = [], start, set()
    while key in memo and key not in seen:
        witness.append(key)
        seen.add(key)
        key = memo[key][1]
    if direction == "upstream":
        witness.reverse()
    terminals = []
    for key in memo:
        node = design.node(key)
        if not adjacency.get(key):
            context, local = key.rsplit("/", 1)
            kind = "top_output" if direction == "downstream" and node["op"] not in ("unknown", "memory") and context == design.top and design.module(context)["ports"].get(local) == "output" else node["op"]
            terminals.append(dict(signal=key, kind=kind, reason=node.get("reason")))
    incomplete = [t for t in terminals if t["kind"] in ("unknown", "memory")]
    if cycles or limited:
        incomplete.append(dict(signal=start, kind="limit" if limited else "cycle", reason="node budget reached" if limited else "combinational cycle"))
    return dict(depth=memo.get(start, (0,))[0], witness=witness, reachable_nodes=len(memo),
                terminals=sorted(terminals, key=lambda x: (x["kind"], x["signal"])),
                incomplete=incomplete, cycles=sorted(cycles), node_limit_reached=limited)


def path_summary(design, path):
    operators, composition = [], collections.Counter()
    for key in path:
        n = design.node(key)
        if cost(n):
            cat = category(n["op"])
            composition[cat] += 1
            operators.append(dict(node=key, operator=n["op"], category=cat, result_width=n.get("width"),
                                  operand_widths=[design.node(d).get("width") for d in n["deps"]],
                                  path=n.get("path"), line=n.get("line"),
                                  roles=n.get("roles")))
    return dict(depth=len(operators), composition=dict(composition), operators=operators,
                source=path[0] if path else None, destination=path[-1] if path else None,
                signals=[k for k in path if not k.rsplit("/", 1)[1].startswith(("%", "@"))])


def validate_index(index, allow_stale=False):
    if index.get("version") != VERSION:
        raise ValueError("unsupported index version; rebuild index")
    stale = []
    for source in index["sources"]:
        p = Path(source["path"])
        if not p.exists() or p.stat().st_size != source["size"] or p.stat().st_mtime_ns != source["mtime_ns"]:
            stale.append(str(p))
    if stale and not allow_stale:
        raise ValueError("stale RTL index; rebuild (or explicitly --allow-stale): " + ", ".join(stale[:8]))
    return stale


def trace(args):
    index = json.loads(Path(args.index).read_text())
    stale = validate_index(index, args.allow_stale)
    design = Design(index, args.top)
    reports = []
    for query in args.signal:
        key = design.resolve(query)
        is_reg = design.node(key)["op"] == "reg_q"
        upstream_start = key + "$D" if is_reg else key
        up = walk(design, upstream_start, "upstream", args.max_nodes)
        down = walk(design, key, "downstream", args.max_nodes)
        up_keys, down_keys = up.pop("witness"), down.pop("witness")
        up["path"] = path_summary(design, up_keys)
        down["path"] = path_summary(design, down_keys)
        combined = None
        if not is_reg:
            # Recover the full key sequence for reproducible combined witness.
            combined = path_summary(design, up_keys + down_keys[1:])
        # A downstream route can have an unknown side operand. Inspect the
        # complete fanin of the reported endpoint before calling it complete.
        endpoint = down["path"]["destination"]
        endpoint_check = walk(design, endpoint, "upstream", args.max_nodes) if endpoint else dict(incomplete=[])
        incomplete = up["incomplete"] + down["incomplete"] + endpoint_check["incomplete"]
        reports.append(dict(query=query, signal=key, register_query=is_reg, width=design.node(key).get("width"),
                            status="partial" if incomplete else "complete_for_supported_signal_model",
                            upstream=up, downstream=down, through_signal=combined,
                            downstream_endpoint_fanin_unknowns=endpoint_check["incomplete"]))
    active_modules = sorted(set(design.contexts.values()))
    warnings = [dict(module=m, path=index["modules"][m]["path"], **w) for m in active_modules if m in index["modules"] for w in index["modules"][m]["warnings"]]
    structural_gaps = [w for w in warnings if any(term in w["message"] for term in
                       ("module body incomplete", "unknown/inout port", "positional instance", "parameterized", "not elaborated",
                        "partial", "unsupported procedural", "procedural temporary", "multiple drivers", "nonliteral declaration", "unsupported declaration"))]
    active_files = {index["modules"][m]["path"] for m in active_modules if m in index["modules"]}
    structural_gaps += [w for w in index["warnings"] if w["path"] in active_files]
    if structural_gaps:
        for r in reports:
            r["status"] = "partial"
            r["coverage_gaps"] = structural_gaps
    report = dict(metric=index["metric"], top=args.top, index=str(Path(args.index).resolve()), stale_sources=stale,
                  assumptions=["register Q/D cut; clock/reset pins are not data traversal roots", "all operator costs are 1; wiring/casts/static slices/concatenation cost 0",
                               "signal-granularity dependency upper model; static slices retain whole-bus dependency", "widths are inferred unsigned sizes, not signed timing or gate counts"],
                  reports=reports, coverage=dict(indexed_modules=len(index["modules"]), visited_module_types=len(active_modules),
                                                materialized_nodes=len(design.cache), warnings=warnings, preprocessing_warnings=index["warnings"]))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
    lines = ["# RTL operator depth", "", "Metric: unit operator depth on the supplied RTL text. This is not STA or mapped gate depth.", ""]
    for r in reports:
        lines += ["## `" + r["signal"] + "`", "", "Status: **" + r["status"] + "**; signal width: " + str(r["width"]) + ".", ""]
        if r["register_query"]:
            lines += ["Register query: incoming D and outgoing Q are separate paths across a state boundary.", ""]
        for name in ("upstream", "downstream"):
            part, p = r[name], r[name]["path"]
            lines += [name.capitalize() + " depth: **" + str(part["depth"]) + "**. `" + str(p["source"]) + "` → `" + str(p["destination"]) + "`.", "",
                      "Composition: " + json.dumps(p["composition"], ensure_ascii=False) + ". Reachable nodes: " + str(part["reachable_nodes"]) + ".", ""]
        p = r["through_signal"]
        if p:
            lines += ["Longest structural path through signal: **" + str(p["depth"]) + "** operators. `" + str(p["source"]) + "` → `" + str(p["destination"]) + "`.", ""]
        else:
            p = r["upstream"]["path"]
        lines += ["| Stage | Operator | Result bits | Operand bits | RTL |", "| --- | --- | --- | --- | --- |"]
        for i, op in enumerate(p["operators"], 1):
            lines.append("| %s | `%s` | %s | %s | `%s:%s` |" % (i, op["operator"].replace("|", "\\|"), op["result_width"], op["operand_widths"], op["path"].replace("|", "\\|"), op["line"]))
        lines += [""]
        unknowns = r["upstream"]["incomplete"] + r["downstream"]["incomplete"] + r["downstream_endpoint_fanin_unknowns"]
        if unknowns:
            lines += ["Unknown or cut boundaries (reported depth is an observed partial value):", ""]
            lines += ["- `" + u["signal"] + "`: " + u["kind"] + "; " + str(u.get("reason", "")) for u in unknowns]
            lines += [""]
        lines += ["Boundary counts: upstream " + json.dumps(dict(collections.Counter(t["kind"] for t in r["upstream"]["terminals"]))) + "; downstream " + json.dumps(dict(collections.Counter(t["kind"] for t in r["downstream"]["terminals"]))) + ".", ""]
    if warnings or index["warnings"]:
        lines += ["## Coverage notes", "", "The JSON includes all active module diagnostics; a diagnostic alone does not imply every query path is affected.", ""]
        lines += ["- `" + w.get("module", "") + "` " + w["message"] + " (`" + w.get("path", "") + ":" + str(w["line"]) + "`)" for w in warnings[:80]]
    text = "\n".join(lines) + "\n"
    if args.markdown:
        Path(args.markdown).write_text(text)
    else:
        print(text)
    return 2 if args.require_complete and any(r["status"] == "partial" for r in reports) else 0


def list_signals(args):
    index = json.loads(Path(args.index).read_text())
    validate_index(index, args.allow_stale)
    d = Design(index, args.top)
    context = args.instance.replace(".", "/") if args.instance else args.top
    if not context.startswith(args.top):
        context = args.top + "/" + context
    mod = d.module(context)
    out = dict(instance=context, module=d.context(context), children=mod["instances"],
               signals=[dict(name=n, width=v.get("width"), kind=v["op"]) for n, v in mod["nodes"].items()
                        if not n.startswith(("%", "@")) and (not args.match or re.search(args.match, n))])
    print(json.dumps(out, indent=2))


def locate(args):
    """Stream source locators from large CHIRRTL; no claim of exact lowering."""
    pattern = re.compile(args.source or args.match or ".")
    candidates = []
    if args.diff:
        current, lineno = None, 0
        for line in Path(args.diff).read_text().splitlines():
            if line.startswith("+++ "):
                current = line[4:].removeprefix("b/")
            elif line.startswith("@@"):
                m = re.search(r"\+(\d+)", line)
                if m:
                    lineno = int(m[1])
            elif line.startswith("+"):
                candidates.append((current, lineno)); lineno += 1
            elif not line.startswith("-"):
                lineno += 1
    hits, module = [], None
    for path in args.firrtl:
        with Path(path).open(errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                m = re.match(r"\s*(?:extmodule|module)\s+([^\s:]+)", line)
                if m:
                    module = m[1]
                if "@[" not in line:
                    continue
                locator = line.split("@[", 1)[1].rsplit("]", 1)[0]
                if args.diff and not candidates and not (args.source or args.match):
                    matched = False
                elif candidates:
                    matched = False
                    for src, n in candidates:
                        if not src:
                            continue
                        for token in re.finditer(re.escape(src) + r"\s+(\d+)(?::\d+)?", locator):
                            if abs(int(token[1]) - n) <= args.line_slop:
                                matched = True
                                break
                        if matched:
                            break
                else:
                    matched = bool(pattern.search(locator if args.source else line))
                if matched:
                    hits.append(dict(firrtl=str(Path(path).resolve()), line=line_no, module=module,
                                     declaration=line.strip(), locator=locator))
                    if len(hits) >= args.limit:
                        break
        if len(hits) >= args.limit:
            break
    result = dict(mapping="locator candidates; verify generated module/instance/signal correspondence in Release RTL", hits=hits,
                  truncated=len(hits) >= args.limit, diff_added_lines=len(candidates))
    data = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(data + "\n")
    else:
        print(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("index", help="parse module templates once into a reusable JSON index")
    p.add_argument("--rtl", action="append", help="RTL file or directory; repeatable")
    p.add_argument("--file-list", action="append", help="generated .f list; repeatable")
    p.add_argument("--file-list-base", help="override relative file-list path base")
    p.add_argument("--define", action="append", help="defined conditional macro, in addition to SYNTHESIS")
    p.add_argument("--output", required=True)
    p.set_defaults(run=make_index)
    for name, action in (("trace", trace), ("list", list_signals)):
        p = subs.add_parser(name)
        p.add_argument("--index", required=True)
        p.add_argument("--top", required=True)
        p.add_argument("--allow-stale", action="store_true")
        p.set_defaults(run=action)
        if name == "trace":
            p.add_argument("--signal", required=True, action="append", help="instance/signal under top; repeat for batch")
            p.add_argument("--max-nodes", type=int, default=250000)
            p.add_argument("--json")
            p.add_argument("--markdown")
            p.add_argument("--require-complete", action="store_true", help="exit 2 if any query touches an unknown boundary")
        else:
            p.add_argument("--instance")
            p.add_argument("--match", help="signal-name regex")
    p = subs.add_parser("locate", help="find Chisel source/diff locator candidates in FIRRTL")
    p.add_argument("--firrtl", action="append", required=True)
    p.add_argument("--source", help="regex matched against source locators")
    p.add_argument("--match", help="regex matched against complete FIRRTL lines")
    p.add_argument("--diff", help="unified Chisel git diff; matches added source line locators")
    p.add_argument("--line-slop", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--output")
    p.set_defaults(run=locate)
    args = parser.parse_args(argv)
    try:
        return args.run(args) or 0
    except (OSError, ValueError, KeyError) as e:
        print("rtl-depth: " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
