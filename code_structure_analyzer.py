#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =========================================================================== #
#  CONFIGURATION  —  edit these values, then run:  python code_structure_analyzer.py
#  (anything given on the command line overrides the value here)
# =========================================================================== #

# Folder path of the repository to analyse.
REPO_FOLDER_PATH = r"C:\Users\Joseph\Documents\Code_Analyze\repo"

# Full path of the HTML report to create. structure.json and the _batches/
# work folder (used by --resume) are written in the same folder.
OUTPUT_REPORT_FILE = r"C:\Users\Joseph\Documents\Code_Analyze\structure_report\report.html"

# Number of files processed per batch in the queue.
BATCH_SIZE = 1000

# Parallel worker processes per batch (0 = automatic: CPUs-1, max 4; 1 = none).
WORKERS = 0

# Folder or file NAMES skipped anywhere in the repo. Every other file is analysed,
# whatever its extension. Version-control internals are skipped by default;
# set to [] to include absolutely everything.
EXCLUDE_NAMES = [".git", ".svn", ".hg"]

# Include folders whose name starts with "." (e.g. .github, .mvn, .idea).
INCLUDE_HIDDEN_FOLDERS = True

# Files larger than this are listed but not read (bytes).
MAX_FILE_SIZE_BYTES = 2_000_000

# "Odd ones out" thresholds.
MAX_METHODS_PER_CLASS = 40
MAX_LINES_PER_CLASS = 1000
MAX_LINES_PER_METHOD = 100
MAX_PARAMS_PER_METHOD = 7

# =========================================================================== #

r"""
Code Structure Analyzer  (Java-focused, fully offline, Python standard library only)
===================================================================================

Scans one repository and produces a single self-contained HTML report showing:
  * which files are in which folder, file-name patterns per folder
  * every Java type (class / interface / enum / record / annotation, incl. nested,
    local and anonymous) with ALL its fields, constructors and methods
    - IN  : parameters  (name : Type, annotations, varargs)
    - OUT : return type, declared exceptions
  * light outlines for JS / TS files
  * test linking (technique 1 = test-class name, technique 3 = calls in test body)
  * pattern statistics and "odd ones out"

Every file in the repository is listed and read (any extension). Java files get a
full outline, JS/TS files a light outline, all other files their size/line count.

Large repos are processed as a QUEUE of batches (default 1000 files per batch).
Each finished batch is saved to <report folder>/_batches/, so an interrupted run
can be continued with --resume.

Usage
-----
  1. Edit the CONFIGURATION block below (REPO_FOLDER_PATH, OUTPUT_REPORT_FILE).
  2. Run:   python code_structure_analyzer.py
  Command-line options override the configuration, e.g.
     python code_structure_analyzer.py C:\path\to\repo --report-file C:\out\report.html
     python code_structure_analyzer.py --resume
     python code_structure_analyzer.py --help

Requires Python 3.7+.  No internet, no third-party packages.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

VERSION = "1.6.0"

# Extensions that get a code outline (all other files are still listed and read).
JAVA_EXTS = ("java",)
JS_TS_EXTS = ("js", "jsx", "mjs", "cjs", "ts", "tsx")
# Build files recognised in the Overview.
BUILD_FILE_NAMES = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
                    "settings.gradle.kts", "build.xml")

# --------------------------------------------------------------------------- #
#  Java tokenizer                                                              #
# --------------------------------------------------------------------------- #

ID, OP, STR, NUM = 1, 2, 3, 4

_TOKEN_RE = re.compile(r'''
  (?P<ws>\s+)
| (?P<doc>/\*\*(?!/).*?\*/)
| (?P<cm>/\*.*?\*/|//[^\r\n]*)
| (?P<tb>"""[ \t\f]*\r?\n(?:[^"\\]|\\.|"(?!""))*""")
| (?P<str>"(?:[^"\\\r\n]|\\.)*")
| (?P<chr>'(?:[^'\\\r\n]|\\.){1,10}')
| (?P<num>\.?\d(?:[eEpP][+-]|[\w.])*)
| (?P<id>[^\W\d][\w$]*|\$[\w$]*)
| (?P<op>>>>=|<<=|>>=|\.\.\.|->|::|\+\+|--|&&|\|\||[=!<>+\-*/%&|^]=|[{}()\[\];,.@=<>!~?:+\-*/&|^%])
| (?P<bad>.)
''', re.S | re.X)

OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {")": "(", "]": "[", "}": "{"}


def tokenize(src):
    kinds, vals, lines, docs = [], [], [], {}
    line = 1
    pending_doc = None
    for m in _TOKEN_RE.finditer(src):
        g = m.lastgroup
        t = m.group()
        if g == "ws" or g == "cm":
            line += t.count("\n")
            continue
        if g == "doc":
            pending_doc = t
            line += t.count("\n")
            continue
        if pending_doc is not None:
            docs[len(vals)] = pending_doc
            pending_doc = None
        if g == "id":
            kinds.append(ID)
            vals.append(t)
        elif g == "op" or g == "bad":
            kinds.append(OP)
            vals.append(t)
        elif g == "num":
            kinds.append(NUM)
            vals.append("0")
        else:  # str / chr / text block - content not needed
            kinds.append(STR)
            vals.append('""')
        lines.append(line)
        if g == "tb":
            line += t.count("\n")
    # bracket matching (with light recovery from mismatches)
    match = [-1] * len(vals)
    stack = []
    for i, v in enumerate(vals):
        if kinds[i] != OP:
            continue
        if v in OPENERS:
            stack.append(i)
        elif v in CLOSERS:
            want = CLOSERS[v]
            if stack and vals[stack[-1]] == want:
                j = stack.pop()
                match[i], match[j] = j, i
            else:
                for d in range(len(stack) - 1, max(-1, len(stack) - 4), -1):
                    if vals[stack[d]] == want:
                        j = stack[d]
                        del stack[d:]
                        match[i], match[j] = j, i
                        break
    return kinds, vals, lines, docs, match


# --------------------------------------------------------------------------- #
#  Java parser (declarations for Java 1.4 ... 21+)                             #
# --------------------------------------------------------------------------- #

MODIFIERS = {"public", "protected", "private", "static", "final", "abstract", "native",
             "synchronized", "transient", "volatile", "strictfp", "default", "sealed"}
NOT_TYPE = {"return", "new", "if", "else", "for", "while", "do", "switch", "case", "break",
            "continue", "throw", "try", "catch", "finally", "this", "super", "class",
            "interface", "enum", "instanceof", "assert", "synchronized", "import", "package",
            "true", "false", "null", "yield", "extends", "implements", "throws", "goto",
            "const", "public", "protected", "private", "static", "final", "abstract",
            "native", "transient", "volatile", "strictfp", "default", "permits"}
CONTROL = {"if", "for", "while", "switch", "catch", "synchronized", "return", "throw", "new",
           "else", "do", "try", "assert", "case", "yield"}
STMT_START = {"{", ";", "}", "(", ")", "final", ":"}
DECL_FOLLOW = {"=", ";", ":", ",", ")", "["}
TEST_ANNOS = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate",
              "Property", "Theory", "Example"}
MOCK_ANNOS = {"Mock", "MockBean", "MockitoBean", "Captor", "MockInject", "Mocked", "Injectable"}
_WORD = re.compile(r"[\w$]")


def _fmt_type_tokens(toks):
    out = []
    prev = None
    for t in toks:
        if t == ",":
            out.append(", ")
        elif t in ("extends", "super", "&"):
            out.append(" " + t + " ")
        else:
            if prev is not None and _WORD.match(prev[-1:] or " ") and _WORD.match(t[:1] or " ") \
                    and prev not in ("extends", "super"):
                out.append(" ")
            out.append(t)
        prev = t
    return "".join(out)


def _strip_generics(t):
    if not t:
        return ""
    s = t
    while "<" in s:
        n = re.sub(r"<[^<>]*>", "", s)
        if n == s:
            break
        s = n
    s = s.replace("[]", "").replace("...", "")
    s = re.sub(r"@\w+\s*", "", s).strip()
    return s


def _clean_doc(doc):
    if not doc:
        return None
    s = doc[3:-2]
    s = re.sub(r"^\s*\*+ ?", "", s, flags=re.M)
    s = re.sub(r"\{@\w+\s+([^}]*)\}", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.split("\n@")[0] if not s.lstrip().startswith("@") else ""
    s = " ".join(s.split())
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    if m:
        s = m.group(1)
    return s[:220] if s else None


class Ctx(object):
    __slots__ = ("calls", "locals", "news", "mocks")

    def __init__(self):
        self.calls = []
        self.locals = {}
        self.news = []
        self.mocks = set()


class JavaParser(object):
    PAD = 8

    def __init__(self, src, filename=""):
        k, v, ln, docs, mt = tokenize(src)
        self.n = len(v)
        last = ln[-1] if ln else 1
        self.k = k + [0] * self.PAD
        self.v = v + [""] * self.PAD
        self.ln = ln + [last] * self.PAD
        self.mt = mt + [-1] * self.PAD
        self.docs = docs
        self.filename = filename
        self.anon = 0
        self.recoveries = 0

    # ---- small helpers ----------------------------------------------------
    def close_of(self, i):
        m = self.mt[i]
        return m if m > i else self.n

    def after(self, i):
        m = self.mt[i]
        if m > i:
            return m + 1
        return self.n if self.v[i] == "{" else i + 1

    def field_ctx(self, owner):
        c = owner.get("_fi")
        if c is None:
            c = owner["_fi"] = Ctx()
        return c

    def doc_at(self, i):
        return self.docs.get(i)

    def new_type(self, kind, name, owner, line):
        if kind == "anonymous":
            q = (owner["q"] if owner else "") + ".$" + str(self.anon)
        else:
            q = (owner["q"] + "." + name) if owner else name
        return {"k": kind, "n": name, "q": q, "l": line, "e": line, "m": [], "a": [],
                "ext": [], "impl": [], "f": [], "fn": [], "nt": []}

    def is_type_start(self, i):
        v, k = self.v, self.k
        x = v[i]
        if x in ("class", "interface", "enum") and k[i + 1] == ID:
            return True
        if x == "@" and v[i + 1] == "interface":
            return True
        if x == "record" and k[i] == ID and k[i + 1] == ID and v[i + 2] in ("(", "<"):
            return True
        return False

    # ---- entry point ------------------------------------------------------
    def parse(self):
        v, k, n = self.v, self.k, self.n
        res = {"pkg": "", "imp": [], "types": []}
        implicit = None
        i = 0
        is_module_info = self.filename.endswith("module-info.java")
        while i < n:
            start = i
            if v[i] == ";":
                i += 1
                continue
            doc = self.doc_at(i)
            mods, annos, i = self.parse_modifiers(i)
            x = v[i]
            if x == "package" and k[i] == ID:
                j = i + 1
                parts = []
                while j < n and v[j] != ";":
                    parts.append(v[j])
                    j += 1
                res["pkg"] = "".join(parts)
                i = j + 1
                continue
            if x == "import" and k[i] == ID:
                j = i + 1
                static = False
                if v[j] == "static":
                    static = True
                    j += 1
                parts = []
                while j < n and v[j] != ";":
                    parts.append(v[j])
                    j += 1
                res["imp"].append(("static " if static else "") + "".join(parts))
                i = j + 1
                continue
            if is_module_info and (x == "module" or (x == "open" and v[i + 1] == "module")):
                if x == "open":
                    i += 1
                j = i + 1
                parts = []
                while j < n and v[j] != "{":
                    parts.append(v[j])
                    j += 1
                res["module"] = "".join(parts)
                i = self.after(j) if j < n else n
                continue
            if self.is_type_start(i):
                t, i = self.parse_type_decl(i, mods, annos, doc, None)
                res["types"].append(t)
                continue
            if x == "}":
                i += 1
                continue
            # Java 21+ unnamed / implicit class: top-level methods & fields
            if implicit is None:
                implicit = self.new_type("class", "(implicit class)", None, self.ln[i])
                implicit["implicit"] = True
                res["types"].append(implicit)
            i = self.parse_member(i, implicit, mods, annos, doc)
            if i <= start:
                i = start + 1
        if implicit is not None:
            implicit["e"] = self.ln[n - 1] if n else 1
        res["recoveries"] = self.recoveries
        for t in _walk_types(res["types"]):
            c = t.pop("_fi", None)
            if c is not None and (c.calls or c.news):
                t["fi"] = {"calls": c.calls, "news": c.news}
        return res

    # ---- modifiers / annotations -----------------------------------------
    def parse_modifiers(self, i):
        v, k = self.v, self.k
        mods, annos = [], []
        while i < self.n:
            x = v[i]
            if x == "@" and v[i + 1] != "interface" and k[i + 1] == ID:
                j = i + 1
                name = v[j]
                j += 1
                while v[j] == "." and k[j + 1] == ID:
                    name = v[j + 1]
                    j += 2
                if v[j] == "(":
                    j = self.after(j)
                annos.append(name)
                i = j
                continue
            if k[i] == ID and x in MODIFIERS:
                if x in ("sealed", "default") and not (k[i + 1] == ID or v[i + 1] in ("<", "@")):
                    break
                mods.append(x)
                i += 1
                continue
            if x == "non" and v[i + 1] == "-" and v[i + 2] == "sealed":
                mods.append("non-sealed")
                i += 3
                continue
            break
        return mods, annos, i

    # ---- types ------------------------------------------------------------
    def parse_type(self, i):
        v, k = self.v, self.k
        while v[i] == "@" and v[i + 1] != "interface" and k[i + 1] == ID:
            _, _, i = self.parse_modifiers(i)
        if k[i] != ID or v[i] in NOT_TYPE:
            return None, i
        parts = []
        while True:
            parts.append(v[i])
            i += 1
            if v[i] == "<":
                ta, j = self.parse_type_args(i)
                if ta is None:
                    return None, i
                parts.append(ta)
                i = j
            if v[i] == "." and k[i + 1] == ID and v[i + 1] not in NOT_TYPE:
                parts.append(".")
                i += 1
                continue
            break
        while v[i] == "[" and v[i + 1] == "]":
            parts.append("[]")
            i += 2
        return "".join(parts), i

    def parse_type_args(self, i):
        v, k = self.v, self.k
        depth = 0
        out = []
        while i < self.n:
            x = v[i]
            if x == "<":
                depth += 1
            elif x == ">":
                depth -= 1
            elif not (k[i] == ID or x in (".", ",", "?", "[", "]", "&", "@")):
                return None, i
            out.append(x)
            i += 1
            if depth == 0:
                return _fmt_type_tokens(out), i
            if len(out) > 400:
                return None, i
        return None, i

    def parse_type_list(self, i):
        lst = []
        while i < self.n:
            _, _, i = self.parse_modifiers(i)
            t, j = self.parse_type(i)
            if t is None:
                return lst, i
            lst.append(t)
            i = j
            if self.v[i] == ",":
                i += 1
                continue
            break
        return lst, i

    # ---- type declarations ------------------------------------------------
    def parse_type_decl(self, i, mods, annos, doc, owner, local=False):
        v, k = self.v, self.k
        if v[i] == "@":
            kind = "annotation"
            i += 2
        else:
            kind = v[i]
            i += 1
        name = v[i] if k[i] == ID else "?"
        line = self.ln[i]
        i += 1
        t = self.new_type(kind, name, owner, line)
        t["m"] = list(mods)
        t["a"] = list(annos)
        d = _clean_doc(doc)
        if d:
            t["d"] = d
        if local:
            t["local"] = True
        if v[i] == "<":
            tp, j = self.parse_type_args(i)
            if tp is not None:
                t["tp"] = tp
                i = j
        if kind == "record" and v[i] == "(":
            params, i = self.parse_params(i)
            for p in params:
                f = {"n": p[0], "t": p[1], "l": line, "m": ["private", "final"], "comp": True}
                if len(p) > 2:
                    f["a"] = p[2]
                t["f"].append(f)
        while i < self.n:
            x = v[i]
            if x == "{":
                break
            if x in ("extends", "implements", "permits") and k[i] == ID:
                lst, i = self.parse_type_list(i + 1)
                t[{"extends": "ext", "implements": "impl", "permits": "perm"}[x]] = lst
                continue
            if x in (";", "}"):
                return t, i
            i += 1
        if i >= self.n:
            return t, i
        end = self.close_of(i)
        self.parse_body(i, t, kind == "enum")
        t["e"] = self.ln[min(end, self.n - 1)]
        return t, end + 1

    def parse_body(self, open_i, owner, is_enum=False):
        v = self.v
        end = self.close_of(open_i)
        i = open_i + 1
        if is_enum:
            i = self.parse_enum_constants(i, end, owner)
        while i < end:
            if v[i] == ";":
                i += 1
                continue
            start = i
            doc = self.doc_at(i)
            mods, annos, i = self.parse_modifiers(i)
            if i >= end:
                break
            i = self.parse_member(i, owner, mods, annos, doc)
            if i <= start:
                i = start + 1
        return end + 1

    def parse_enum_constants(self, i, end, owner):
        v, k = self.v, self.k
        consts = []
        while i < end:
            x = v[i]
            if x == ";":
                i += 1
                break
            if x == ",":
                i += 1
                continue
            _, _, i = self.parse_modifiers(i)
            if k[i] == ID and v[i + 1] in (",", ";", "(", "{", "}") or (k[i] == ID and i + 1 == end):
                name = v[i]
                line = self.ln[i]
                consts.append(name)
                i += 1
                if v[i] == "(":
                    close = self.close_of(i)
                    self.scan(i + 1, close, self.field_ctx(owner), owner)
                    i = close + 1
                if v[i] == "{":
                    body = self.new_type("enum constant", name, owner, line)
                    body["q"] = owner["q"] + "." + name
                    bclose = self.close_of(i)
                    self.parse_body(i, body)
                    body["e"] = self.ln[min(bclose, self.n - 1)]
                    owner["nt"].append(body)
                    i = bclose + 1
                continue
            # not a constant list (e.g. enum with no constants) -> stop
            break
        if consts:
            owner["c"] = consts
        return i

    # ---- members ------------------------------------------------------------
    def parse_member(self, i, owner, mods, annos, doc):
        v, k = self.v, self.k
        x = v[i]
        if x == "{":  # initializer block
            close = self.close_of(i)
            ctx = Ctx()
            self.scan(i + 1, close, ctx, owner)
            static = "static" in mods
            m = self.new_method("init", "static {…}" if static else "{…} (instance init)",
                                self.ln[i], self.ln[min(close, self.n - 1)], mods, annos, None,
                                [], None, [], doc)
            m["b"] = 1
            self.attach_ctx(m, ctx)
            owner["fn"].append(m)
            return close + 1
        if self.is_type_start(i):
            t, j = self.parse_type_decl(i, mods, annos, doc, owner)
            owner["nt"].append(t)
            return j
        tp = None
        if x == "<":
            tp, j = self.parse_type_args(i)
            if tp is None:
                return self.recover(i)
            i = j
        if k[i] == ID and v[i + 1] == "(" and v[i] == owner["n"]:
            return self.parse_method(i, owner, mods, annos, doc, tp, None, "ctor")
        if owner["k"] == "record" and k[i] == ID and v[i] == owner["n"] and v[i + 1] == "{":
            close = self.close_of(i + 1)
            ctx = Ctx()
            self.scan(i + 2, close, ctx, owner)
            params = [[f["n"], f["t"]] for f in owner["f"] if f.get("comp")]
            m = self.new_method("ctor", v[i], self.ln[i], self.ln[min(close, self.n - 1)],
                                mods, annos, tp, params, None, [], doc)
            m["compact"] = True
            m["b"] = 1
            self.attach_ctx(m, ctx)
            owner["fn"].append(m)
            return close + 1
        t, j = self.parse_type(i)
        if t is None or k[j] != ID:
            return self.recover(i)
        if v[j + 1] == "(":
            return self.parse_method(j, owner, mods, annos, doc, tp, t, "method")
        return self.parse_fields(j, owner, t, mods, annos, doc)

    def recover(self, i):
        self.recoveries += 1
        v, n = self.v, self.n
        while i < n:
            x = v[i]
            if x == ";":
                return i + 1
            if x == "{":
                return self.after(i)
            if x == "}":
                return i
            if x in ("(", "["):
                i = self.after(i)
                continue
            i += 1
        return n

    def new_method(self, kind, name, line, end, mods, annos, tp, params, ret, throws, doc):
        m = {"k": kind, "n": name, "l": line, "e": end, "p": params}
        if mods:
            m["m"] = list(mods)
        if annos:
            m["a"] = list(annos)
        if tp:
            m["tp"] = tp
        if ret is not None:
            m["r"] = ret
        if throws:
            m["th"] = throws
        d = _clean_doc(doc)
        if d:
            m["d"] = d
        return m

    def attach_ctx(self, m, ctx):
        if ctx.calls:
            m["calls"] = ctx.calls
        if ctx.locals:
            m["loc"] = ctx.locals
        if ctx.news:
            m["news"] = ctx.news
        if ctx.mocks:
            m["mk"] = sorted(ctx.mocks)

    def parse_params(self, i):
        v, k = self.v, self.k
        end = self.close_of(i)
        params = []
        i += 1
        while i < end:
            _, annos, i = self.parse_modifiers(i)
            t, j = self.parse_type(i)
            if t is None:
                break
            if v[j] == "@":
                _, _, j = self.parse_modifiers(j)
            if v[j] == "...":
                t += "..."
                j += 1
            name = v[j] if k[j] == ID else ""
            j += 1
            receiver = name == "this"
            while v[j] == "." and k[j + 1] == ID:
                if v[j + 1] == "this":
                    receiver = True
                j += 2
            while v[j] == "[" and v[j + 1] == "]":
                t += "[]"
                j += 2
            if not receiver:
                params.append([name, t, annos] if annos else [name, t])
            i = j
            if v[i] == ",":
                i += 1
                continue
            break
        return params, end + 1

    def parse_method(self, i, owner, mods, annos, doc, tp, ret, kind):
        v = self.v
        name = v[i]
        line = self.ln[i]
        params, j = self.parse_params(i + 1)
        while v[j] == "[" and v[j + 1] == "]":
            ret = (ret or "") + "[]"
            j += 2
        throws = []
        if v[j] == "throws":
            throws, j = self.parse_type_list(j + 1)
        if v[j] == "default":  # annotation element default value
            j = self.expr_end(j + 1)
        m = self.new_method(kind, name, line, line, mods, annos, tp, params, ret, throws, doc)
        if v[j] == "{":
            close = self.close_of(j)
            ctx = Ctx()
            for p in params:
                ctx.locals[p[0]] = p[1]
            self.scan(j + 1, close, ctx, owner)
            m["e"] = self.ln[min(close, self.n - 1)]
            m["b"] = 1
            ctx.locals = {a: b for a, b in ctx.locals.items()}
            self.attach_ctx(m, ctx)
            j = close + 1
        elif v[j] == ";":
            j += 1
        owner["fn"].append(m)
        return j

    def is_mock_init(self, i):
        v = self.v
        if v[i] in ("mock", "createMock", "createNiceMock", "createStrictMock") and v[i + 1] == "(":
            return True
        if v[i] in ("Mockito", "EasyMock", "PowerMockito") and v[i + 1] == "." and \
                v[i + 2] in ("mock", "createMock", "createNiceMock", "createStrictMock"):
            return True
        return False

    def parse_fields(self, j, owner, t, mods, annos, doc):
        v, k = self.v, self.k
        d = _clean_doc(doc)
        while k[j] == ID:
            name = v[j]
            line = self.ln[j]
            j += 1
            ft = t
            while v[j] == "[" and v[j + 1] == "]":
                ft += "[]"
                j += 2
            f = {"n": name, "t": ft, "l": line}
            if mods:
                f["m"] = list(mods)
            if annos:
                f["a"] = list(annos)
            if d:
                f["d"] = d
            if v[j] == "=":
                end = self.expr_end(j + 1)
                if self.is_mock_init(j + 1):
                    f["mock"] = True
                self.scan(j + 1, end, self.field_ctx(owner), owner)
                j = end
            owner["f"].append(f)
            if v[j] == ",":
                j += 1
                continue
            break
        if v[j] == ";":
            j += 1
        return j

    # ---- expression helpers ------------------------------------------------
    def expr_end(self, i):
        v, k, n = self.v, self.k, self.n
        while i < n:
            x = v[i]
            if x in OPENERS:
                m = self.mt[i]
                i = m + 1 if m > i else i + 1
                continue
            if x == "new" and k[i] == ID:
                t, j = self.parse_type(i + 1)
                i = j if t else i + 1
                continue
            if x == "." and v[i + 1] == "<":
                ta, j = self.parse_type_args(i + 1)
                i = j if ta else i + 1
                continue
            if x in (",", ";", ")", "}", "]"):
                return i
            i += 1
        return n

    def count_args(self, i):
        end = self.close_of(i)
        if end == i + 1:
            return 0
        v, k = self.v, self.k
        c = 0
        j = i + 1
        while j < end:
            x = v[j]
            if x in OPENERS:
                m = self.mt[j]
                j = m + 1 if m > j else j + 1
                continue
            if x == "new" and k[j] == ID:
                t, jj = self.parse_type(j + 1)
                j = jj if t else j + 1
                continue
            if x == "." and v[j + 1] == "<":
                ta, jj = self.parse_type_args(j + 1)
                j = jj if ta else j + 1
                continue
            if x == ",":
                c += 1
            j += 1
        return c + 1

    def receiver(self, i):
        v, k = self.v, self.k
        p = i - 1
        if v[p] == ">":
            d = 0
            while p > 0:
                if v[p] == ">":
                    d += 1
                elif v[p] == "<":
                    d -= 1
                    if d == 0:
                        break
                p -= 1
            p -= 1
        if p < 0 or v[p] != ".":
            return ""
        p -= 1
        if p < 0:
            return "?"
        if k[p] == ID:
            s = p
            while s >= 2 and v[s - 1] == "." and k[s - 2] == ID:
                s -= 2
            return "".join(v[s:p + 1])
        if v[p] == ")":
            m = self.mt[p]
            if 0 <= m < p:
                q = m - 1
                if v[q] == ">":  # new Foo<>().bar()  /  new Foo<T>().bar()
                    d = 0
                    while q > 0:
                        if v[q] == ">":
                            d += 1
                        elif v[q] == "<":
                            d -= 1
                            if d == 0:
                                break
                        q -= 1
                    q -= 1
                if k[q] == ID:
                    s = q
                    while v[s - 1] == "." and k[s - 2] == ID:
                        s -= 2
                    if v[s - 1] == "new":  # new Foo(...).bar() -> receiver type Foo
                        return "new:" + "".join(v[s:q + 1])
                    return v[q] + "()"
            return "?"
        if k[p] == STR:
            return '""'
        return "?"

    # ---- body scanning (calls, locals, anonymous/local classes) ------------
    def scan(self, i, end, ctx, owner):
        v, k, ln = self.v, self.k, self.ln
        while i < end:
            x = v[i]
            if k[i] == ID:
                if x == "new":
                    i = self.scan_new(i, end, ctx, owner)
                    continue
                prev = v[i - 1] if i > 0 else ";"
                if prev != "." and self.is_type_start(i):
                    t, j = self.parse_type_decl(i, [], [], None, owner, local=True)
                    owner["nt"].append(t)
                    i = j
                    continue
                if v[i + 1] == "(" and x not in CONTROL:
                    ctx.calls.append([self.receiver(i), x, self.count_args(i + 1), ln[i]])
                    i += 1
                    continue
                if x in ("instanceof", "case"):
                    t, j = self.parse_type(i + 1)
                    if t and k[j] == ID and v[j] not in NOT_TYPE and v[j] != "when":
                        ctx.locals[v[j]] = t          # pattern variable: case Circle c / instanceof Circle c
                        i = j + 1
                        continue
                    if t and v[j] == "(" and t[:1].isupper():
                        i = j + 1                     # record pattern: case Square(double side) - not a call
                        continue
                    i += 1
                    continue
                if prev in STMT_START and x not in NOT_TYPE:
                    t, j = self.parse_type(i)
                    if t and k[j] == ID and v[j] not in NOT_TYPE and v[j + 1] in DECL_FOLLOW:
                        name = v[j]
                        typ = t
                        if v[j + 1] == "=":
                            if t == "var" and v[j + 2] == "new":
                                t2, _ = self.parse_type(j + 3)
                                if t2:
                                    typ = t2
                            if self.is_mock_init(j + 2):
                                ctx.mocks.add(name)
                        ctx.locals[name] = typ
                        i = j + 1
                        continue
                i += 1
                continue
            if x == "::":
                if k[i + 1] == ID:
                    recv = v[i - 1] if k[i - 1] == ID else "?"
                    ctx.calls.append([recv, v[i + 1], -1, ln[i]])
                i += 2
                continue
            i += 1

    def scan_new(self, i, end, ctx, owner):
        v = self.v
        _, _, j = self.parse_modifiers(i + 1)
        t, j2 = self.parse_type(j)
        if t is None:
            return i + 1
        if v[j2] == "(":
            close = self.close_of(j2)
            base = _strip_generics(t)
            ctx.news.append([base, self.count_args(j2), self.ln[i]])
            self.scan(j2 + 1, close, ctx, owner)
            nxt = close + 1
            if v[nxt] == "{":
                self.anon += 1
                a = self.new_type("anonymous", "new " + base.split(".")[-1] + "() {…}",
                                  owner, self.ln[nxt])
                a["ext"] = [t]
                bclose = self.close_of(nxt)
                self.parse_body(nxt, a)
                a["e"] = self.ln[min(bclose, self.n - 1)]
                owner["nt"].append(a)
                return bclose + 1
            return nxt
        return j2


# --------------------------------------------------------------------------- #
#  JS / TS light outline (regex based)                                         #
# --------------------------------------------------------------------------- #

_JS_PATTERNS = [
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"
                         r"(?:\s+extends\s+([\w$.]+))?", re.M)),
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)", re.M)),
    ("enum", re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)", re.M)),
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^=]*>)?\s*=", re.M)),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*"
                            r"([A-Za-z_$][\w$]*)\s*(<[^>(]*>)?\s*\(([^)]*)\)\s*(?::\s*([^{;=]+))?", re.M)),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
                            r"(?:async\s+)?(?:function\s*)?\(([^)]*)\)\s*(?::\s*([^=>{]+))?\s*(?:=>|\{)", re.M)),
    ("method", re.compile(r"^\s+(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*"
                          r"(?!if\b|for\b|while\b|switch\b|catch\b|return\b|function\b)"
                          r"([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*(?::\s*([^{;]+))?\s*\{", re.M)),
    ("const", re.compile(r"^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::\s*([^=]+?))?\s*=", re.M)),
]
_JS_DECL = re.compile(r"\s*(?:export\s+)?(const|let|var)\b")
_JS_IMPORT = re.compile(r"^\s*import\s+.*?from\s+['\"]([^'\"]+)['\"]|require\(\s*['\"]([^'\"]+)['\"]\s*\)", re.M)


def outline_js(src):
    syms = []
    seen = set()
    for kind, rx in _JS_PATTERNS:
        for m in rx.finditer(src):
            name = m.group(1)
            line = src.count("\n", 0, m.start(1)) + 1
            key = (name, line)
            if key in seen:
                continue
            seen.add(key)
            s = {"k": kind, "n": name, "l": line}
            if kind in ("function", "method"):
                g = m.groups()
                if kind == "function" and len(g) == 4:
                    params, ret = g[2], g[3]
                else:
                    params, ret = g[1], g[2] if len(g) > 2 else None
                s["p"] = " ".join((params or "").split())
                if ret:
                    s["r"] = ret.strip()
            if kind == "class" and m.group(2):
                s["ext"] = m.group(2)
            if kind == "function":
                d = _JS_DECL.match(m.group(0))
                if d:
                    s["decl"] = d.group(1)
            if kind == "method" and re.search(r"\bstatic\b", src[m.start():m.start(1)]):
                s["static"] = True
            if kind == "const" and m.group(2):
                s["r"] = m.group(2).strip()
            syms.append(s)
    syms.sort(key=lambda s: s["l"])
    imps = [a or b for a, b in _JS_IMPORT.findall(src)]
    return syms[:500], imps[:200]


# --------------------------------------------------------------------------- #
#  Per-file worker                                                             #
# --------------------------------------------------------------------------- #

_TEST_DIR_RE = re.compile(r"(^|/)(src/test|src/it|src/integrationTest|test|tests|it)(/|$)")
_TEST_NAME_RE = re.compile(r"(Test|Tests|IT|ITCase|TestCase|IntegrationTest|Spec)\.java$|(^|/)Test[A-Z]\w*\.java$")


def _walk_types(types):
    for t in types:
        yield t
        for c in _walk_types(t.get("nt", [])):
            yield c


def _mark_tests(rec):
    path_test = bool(_TEST_DIR_RE.search(rec["p"]))
    any_test = False
    for t in _walk_types(rec.get("types", [])):
        is_junit3 = any(_strip_generics(e).split(".")[-1] in ("TestCase", "GroovyTestCase",
                                                                "AbstractJUnit4SpringContextTests")
                        for e in t.get("ext", []))
        class_level = "Test" in t.get("a", [])  # TestNG class-level @Test
        has = False
        for m in t.get("fn", []):
            annos = set(m.get("a", []))
            if annos & TEST_ANNOS or \
                    (is_junit3 and m["n"].startswith("test") and not m["p"] and "public" in m.get("m", [])) or \
                    (class_level and m["k"] == "method" and "public" in m.get("m", []) and m.get("r") == "void"):
                m["test"] = True
                has = True
        if has or is_junit3:
            t["test"] = True
            any_test = True
    rec["test"] = bool(path_test or any_test or _TEST_NAME_RE.search(rec["p"]))
    return rec


def _strip_body_info(types):
    for t in _walk_types(types):
        for m in t.get("fn", []):
            for key in ("calls", "loc", "news", "mk"):
                m.pop(key, None)


def parse_file_task(args):
    root, rel, max_size, keep_calls = args
    full = os.path.join(root, rel)
    name = rel.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name[1:] else ""
    lang = ext or "(no extension)"
    rec = {"p": rel, "x": ext, "lang": lang}
    try:
        size = os.path.getsize(full)
    except OSError as e:
        rec["err"] = "unreadable: %s" % e
        return rec
    rec["s"] = size
    if size > max_size:
        rec["skipped"] = "larger than MAX_FILE_SIZE_BYTES (%d bytes)" % max_size
        return rec
    try:
        with open(full, "rb") as fh:
            raw = fh.read()
        if b"\x00" in raw[:4096]:
            rec["binary"] = True
            return rec
        src = raw.decode("utf-8", errors="replace").lstrip("﻿")
    except Exception as e:  # noqa
        rec["err"] = "unreadable: %s" % e
        return rec
    rec["ln"] = src.count("\n") + 1
    try:
        if ext in JAVA_EXTS:
            res = JavaParser(src, name).parse()
            rec.update(res)
            _mark_tests(rec)
        elif ext in JS_TS_EXTS:
            syms, imps = outline_js(src)
            rec["sym"] = syms
            if imps:
                rec["imp"] = imps
    except RecursionError:
        rec["err"] = "too deeply nested to parse"
    except Exception as e:  # noqa - never let one file kill the run
        rec["err"] = "parse error: %s: %s" % (type(e).__name__, e)
    return rec


# --------------------------------------------------------------------------- #
#  Scanner + batch queue                                                       #
# --------------------------------------------------------------------------- #

def scan_repo(root, excludes, include_hidden):
    files, dirs = [], []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in excludes and (include_hidden or not d.startswith(".")))
        rel_dir = os.path.relpath(dp, root).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        dirs.append(rel_dir)
        for f in sorted(fn):
            if f in excludes:
                continue
            files.append((rel_dir + "/" + f) if rel_dir else f)
    return files, dirs


def _hash_list(items):
    h = hashlib.sha1()
    for x in items:
        h.update(x.encode("utf-8", "replace"))
        h.update(b"\n")
    return h.hexdigest()


def run_queue(root, files, out_dir, batch_size, workers, resume, max_size, keep_calls, log):
    bdir = os.path.join(out_dir, "_batches")
    os.makedirs(bdir, exist_ok=True)
    manifest_path = os.path.join(bdir, "manifest.json")
    manifest = {"version": VERSION, "root": os.path.abspath(root), "batch_size": batch_size,
                "files_hash": _hash_list(files), "n_files": len(files), "keep_calls": keep_calls}
    batches = [files[i:i + batch_size] for i in range(0, len(files), batch_size)] or [[]]
    can_resume = False
    if resume and os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                old = json.load(fh)
            can_resume = all(old.get(k) == manifest[k] for k in manifest)
            if not can_resume:
                log("  ! --resume ignored: the file list or settings changed since the last run.")
        except Exception:
            can_resume = False
    if not can_resume:
        for f in os.listdir(bdir):
            if f.startswith("batch_") and f.endswith(".json"):
                os.remove(os.path.join(bdir, f))
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    executor = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    total = len(batches)
    t_all = time.time()
    try:
        for bi, batch in enumerate(batches, 1):
            path = os.path.join(bdir, "batch_%05d.json" % bi)
            lo = (bi - 1) * batch_size + 1
            hi = lo + len(batch) - 1
            if can_resume and os.path.exists(path):
                log("  Batch %d/%d  files %d-%d  … already done (resumed)" % (bi, total, lo, hi))
                continue
            t0 = time.time()
            tasks = [(root, rel, max_size, keep_calls) for rel in batch]
            if executor:
                results = list(executor.map(parse_file_task, tasks, chunksize=16))
            else:
                results = [parse_file_task(t) for t in tasks]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"batch": bi, "files": results}, fh, separators=(",", ":"))
            os.replace(tmp, path)
            nj = sum(1 for r in results if r.get("lang") == "java")
            ne = sum(1 for r in results if r.get("err"))
            log("  Batch %d/%d  files %d-%d  … done in %.1fs  (java %d, other %d, errors %d)"
                % (bi, total, lo, hi, time.time() - t0, nj, len(results) - nj, ne))
    finally:
        if executor:
            executor.shutdown()
    log("  Queue finished in %.1fs" % (time.time() - t_all))
    merged = []
    for bi in range(1, total + 1):
        with open(os.path.join(bdir, "batch_%05d.json" % bi), "r", encoding="utf-8") as fh:
            merged.extend(json.load(fh)["files"])
    return merged


# --------------------------------------------------------------------------- #
#  Analysis: naming patterns                                                   #
# --------------------------------------------------------------------------- #

def naming_style(s):
    if not s:
        return "dotfile"
    if re.fullmatch(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)+", s):
        return "UPPER_SNAKE"
    if re.fullmatch(r"[A-Z][A-Z0-9]*", s) and len(s) > 1:
        return "UPPERCASE"
    if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", s):
        return "kebab-case"
    if re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)+", s):
        return "snake_case"
    if re.fullmatch(r"[A-Z][a-zA-Z0-9]*", s):
        return "PascalCase"
    if re.fullmatch(r"[a-z][a-z0-9]*", s):
        return "lowercase"
    if re.fullmatch(r"[a-z][a-zA-Z0-9]*", s):
        return "camelCase"
    return "mixed"


ROLE_SUFFIXES = sorted([
    "ServiceImpl", "Impl", "Controller", "RestController", "Resource", "Endpoint", "Service",
    "Repository", "Repo", "Dao", "DAO", "Entity", "Dto", "DTO", "VO", "Model", "Mapper",
    "Converter", "Config", "Configuration", "Properties", "Util", "Utils", "Helper", "Factory",
    "Builder", "Handler", "Listener", "Filter", "Interceptor", "Exception", "Error", "Validator",
    "Client", "Adapter", "Provider", "Manager", "Processor", "Job", "Task", "Event", "Request",
    "Response", "Constants", "Test", "Tests", "IT", "Servlet", "Action", "Bean", "Facade",
    "Strategy", "Command", "Query", "Aspect", "Advice", "Application", "Module",
], key=len, reverse=True)
LAYER_WORDS = {"controller", "controllers", "web", "rest", "api", "resource", "resources",
               "service", "services", "impl", "repository", "repositories", "repo", "dao",
               "model", "models", "entity", "entities", "domain", "dto", "dtos", "vo",
               "config", "configuration", "util", "utils", "helper", "helpers", "mapper",
               "mappers", "exception", "exceptions", "common", "core", "constants",
               "handler", "handlers", "filter", "filters", "security", "persistence"}
ROLE_PACKAGES = {"Controller": {"controller", "controllers", "web", "rest", "api", "resource",
                                "resources", "endpoint"},
                 "Repository": {"repository", "repositories", "repo", "dao", "persistence"},
                 "Dao": {"dao", "repository", "repositories", "persistence"},
                 "Entity": {"entity", "entities", "model", "models", "domain"}}


def role_of(name):
    for s in ROLE_SUFFIXES:
        if name.endswith(s) and len(name) > len(s):
            return s
    return None


# --------------------------------------------------------------------------- #
#  Analysis: test linking (technique 1 = name, technique 3 = calls)            #
# --------------------------------------------------------------------------- #

TEST_CLASS_SUFFIXES = ["IntegrationTests", "IntegrationTest", "ITCase", "TestCase", "Tests",
                       "Test", "IT", "Spec"]


def test_target_name(cls):
    for s in TEST_CLASS_SUFFIXES:
        if cls.endswith(s) and len(cls) > len(s):
            return cls[:-len(s)]
    if cls.startswith("Test") and len(cls) > 4 and cls[4].isupper():
        return cls[4:]
    return None


class Linker(object):
    def __init__(self, files):
        self.files = files
        self.index = defaultdict(list)          # simple name -> [(type, file)]  (main code)
        self.test_index = defaultdict(list)     # simple name -> [(type, file)]  (test code)
        self.implementers = defaultdict(list)   # iface simple name -> [(type, file)]
        self.by_id = {}
        self._cache = {}
        for f in files:
            for t in _walk_types(f.get("types", [])):
                self.by_id[t["id"]] = (t, f)
                if t["k"] in ("anonymous", "enum constant") or t.get("local"):
                    continue
                (self.test_index if f.get("test") else self.index)[t["n"]].append((t, f))
                if not f.get("test"):
                    for s in t.get("ext", []) + t.get("impl", []):
                        self.implementers[_strip_generics(s).split(".")[-1]].append((t, f))

    @staticmethod
    def fq(entry):
        t, f = entry
        return (f.get("pkg") + "." if f.get("pkg") else "") + t["q"]

    def pick(self, cands, f, base=None):
        if not cands:
            return None
        if base and "." in base:
            c2 = [c for c in cands if self.fq(c).endswith(base)]
            cands = c2 or cands
        if len(cands) == 1:
            return cands[0]
        imps = set(f.get("imp", []))
        for c in cands:
            fq = self.fq(c)
            if fq in imps or fq.rsplit(".", 1)[0] in imps:
                return c
        pkg = f.get("pkg", "")
        same = [c for c in cands if c[1].get("pkg", "") == pkg]
        if same:
            return same[0]
        for c in cands:
            if (c[1].get("pkg", "") + ".*") in imps:
                return c

        def common(c):
            a, b = c[1].get("pkg", "").split("."), pkg.split(".")
            n = 0
            for x, y in zip(a, b):
                if x != y:
                    break
                n += 1
            return n
        return sorted(cands, key=common, reverse=True)[0]

    def resolve(self, typ, f):
        if not typ:
            return None
        key = (typ, f["p"])
        if key in self._cache:
            return self._cache[key]
        base = _strip_generics(typ)
        simple = base.split(".")[-1]
        r = None
        if simple and simple[0].isupper():
            r = self.pick(self.index.get(simple, []), f, base)
        self._cache[key] = r
        return r

    def find_methods(self, entry, name, argc, depth=0):
        t, f = entry
        cands = [m for m in t.get("fn", []) if m["k"] == "method" and m["n"] == name]
        hits = []
        for m in cands:
            np = len(m["p"])
            var = np and m["p"][-1][1].endswith("...")
            if argc < 0 or np == argc or (var and argc >= np - 1):
                hits.append(m)
        if not hits and len(cands) == 1:
            hits = cands
        if not hits and depth < 5:
            for s in t.get("ext", []) + t.get("impl", []):
                sup = self.resolve(s, f)
                if sup and sup[0] is not t:
                    hits = self.find_methods(sup, name, argc, depth + 1)
                    if hits:
                        break
        return hits

    def find_ctors(self, entry, argc):
        t, _ = entry
        return [m for m in t.get("fn", []) if m["k"] == "ctor" and
                (len(m["p"]) == argc or (m["p"] and m["p"][-1][1].endswith("...") and argc >= len(m["p"]) - 1))]

    def related_ids(self, entry):
        """target type + its interfaces + implementers (used for 'name' confidence)."""
        t, f = entry
        ids = {t["id"]}
        for s in t.get("impl", []) + t.get("ext", []):
            r = self.resolve(s, f)
            if r:
                ids.add(r[0]["id"])
        for impl in self.implementers.get(t["n"], []):
            ids.add(impl[0]["id"])
        return ids

    def class_vars(self, t, f, depth=0):
        vars_ = {}
        for fl in t.get("f", []):
            mock = bool(set(fl.get("a", [])) & MOCK_ANNOS) or fl.get("mock", False)
            vars_[fl["n"]] = (fl["t"], mock)
        if depth < 5:
            for s in t.get("ext", []):
                simple = _strip_generics(s).split(".")[-1]
                for base in self.test_index.get(simple, []):
                    if base[0] is not t:
                        inherited = self.class_vars(base[0], base[1], depth + 1)
                        for kname, val in inherited.items():
                            vars_.setdefault(kname, val)
                        break
        return vars_

    def static_imports(self, f):
        members, wild = {}, []
        for imp in f.get("imp", []):
            if not imp.startswith("static "):
                continue
            path = imp[7:]
            cls, _, mem = path.rpartition(".")
            simple = cls.split(".")[-1]
            if mem == "*":
                wild.append(simple)
            else:
                members[mem] = simple
        return members, wild

    def link(self):
        stats = Counter()
        for f in self.files:
            if not f.get("test"):
                continue
            for t in _walk_types(f.get("types", [])):
                if t["k"] in ("anonymous", "enum constant"):
                    continue
                self._link_class(t, f, stats)
        return stats

    def _link_class(self, t, f, stats):
        test_methods = [m for m in t.get("fn", []) if m.get("test")]
        name_ids = set()
        # ---- technique 1: test-class name -> class under test
        if t.get("test") or test_methods:
            tname = test_target_name(t["n"])
            if tname:
                cands = self.index.get(tname, []) + self.index.get(tname + "Impl", [])
                concrete = [c for c in cands if c[0]["k"] in ("class", "enum", "record")]
                tgt = self.pick(concrete or cands, f)
                if tgt:
                    t["tf"] = tgt[0]["id"]
                    tgt[0].setdefault("tc", []).append(t["id"])
                    name_ids = self.related_ids(tgt)
                    stats["name_linked_classes"] += 1
                else:
                    t["tf_missing"] = tname
                    stats["name_unlinked_classes"] += 1
        if not test_methods:
            return
        # ---- technique 3: calls in test bodies
        cvars = self.class_vars(t, f)
        smembers, swild = self.static_imports(f)
        own = defaultdict(list)
        for m in t.get("fn", []):
            own[m["n"]].append(m)
        links, helpers = {}, {}
        for m in t.get("fn", []):
            got, hs = {}, set()
            loc = m.get("loc", {})
            mocks = set(m.get("mk", []))
            for recv, name, argc, _line in m.get("calls", []):
                entry = None
                if not recv.startswith("new:") and "." in recv:
                    recv = recv.rsplit(".", 1)[-1]
                if recv == "" or recv in ("this", "super"):
                    if name in own:
                        hs.add(name)
                        continue
                    if recv == "":
                        cls = smembers.get(name)
                        if cls:
                            entry = self.resolve(cls, f)
                        else:
                            for w in swild:
                                e = self.resolve(w, f)
                                if e and any(x["n"] == name for x in e[0].get("fn", [])):
                                    entry = e
                                    break
                elif recv.startswith("new:"):
                    entry = self.resolve(recv[4:], f)
                elif recv in loc:
                    if recv in mocks:
                        continue
                    entry = self.resolve(loc[recv], f)
                elif recv in cvars:
                    typ, mock = cvars[recv]
                    if mock:
                        continue
                    entry = self.resolve(typ, f)
                elif recv[:1].isupper():
                    entry = self.resolve(recv, f)
                if not entry:
                    continue
                hits = self.find_methods(entry, name, argc)
                if not hits and entry[0]["k"] == "interface":
                    for impl in self.implementers.get(entry[0]["n"], [])[:3]:
                        hits += self.find_methods(impl, name, argc)
                for h in hits:
                    got[h["id"]] = h
            for typ, argc, _line in m.get("news", []):
                entry = self.resolve(typ, f)
                if entry:
                    for h in self.find_ctors(entry, argc):
                        got[h["id"]] = h
            links[m["id"]] = got
            helpers[m["id"]] = hs
        # one level of helper propagation (test -> helper method in same class)
        for m in test_methods:
            for h in helpers.get(m["id"], ()):
                for hm in own.get(h, []):
                    if not hm.get("test"):
                        links[m["id"]].update(links.get(hm["id"], {}))
        for m in test_methods:
            got = links.get(m["id"], {})
            if not got:
                stats["test_methods_without_target"] += 1
                continue
            stats["test_methods_linked"] += 1
            tt = []
            for mid, target in got.items():
                owner = self.method_owner.get(mid)
                conf = "strong" if owner in name_ids else "call"
                tt.append([mid, conf])
                target.setdefault("tb", []).append([m["id"], conf])
            m["tt"] = tt


# --------------------------------------------------------------------------- #
#  Analysis: calls inside each function (A) and "called by" (B)                #
# --------------------------------------------------------------------------- #

_GENERIC_VAR = re.compile(r"[A-Z][0-9]?")


def _simple(t):
    return _strip_generics(t or "").split(".")[-1]


def _id_key(x):
    return (x[0], int(x[1:]) if x[1:].isdigit() else 0)


class CallResolver(Linker):
    """Works out, for every call inside every method / constructor / initializer,
    which method in this repo it goes to (kind 'i'), or that it goes to a
    library / JDK type (kind 'x'), or that the receiver type is unknown ('u').
    Then builds the reverse lists: who calls each method."""

    ENUM_BUILTINS = {"values", "valueOf", "ordinal", "name", "compareTo", "getDeclaringClass", "describeConstable"}
    UNUSED_OK = {"readObject", "writeObject", "readResolve", "writeReplace", "readObjectNoData"}
    MAX_CALLS = 400

    def __init__(self, files):
        self.files = files
        self.index = defaultdict(list)          # ALL named types (main + test)
        self.implementers = defaultdict(list)
        self.parent, self.methods, self.owner = {}, {}, {}
        self._cache = {}
        for f in files:
            self._walk(f.get("types", []), None, f)

    def _walk(self, types, par, f):
        for t in types:
            self.parent[t["id"]] = par
            if t["k"] not in ("anonymous", "enum constant"):
                self.index[t["n"]].append((t, f))
                for sup in t.get("ext", []) + t.get("impl", []):
                    self.implementers[_simple(sup)].append((t, f))
            for m in t.get("fn", []):
                self.methods[m["id"]] = m
                self.owner[m["id"]] = (t, f)
            self._walk(t.get("nt", []), t, f)

    def chain(self, t):
        out = []
        while t is not None:
            out.append(t)
            t = self.parent.get(t["id"])
        return out

    def _fields_into(self, t, f, vars_, depth):
        for fl in t.get("f", []):
            vars_.setdefault(fl["n"], fl["t"])
        if depth < 4:
            for sup in t.get("ext", []):
                e = self.resolve(sup, f)
                if e and e[0] is not t:
                    self._fields_into(e[0], e[1], vars_, depth + 1)

    def type_vars(self, t, f):
        vars_ = {}
        for ct in self.chain(t):
            self._fields_into(ct, f, vars_, 0)
        return vars_

    def field_type(self, entry, name, depth=0):
        t, f = entry
        for fl in t.get("f", []):
            if fl["n"] == name:
                return fl["t"], f
        if depth < 4:
            for sup in t.get("ext", []):
                e = self.resolve(sup, f)
                if e and e[0] is not t:
                    r = self.field_type(e, name, depth + 1)
                    if r:
                        return r
        return None

    @staticmethod
    def _hit(owner_name, name, hits):
        return (owner_name + "." + name, "i", [h["id"] for h in hits], hits[0].get("r"))

    def resolve_call(self, recv, name, argc, t, f, chain, cvars, loc, last, smembers, swild):
        """-> (label, kind, target ids, return type of the target)"""
        # ---- this(...) / super(...) constructor delegation
        if recv == "" and name in ("this", "super"):
            entry = (t, f) if name == "this" else None
            if name == "super":
                for sup in t.get("ext", []):
                    entry = self.resolve(sup, f)
                    if entry:
                        break
                if entry is None:
                    sup = t["ext"][0] if t.get("ext") else "Object"
                    return ("super(…) → " + _simple(sup), "x", [], None)
            hits = self.find_ctors(entry, argc)
            return ("new " + entry[0]["n"] + " (" + name + "(…))", "i" if hits else "x",
                    [h["id"] for h in hits], None)
        segs = recv.split(".") if recv and not recv.startswith("new:") else [recv]
        if "this" in segs:                       # this.x / Outer.this.x
            segs = segs[len(segs) - segs[::-1].index("this"):]
        # ---- unqualified call: own class, enclosing classes, parents, static imports
        if segs in ([""], []):
            for ct in chain:
                hits = self.find_methods((ct, f), name, argc)
                if hits:
                    owner = self.owner.get(hits[0]["id"], (ct, f))[0]
                    return self._hit(owner["n"], name, hits)
            if recv == "":
                cls = smembers.get(name)
                for c in ([cls] if cls else swild):
                    e = self.resolve(c, f)
                    if e:
                        hits = self.find_methods(e, name, argc)
                        if hits:
                            return self._hit(e[0]["n"], name, hits)
                    elif cls:
                        return (cls + "." + name, "x", [], None)
                unresolved_wild = [w for w in swild if not self.resolve(w, f)]
                if len(unresolved_wild) == 1:
                    return (unresolved_wild[0] + "." + name, "x", [], None)
            for ct in chain:                    # values(), valueOf(), ordinal() … of an enum
                if ct["k"] == "enum" and name in self.ENUM_BUILTINS:
                    return (ct["n"] + "." + name + " (enum built-in)", "x", [], ct["n"] + "[]" if name == "values" else None)
            for ct in chain:                    # inherited from a library parent class
                for sup in ct.get("ext", []):
                    if not self.resolve(sup, f):
                        return (_simple(sup) + "." + name, "x", [], None)
            return (name, "u", [], None)
        # ---- super.method()
        if segs == ["super"]:
            for sup in t.get("ext", []):
                e = self.resolve(sup, f)
                if not e:
                    return (_simple(sup) + "." + name, "x", [], None)
                hits = self.find_methods(e, name, argc)
                if hits:
                    return self._hit(self.owner[hits[0]["id"]][0]["n"], name, hits)
            return ("Object." + name, "x", [], None)
        # ---- typed receiver
        first, rest, ctxf, typ = segs[0], segs[1:], f, None
        if first.startswith("new:"):
            typ = first[4:]
        elif first.endswith("()"):
            prev = last.get(first[:-2])
            if prev is None:
                return (first[:-2] + "(…)." + name, "u", [], None)
            if prev[0] != "i" or not prev[1]:
                return (first[:-2] + "(…)." + name, "x" if prev[0] == "x" else "u", [], None)
            typ = prev[1]
        elif first == '""':
            typ = "String"
        elif first in loc:
            typ = loc[first]
        elif first in cvars:
            typ = cvars[first]
        elif first[:1].isupper():
            typ = first
        else:
            ups = [i for i, x in enumerate(segs) if x[:1].isupper()]
            if not ups:
                return (recv + "." + name, "u", [], None)
            typ, rest = segs[ups[0]], segs[ups[0] + 1:]      # java.util.Collections.sort
        for seg in rest:
            e = self.resolve(typ, ctxf)
            if e:
                ft = self.field_type(e, seg)
                if ft:
                    typ, ctxf = ft
                    continue
                if seg[:1].isupper():
                    typ = seg
                    continue
                return (e[0]["n"] + "." + seg + "." + name, "u", [], None)
            if seg[:1].isupper():
                typ = seg
                continue
            return (_simple(typ) + "." + seg + "." + name, "x", [], None)   # System.out.println
        simple = _simple(typ)
        if typ == "var" or not simple:
            return (recv + "." + name, "u", [], None)
        e = self.resolve(typ, ctxf)
        if not e:
            if _GENERIC_VAR.fullmatch(simple):
                return (simple + "." + name, "u", [], None)
            return (simple + "." + name, "x" if simple[:1].isupper() else "u", [], None)
        hits = self.find_methods(e, name, argc)
        if hits:
            return self._hit(self.owner[hits[0]["id"]][0]["n"], name, hits)
        if e[0]["k"] == "record" and argc == 0:
            comp = [fl for fl in e[0].get("f", []) if fl["n"] == name and fl.get("comp")]
            if comp:
                return (e[0]["n"] + "." + name + "() (record accessor)", "x", [], comp[0]["t"])
        return (e[0]["n"] + "." + name, "x", [], None)    # inherited from a library parent

    def implementations(self, mid):
        m = self.methods.get(mid)
        ot = self.owner.get(mid, (None, None))[0]
        if not m or not ot or m["k"] != "method":
            return []
        if ot["k"] != "interface" and "abstract" not in m.get("m", []):
            return []
        out = []
        for it, _f in self.implementers.get(ot["n"], [])[:8]:
            for x in it.get("fn", []):
                if x["k"] == "method" and x["n"] == m["n"] and len(x.get("p", [])) == len(m.get("p", [])):
                    out.append(x["id"])
        return out

    def resolve_source(self, src, t, f, chain, cvars, smembers, swild):
        loc = src.get("loc", {})
        last = {}
        out = []
        for recv, name, argc, line in src.get("calls", []):
            label, kind, targets, rtype = self.resolve_call(recv, name, argc, t, f, chain, cvars, loc,
                                                            last, smembers, swild)
            last[name] = (kind, rtype)
            impls = []
            for tid in targets:
                impls += [x for x in self.implementations(tid) if x not in targets and x not in impls]
            out.append([line, label, kind, targets, impls])
        for typ, argc, line in src.get("news", []):
            e = self.resolve(typ, f)
            if e:
                hits = self.find_ctors(e, argc)
                out.append([line, "new " + e[0]["n"], "i", [h["id"] for h in hits] or [e[0]["id"]], []])
            else:
                out.append([line, "new " + _simple(typ), "x", [], []])
        out.sort(key=lambda c: c[0])
        return out[:self.MAX_CALLS]

    def run(self):
        stats = Counter()
        called_by = defaultdict(set)
        for f in self.files:
            if not f.get("types"):
                continue
            smembers, swild = self.static_imports(f)
            for t in _walk_types(f["types"]):
                chain = self.chain(t)
                cvars = self.type_vars(t, f)
                sources = [(m["id"], m) for m in t.get("fn", [])]
                if t.get("fi"):
                    sources.append((t["id"], t["fi"]))
                for caller, src in sources:
                    entries = self.resolve_source(src, t, f, chain, cvars, smembers, swild)
                    for e in entries:
                        stats[e[2]] += 1
                        for tid in e[3] + e[4]:
                            if tid[0] == "M":
                                called_by[tid].add(caller)
                    if entries:
                        if caller[0] == "M":
                            src["cl"] = entries
                        else:
                            t["fcl"] = entries
        for mid, callers in called_by.items():
            self.methods[mid]["cb"] = sorted(callers, key=_id_key)
        return {"in_repo": stats["i"], "library": stats["x"], "unresolved": stats["u"]}

    def unused_private(self):
        for mid, m in self.methods.items():
            if m["k"] != "method" or "private" not in m.get("m", []) or m.get("a") or m.get("test"):
                continue
            if m["n"] in self.UNUSED_OK:
                continue
            if not [c for c in m.get("cb", []) if c != mid]:
                yield mid, m, self.owner[mid]


# --------------------------------------------------------------------------- #
#  Analysis driver                                                             #
# --------------------------------------------------------------------------- #

def _is_snake_test_name(n):
    return "_" in n


def analyze(files, dirs, opts, project):
    # ---- assign ids
    tid = mid = 0
    method_owner = {}
    for f in files:
        for t in _walk_types(f.get("types", [])):
            tid += 1
            t["id"] = "T%d" % tid
            for m in t.get("fn", []):
                mid += 1
                m["id"] = "M%d" % mid
                method_owner[m["id"]] = t["id"]

    # ---- test linking
    linker = Linker(files)
    linker.method_owner = method_owner
    link_stats = linker.link()

    # ---- calls inside each function + called by
    resolver = CallResolver(files)
    call_stats = resolver.run()

    java = [f for f in files if f.get("lang") == "java"]
    main_java = [f for f in java if not f.get("test")]
    test_java = [f for f in java if f.get("test")]

    # ---- counts
    kinds = Counter()
    n_methods = n_fields = n_test_methods = 0
    for f in java:
        for t in _walk_types(f.get("types", [])):
            kinds[t["k"]] += 1
            n_fields += len(t.get("f", []))
            for m in t.get("fn", []):
                n_methods += 1
                if m.get("test"):
                    n_test_methods += 1
    langs = Counter(f.get("lang") or "(no extension)" for f in files)
    build = sorted(set(f["p"] for f in files if f["p"].rsplit("/", 1)[-1] in BUILD_FILE_NAMES))

    # ---- source roots
    roots = Counter()
    for f in java:
        d = f["p"].rsplit("/", 1)[0] if "/" in f["p"] else ""
        pk = f.get("pkg", "").replace(".", "/")
        if pk and (d == pk or d.endswith("/" + pk)):
            roots[d[: len(d) - len(pk)].rstrip("/") or "(repo root)"] += 1
        elif not pk:
            roots[d or "(repo root)"] += 1

    # ---- folder patterns
    folders = defaultdict(list)
    for f in files:
        d = f["p"].rsplit("/", 1)[0] if "/" in f["p"] else ""
        folders[d].append(f)
    folder_rows = []
    for d in sorted(folders):
        fl = folders[d]
        styles = Counter(naming_style(x["p"].rsplit("/", 1)[-1].split(".")[0]) for x in fl)
        exts = Counter(x.get("x") or "(none)" for x in fl)
        main, cnt = styles.most_common(1)[0]
        roles = Counter()
        for x in fl:
            if x.get("lang") == "java":
                r = role_of(x["p"].rsplit("/", 1)[-1][:-5])
                if r:
                    roles[r] += 1
        folder_rows.append({"d": d or "(root)", "n": len(fl), "style": main,
                            "cons": round(100.0 * cnt / len(fl)), "styles": dict(styles),
                            "ext": dict(exts.most_common(6)), "roles": dict(roles.most_common(4))})

    # ---- package patterns (java)
    pk_rows = defaultdict(lambda: {"types": 0, "files": 0, "roles": Counter(), "kinds": Counter(),
                                   "test": 0})
    for f in java:
        row = pk_rows[f.get("pkg") or "(default package)"]
        row["files"] += 1
        if f.get("test"):
            row["test"] += 1
        for t in f.get("types", []):
            row["types"] += 1
            row["kinds"][t["k"]] += 1
            r = role_of(t["n"])
            if r:
                row["roles"][r] += 1
    package_rows = []
    for p in sorted(pk_rows):
        r = pk_rows[p]
        package_rows.append({"p": p, "files": r["files"], "types": r["types"], "test": r["test"],
                             "kinds": dict(r["kinds"]), "roles": dict(r["roles"].most_common(5)),
                             "main_role": r["roles"].most_common(1)[0][0] if r["roles"] else None})
    last_segs = Counter((f.get("pkg") or "").split(".")[-1] for f in main_java if f.get("pkg"))
    layer_pk = sum(c for s, c in last_segs.items() if s.lower() in LAYER_WORDS)
    total_pk_files = sum(last_segs.values()) or 1
    layer_share = 100.0 * layer_pk / total_pk_files
    org = ("LAYERED (packages named by technical layer)" if layer_share >= 60 else
           "FEATURE-BASED (packages named by business feature)" if layer_share <= 25 else
           "MIXED (some layer packages, some feature packages)")

    # ---- anomalies
    anomalies = []

    def add(sev, cat, f, msg, ref=None, line=None):
        a = {"sev": sev, "cat": cat, "p": f["p"], "msg": msg}
        if ref:
            a["ref"] = ref
        if line:
            a["l"] = line
        anomalies.append(a)

    has_test_root = any("src/test/" in f["p"] for f in files)
    existing_pk_segs = set(s.lower() for f in main_java for s in (f.get("pkg") or "").split("."))
    for f in files:
        if f.get("err"):
            add("warn", "Parse problem", f, f["err"])
        if f.get("skipped"):
            add("info", "Skipped file", f, f["skipped"])
    for f in java:
        stem = f["p"].rsplit("/", 1)[-1][:-5]
        d = f["p"].rsplit("/", 1)[0] if "/" in f["p"] else ""
        pk = f.get("pkg", "")
        tops = f.get("types", [])
        if f.get("recoveries"):
            add("info", "Parse problem", f, "Parser skipped %d unrecognised construct(s)" % f["recoveries"])
        if stem not in ("package-info", "module-info"):
            pubs = [t for t in tops if "public" in t.get("m", [])]
            if pubs and pubs[0]["n"] != stem:
                add("warn", "File/class name mismatch", f,
                    "Public type '%s' is in file '%s.java'" % (pubs[0]["n"], stem), pubs[0]["id"], pubs[0]["l"])
            if len([t for t in tops if not t.get("implicit")]) > 1:
                add("info", "Several top-level types in one file", f,
                    "%d top-level types: %s" % (len(tops), ", ".join(t["n"] for t in tops[:6])))
        if pk:
            want = pk.replace(".", "/")
            if not (d == want or d.endswith("/" + want)):
                add("warn", "Package/folder mismatch", f, "package %s but folder is %s/" % (pk, d or "(root)"))
            if pk != pk.lower():
                add("info", "Naming: package", f, "Package '%s' contains uppercase letters" % pk)
        elif stem not in ("module-info",) and tops:
            add("info", "Default package", f, "File has no package declaration")
        if f.get("test") and has_test_root and "src/test/" not in f["p"] and "/test/" not in "/" + f["p"]:
            add("warn", "Test outside test folder", f, "Test code found outside src/test/")
        for t in _walk_types(tops):
            if t["k"] in ("anonymous", "enum constant") or t.get("implicit"):
                continue
            if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", t["n"]):
                add("warn", "Naming: type", f, "Type '%s' is not PascalCase" % t["n"], t["id"], t["l"])
            nm = [m for m in t.get("fn", []) if m["k"] == "method"]
            size = t.get("e", t["l"]) - t["l"] + 1
            if len(nm) > opts.max_methods:
                add("warn", "Large class", f, "%s has %d methods (limit %d)" % (t["n"], len(nm), opts.max_methods),
                    t["id"], t["l"])
            elif size > opts.max_class_lines:
                add("warn", "Large class", f, "%s spans %d lines (limit %d)" % (t["n"], size, opts.max_class_lines),
                    t["id"], t["l"])
            if t.get("tf_missing"):
                add("info", "Test without matching class", f,
                    "%s: no class named '%s' found in main code" % (t["n"], t["tf_missing"]), t["id"], t["l"])
            if not f.get("test"):
                r = role_of(t["n"])
                if r in ROLE_PACKAGES and existing_pk_segs & ROLE_PACKAGES[r]:
                    segs = set(s.lower() for s in pk.split("."))
                    if not segs & ROLE_PACKAGES[r]:
                        add("info", "Role in unexpected package", f,
                            "%s (*%s) is in package '%s'" % (t["n"], r, pk or "(default)"), t["id"], t["l"])
            for fl in t.get("f", []):
                mods = fl.get("m", [])
                if "static" in mods and "final" in mods and \
                        _strip_generics(fl["t"]) in ("int", "long", "double", "float", "short", "byte",
                                                     "char", "boolean", "String") and \
                        not re.fullmatch(r"[A-Z][A-Z0-9_]*", fl["n"]) and fl["n"] != "serialVersionUID":
                    add("info", "Naming: constant", f, "Constant '%s.%s' is not UPPER_SNAKE_CASE" % (t["n"], fl["n"]),
                        t["id"], fl["l"])
            for m in t.get("fn", []):
                if m["k"] != "method":
                    continue
                ln_ = m.get("e", m["l"]) - m["l"] + 1
                if ln_ > opts.max_method_lines:
                    add("warn", "Long method", f, "%s.%s is %d lines (limit %d)" % (t["n"], m["n"], ln_,
                                                                                  opts.max_method_lines),
                        m["id"], m["l"])
                if len(m["p"]) > opts.max_params:
                    add("info", "Many parameters", f, "%s.%s takes %d parameters" % (t["n"], m["n"], len(m["p"])),
                        m["id"], m["l"])
                if not re.fullmatch(r"[a-z][A-Za-z0-9]*", m["n"]) and \
                        not (m.get("test") and re.fullmatch(r"[a-z][A-Za-z0-9_]*", m["n"])):
                    add("info", "Naming: method", f, "Method '%s.%s' is not camelCase" % (t["n"], m["n"]),
                        m["id"], m["l"])
    for mid, m, (t, f) in resolver.unused_private():
        add("info", "Unused private method", f, "%s.%s is private and never called" % (t["n"], m["n"]), mid, m["l"])
    for row in folder_rows:
        fl = folders[row["d"] if row["d"] != "(root)" else ""]
        if row["n"] >= 4 and row["cons"] < 60 and not any(x.get("lang") == "java" for x in fl):
            anomalies.append({"sev": "info", "cat": "Mixed file naming in folder", "p": row["d"],
                              "msg": "File names mix styles: " + ", ".join(
                                  "%s %d" % kv for kv in sorted(row["styles"].items(), key=lambda kv: -kv[1]))})

    # ---- test coverage by package (main code)
    cov = defaultdict(lambda: Counter())
    untested = []
    for f in main_java:
        pk = f.get("pkg") or "(default package)"
        for t in _walk_types(f.get("types", [])):
            if t["k"] not in ("class", "enum", "record") or t.get("local"):
                continue
            c = cov[pk]
            c["classes"] += 1
            tested_any = False
            for m in t.get("fn", []):
                if m["k"] != "method" or not m.get("b") or "private" in m.get("m", []):
                    continue
                c["methods"] += 1
                if m.get("tb"):
                    c["tested"] += 1
                    tested_any = True
                    if any(x[1] == "strong" for x in m["tb"]):
                        c["strong"] += 1
                else:
                    untested.append(m["id"])
            if t.get("tc") or tested_any:
                c["with_test"] += 1
            if t.get("tc"):
                c["with_test_class"] += 1
    cov_rows = [dict(p=p, **{k: v for k, v in c.items()}) for p, c in sorted(cov.items())]
    tot = Counter()
    for c in cov.values():
        tot.update(c)

    meta = {
        "project": project, "root": opts.root_abs, "version": VERSION,
        "scanned": time.strftime("%Y-%m-%d %H:%M"), "batch_size": opts.batch_size,
        "files": len(files), "folders": len(dirs), "java_files": len(java),
        "main_java": len(main_java), "test_java": len(test_java),
        "types": dict(kinds), "methods": n_methods, "fields": n_fields,
        "test_methods": n_test_methods, "java_lines": sum(f.get("ln", 0) for f in java),
        "langs": dict(langs.most_common()), "build_files": build[:50],
        "source_roots": dict(roots.most_common(20)), "organisation": org,
        "layer_share": round(layer_share), "link_stats": dict(link_stats), "call_stats": call_stats,
        "coverage_total": dict(tot), "keep_calls": opts.keep_calls,
        "thresholds": {"max_methods": opts.max_methods, "max_class_lines": opts.max_class_lines,
                       "max_method_lines": opts.max_method_lines, "max_params": opts.max_params},
    }
    return {"meta": meta, "folders": folder_rows, "packages": package_rows,
            "coverage": cov_rows, "untested": untested[:5000], "anomalies": anomalies}


def prune_for_output(files, keep_calls):
    for f in files:
        for t in _walk_types(f.get("types", [])):
            if not keep_calls:
                t.pop("fi", None)
            for key in ("m", "a", "ext", "impl", "f", "fn", "nt"):
                if key in t and not t[key]:
                    del t[key]
            for m in t.get("fn", []):
                if not keep_calls:
                    for key in ("calls", "loc", "news", "mk"):
                        m.pop(key, None)
                if not m.get("p"):
                    m.pop("p", None)


# --------------------------------------------------------------------------- #
#  Output                                                                      #
# --------------------------------------------------------------------------- #

def write_outputs(out_dir, report_file, files, analysis, no_html):
    data = dict(analysis)
    data["files"] = files
    json_path = os.path.join(out_dir, "structure.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    html_path = None
    if not no_html:
        html_path = report_file
        page_data = {k: v for k, v in data.items() if k not in ("coverage", "untested", "folders", "packages")}
        payload = json.dumps(page_data, separators=(",", ":"), ensure_ascii=False)
        payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
        page = HTML_TEMPLATE.replace("__TITLE__", _html_escape(data["meta"]["project"])) \
                            .replace("__DATA__", payload)
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(page)
    return json_path, html_path


def _html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def print_summary(meta, analysis, log):
    m = meta
    ct = m["coverage_total"]
    log("")
    log("=" * 68)
    log(" CODE STRUCTURE SUMMARY — %s" % m["project"])
    log("=" * 68)
    log(" Files: %d in %d folders   Java: %d (main %d, test %d)   Java lines: %d"
        % (m["files"], m["folders"], m["java_files"], m["main_java"], m["test_java"], m["java_lines"]))
    log(" Types: %s" % ", ".join("%s %d" % kv for kv in sorted(m["types"].items(), key=lambda kv: -kv[1])))
    log(" Methods: %d (test methods %d)   Fields: %d" % (m["methods"], m["test_methods"], m["fields"]))
    log(" Organisation: %s" % m["organisation"])
    if m["build_files"]:
        log(" Build files: %s" % ", ".join(m["build_files"][:5]))
    if ct.get("classes"):
        log(" Tests: %d/%d classes have a test (%d%%), %d/%d non-private methods directly tested (%d%%)"
            % (ct.get("with_test", 0), ct["classes"], 100 * ct.get("with_test", 0) // max(1, ct["classes"]),
               ct.get("tested", 0), ct.get("methods", 0), 100 * ct.get("tested", 0) // max(1, ct.get("methods", 0))))
    cats = Counter(a["cat"] for a in analysis["anomalies"])
    if cats:
        log(" Odd ones out: %d  (%s)" % (sum(cats.values()),
                                         ", ".join("%s %d" % kv for kv in cats.most_common(5))))
    log("=" * 68)


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Offline code-structure analyzer (Java-focused). Defaults come from the "
                    "CONFIGURATION block at the top of this file.")
    ap.add_argument("repo", nargs="?", default=REPO_FOLDER_PATH,
                    help="repository folder (default: REPO_FOLDER_PATH = %(default)s)")
    ap.add_argument("--report-file", default=OUTPUT_REPORT_FILE,
                    help="HTML report to write (default: OUTPUT_REPORT_FILE = %(default)s)")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="files per batch (default %(default)s)")
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help="worker processes per batch (0 = auto, 1 = none; default %(default)s)")
    ap.add_argument("--resume", action="store_true", help="continue an interrupted run (skips finished batches)")
    ap.add_argument("--exclude", default="", help="extra comma-separated folder/file names to skip")
    ap.add_argument("--no-excludes", action="store_true", help="ignore EXCLUDE_NAMES (analyse absolutely everything)")
    ap.add_argument("--skip-hidden", action="store_true", help="skip folders starting with '.'")
    ap.add_argument("--max-file-size", type=int, default=MAX_FILE_SIZE_BYTES,
                    help="files larger than this are listed but not read (default %(default)s bytes)")
    ap.add_argument("--keep-calls", action="store_true",
                    help="keep raw call data for every method in structure.json (bigger output; for later linkage)")
    ap.add_argument("--no-html", action="store_true", help="only write structure.json")
    ap.add_argument("--max-methods", type=int, default=MAX_METHODS_PER_CLASS)
    ap.add_argument("--max-class-lines", type=int, default=MAX_LINES_PER_CLASS)
    ap.add_argument("--max-method-lines", type=int, default=MAX_LINES_PER_METHOD)
    ap.add_argument("--max-params", type=int, default=MAX_PARAMS_PER_METHOD)
    ap.add_argument("--quiet", action="store_true", help="less console output")
    opts = ap.parse_args(argv)

    root = os.path.abspath(os.path.expanduser(opts.repo or ""))
    if not opts.repo or not os.path.isdir(root):
        ap.error("repository folder not found: %s\n  Set REPO_FOLDER_PATH at the top of this script "
                 "or pass the folder as the first argument." % (opts.repo or "(empty)"))
    if not opts.report_file:
        ap.error("no report file: set OUTPUT_REPORT_FILE at the top of this script or use --report-file")
    if opts.batch_size < 1:
        ap.error("--batch-size must be >= 1")
    if opts.workers <= 0:
        opts.workers = max(1, min(4, (os.cpu_count() or 2) - 1))
    opts.root_abs = root
    log = (lambda *_: None) if opts.quiet else (lambda s: print(s, flush=True))
    project = os.path.basename(root.rstrip("/\\")) or root
    excludes = set() if opts.no_excludes else set(EXCLUDE_NAMES)
    excludes |= set(x.strip() for x in opts.exclude.split(",") if x.strip())
    report_file = os.path.abspath(os.path.expanduser(opts.report_file))
    if not report_file.lower().endswith((".html", ".htm")):
        report_file += ".html"
    out_dir = os.path.dirname(report_file)
    os.makedirs(out_dir, exist_ok=True)
    try:  # never analyse our own output folder
        if os.path.commonpath([out_dir, root]) == root and out_dir != root:
            excludes.add(os.path.relpath(out_dir, root).split(os.sep)[0])
    except ValueError:
        pass

    t0 = time.time()
    log("Code Structure Analyzer %s — %s" % (VERSION, root))
    log("[1/4] Scanning folders …")
    files, dirs = scan_repo(root, excludes, INCLUDE_HIDDEN_FOLDERS and not opts.skip_hidden)
    if out_dir == root:  # report written into the repo root: skip our own files
        own = {os.path.basename(report_file), "structure.json"}
        files = [f for f in files if f not in own and not f.startswith("_batches/")]
    nb = (len(files) + opts.batch_size - 1) // opts.batch_size
    log("      %d files in %d folders → %d batch(es) of up to %d files" % (len(files), len(dirs), nb, opts.batch_size))
    log("[2/4] Parsing (queue) …")
    recs = run_queue(root, files, out_dir, opts.batch_size, opts.workers, opts.resume,
                     opts.max_file_size, opts.keep_calls, log)
    log("[3/4] Analysing patterns, tests and odd ones out …")
    analysis = analyze(recs, dirs, opts, project)
    prune_for_output(recs, opts.keep_calls)
    log("[4/4] Writing report …")
    jp, hp = write_outputs(out_dir, report_file, recs, analysis, opts.no_html)
    print_summary(analysis["meta"], analysis, log)
    log(" structure.json : %s" % jp)
    if hp:
        log(" report.html    : %s  (%.1f MB)" % (hp, os.path.getsize(hp) / 1e6))
    log(" Total time     : %.1fs" % (time.time() - t0))
    return 0


# --------------------------------------------------------------------------- #
#  HTML template (self-contained, no external resources)                       #
# --------------------------------------------------------------------------- #

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Code Structure — __TITLE__</title>
<style>
:root{--bg:#f6f6f3;--panel:#fff;--ink:#1c1c1e;--muted:#6b6b70;--line:#e3e3df;--soft:#f1f1ed;--acc:#2b59c3;--accs:#e9effc;
--in:#0f7b5f;--ins:#e3f4ee;--out:#8a44b3;--outs:#f3eafa;--warn:#b5651d;--warns:#fcf0e3;--ok:#2e7d32;--oks:#e6f4e7;--test:#7b3fb8;--tests:#f1e8fb;--hl:#fff3bf}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);display:flex;flex-direction:column}
.mono,code,pre,.m,.fld{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
header{padding:14px 22px 10px;background:var(--panel);border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0 0 3px}
.stats{color:var(--muted);font-size:12.5px}.stats b{color:var(--ink)}
nav{display:flex;gap:2px;padding:0 22px;background:var(--panel);border-bottom:1px solid var(--line)}
nav button{border:0;background:none;padding:9px 14px;font:inherit;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent}
nav button.on{color:var(--ink);border-bottom-color:var(--acc);font-weight:600}
main{flex:1;display:grid;grid-template-columns:minmax(0,1fr) 430px;gap:14px;padding:14px 22px;min-height:0}
#left{min-height:0;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
#panel{min-height:0;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.tab{display:none}.tab.on{display:block}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px;position:sticky;top:-12px;background:var(--panel);padding:6px 0;z-index:2}
.toolbar input[type=text],.toolbar input:not([type]){flex:1;min-width:220px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;font:inherit}
.toolbar button,.toolbar select,.btn{padding:5px 10px;border:1px solid var(--line);background:#fff;border-radius:6px;font:inherit;cursor:pointer;font-size:13px}
.toolbar label{font-size:12.5px;color:var(--muted);display:flex;gap:4px;align-items:center}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{border:0;border-radius:0;padding:5px 12px}
.seg button.on{background:var(--accs);color:var(--acc);font-weight:600}
details{margin-left:16px}
#tree>details{margin-left:0}
summary{cursor:pointer;list-style:none;padding:2px 6px;border-radius:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸";display:inline-block;width:13px;color:var(--muted);font-size:11px}
details[open]>summary::before{content:"▾"}
summary:hover,.m:hover,.fld:hover,.row:hover{background:var(--soft)}
.cnt{color:var(--muted);font-size:11.5px;margin-left:8px}
.chip{display:inline-block;font-size:10.5px;line-height:15px;padding:0 5px;border-radius:3px;border:1px solid var(--line);color:var(--muted);margin-left:5px;vertical-align:1px;font-family:system-ui,sans-serif}
.chip.test{background:var(--tests);border-color:#dcc7f3;color:var(--test)}
.chip.ok{background:var(--oks);border-color:#c5e3c7;color:var(--ok)}
.chip.warn{background:var(--warns);border-color:#f0d3b1;color:var(--warn)}
.chip.anno{color:#7a6a20;border-color:#ece2b5;background:#fbf7e6}
.id{display:inline-block;font:700 9.5px/14px system-ui,sans-serif;padding:0 5px;border-radius:3px;letter-spacing:.2px;white-space:nowrap;border:1px solid transparent}
.id-fn{background:#e9effc;color:#2b59c3;border-color:#c9d6f5}
.id-ctor{background:#e3f4f4;color:#0f6f78;border-color:#bfe2e4}
.id-init{background:#eeeeea;color:#6b6b70;border-color:#deded9}
.id-const{background:#fdecea;color:#b3261e;border-color:#f5c9c4}
.id-static{background:#fff3d1;color:#8a6100;border-color:#f0dc9c}
.id-enum{background:#fdeedd;color:#b35c00;border-color:#f3d2ad}
.id-field{background:#f1f1ed;color:#5f6b7a;border-color:#e0e0db}
.id-type{background:#e9effc;color:#2b59c3;border-color:#c9d6f5}
.id-abstract{background:#f3eafa;color:#8a44b3;border-color:#e2cff1}
.dt-primitive{color:#b35c00}
.dt-value{color:#0f7b5f}
.dt-collection{color:#8a44b3}
.dt-project{color:#2b59c3;font-weight:600}
.dt-enum{color:#c26a00;font-weight:600}
.dt-generic{color:#6b6b70;font-style:italic}
.dt-library{color:#1f4e8c}
.legend-row{display:flex;flex-wrap:wrap;gap:4px 6px;align-items:center;font-size:11.5px;color:var(--muted);margin:0 0 10px}
.legend-row .sp{width:10px}
.cc{color:var(--muted);font:10.5px system-ui,sans-serif;white-space:nowrap}
.cc.zero{color:#c0392b}
.steps{display:inline-block;min-width:24px;text-align:center;font-weight:700;font-size:12px;background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:0 6px}
tr.entry .steps{background:#e9effc;border-color:#c9d6f5;color:#2b59c3}
.chip.ep{background:#e9effc;border-color:#c9d6f5;color:#2b59c3}
.only-entry tr.mid{display:none}
.path{margin-top:1px}
#panel tr.call-x td,#panel tr.call-u td{color:var(--muted)}
.only-repo tr.call-x,.only-repo tr.call-u{display:none}
.callsig{color:var(--muted);font-size:11px}
.legend-row .ty{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.ic{display:inline-block;width:16px;height:16px;line-height:16px;text-align:center;border-radius:50%;font:700 9.5px system-ui,sans-serif;color:#fff;margin-right:5px;vertical-align:1px}
.ic.dir{background:none;color:#a08a3c;font-size:12px;border-radius:2px}
.ic.class{background:#2b59c3}.ic.interface{background:#2e8b57}.ic.enum{background:#c77d1a}.ic.record{background:#13838f}
.ic.annotation{background:#8a8a8a}.ic.anonymous,.ic.enumc{background:#9aa3b5}
.ic.file{background:none;color:var(--muted);font-size:11px}
.m,.fld{margin-left:30px;padding:2px 6px;border-radius:4px;cursor:pointer;font-size:12.3px;display:flex;flex-wrap:wrap;column-gap:8px;row-gap:1px;align-items:baseline}
.m .vis,.fld .vis{width:10px;text-align:center;font-weight:700}
.vis.pub{color:#2e7d32}.vis.prot{color:#b5841d}.vis.pkg{color:#5f6b7a}.vis.priv{color:#c0392b}
.m .nm{font-weight:600}
.m .k{font:600 9.5px system-ui,sans-serif;color:var(--muted)}
.io{display:inline-flex;flex-wrap:wrap;gap:4px;align-items:baseline;word-break:break-word}
.tag{font:700 9px system-ui,sans-serif;padding:1px 4px;border-radius:3px;color:#fff;letter-spacing:.3px}
.tag.in{background:var(--in)}.tag.out{background:var(--out)}
.ty{color:var(--acc)}
.pn{color:var(--muted)}
.none{color:var(--muted);font-style:italic}
.ln{color:var(--muted);font-size:10.5px;margin-left:auto;padding-left:8px}
.sep{color:var(--muted)}
.fields>summary{color:var(--muted);font-size:12px}
.hide-priv .priv-row{display:none!important}
.hide-fields .fields{display:none!important}
.note{margin-left:30px;color:var(--muted);font-size:12px;font-style:italic;padding:2px 6px}
.flash{animation:fl 1.6s}
@keyframes fl{0%,45%{background:var(--hl)}100%{background:transparent}}
.sel{background:var(--accs)!important}
#results{border:1px solid var(--line);border-radius:6px;margin-bottom:10px;max-height:340px;overflow:auto}
.row{padding:4px 10px;cursor:pointer;font-size:12.5px;border-bottom:1px solid var(--soft);display:flex;gap:8px;align-items:baseline}
.row .p{color:var(--muted);font-size:11.5px;margin-left:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:45%}
.hide{display:none!important}
#panel h3{margin:0 0 3px;font-size:15.5px;word-break:break-word}
#panel h4{margin:16px 0 6px;font-size:11.5px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}
#panel table,.grid{width:100%;border-collapse:collapse;font-size:12.5px}
#panel td,#panel th,.grid td,.grid th{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:top}
#panel th,.grid th{font-weight:600;color:var(--muted);font-size:11.5px;position:sticky;top:0;background:var(--panel)}
#panel ul{margin:0;padding-left:18px;font-size:12.5px}#panel li{margin:3px 0}
pre.sig{white-space:pre-wrap;word-break:break-word;background:var(--soft);padding:8px 10px;border-radius:6px;font-size:12px;margin:8px 0}
.muted{color:var(--muted);font-size:12.5px}
a.go{color:var(--acc);cursor:pointer;text-decoration:none}a.go:hover{text-decoration:underline}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:6px 0 16px}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:#fff}
.card .v{font-size:22px;font-weight:650}.card .l{color:var(--muted);font-size:12px}
h2{font-size:15px;margin:18px 0 8px}
.barwrap{display:flex;align-items:center;gap:8px}
.bar{height:8px;border-radius:4px;background:var(--acc);min-width:2px}
.bar.g{background:var(--ok)}.bar.o{background:var(--warn)}
.bg{flex:1;background:var(--soft);border-radius:4px;height:8px;overflow:hidden;max-width:220px}
.legend{font-size:12.5px;color:var(--muted);background:var(--soft);border-radius:6px;padding:8px 12px;margin:8px 0}
.sev{font-weight:700;width:14px;display:inline-block}.sev.warn{color:var(--warn)}.sev.info{color:var(--acc)}
.more{margin:8px 0}
</style>
</head>
<body>
<header>
  <h1>Code Structure — <span id="pname"></span></h1>
  <div class="stats" id="hstats"></div>
</header>
<nav id="tabs">
  <button data-tab="overview" class="on">Overview</button>
  <button data-tab="structure">Structure</button>
  <button data-tab="odd">Odd ones out</button>
</nav>
<main>
  <section id="left">
    <div class="tab on" id="t-overview"></div>
    <div class="tab" id="t-structure">
      <div class="toolbar">
        <div class="seg" id="viewseg"><button data-view="folder" class="on">Folders</button><button data-view="package">Packages</button></div>
        <input id="q" placeholder="Search class, method, file, or object type (min 2 chars)…" autocomplete="off">
        <label><input type="checkbox" id="optPriv" checked> private</label>
        <label><input type="checkbox" id="optFields" checked> fields</label>
        <button id="expandFn" title="Open folders, files and classes so every function is visible. Field lists, test files and test classes stay closed.">Expand functions</button>
        <button id="expand" title="Open everything, including field lists">Expand all</button>
        <button id="collapse">Collapse all</button>
      </div>
      <div class="legend-row" id="idlegend">
        <b>Identifiers:</b>
        <span class="id id-fn">ƒ</span>function
        <span class="id id-ctor">ctor</span>constructor
        <span class="id id-const">const</span>constant
        <span class="id id-static">static</span>static
        <span class="id id-enum">enum</span>enum
        <span class="id id-type">class</span>data type
        <span class="id id-field">var</span>field
        <span class="cc">→3 ←2</span>calls into repo / called by
        <span class="sp"></span><b>Data types:</b>
        <span class="ty dt-primitive">int</span>primitive
        <span class="ty dt-value">String</span>value
        <span class="ty dt-collection">List</span>collection
        <span class="ty dt-project">Order</span>defined in repo
        <span class="ty dt-enum">Status</span>enum in repo
        <span class="ty dt-library">Pool</span>library/JDK
        <span class="ty dt-generic">T</span>generic
      </div>
      <div id="results" class="hide"></div>
      <div id="tree"></div>
    </div>
    <div class="tab" id="t-odd"></div>
  </section>
  <aside id="panel"></aside>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
'use strict';
const D = JSON.parse(document.getElementById('data').textContent);
const M = D.meta;
const $ = (s, r) => (r || document).querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = (a, b) => b ? Math.round(100 * a / b) : 0;
const fmtN = n => (n || 0).toLocaleString();

/* ---------------- index ---------------- */
const TY = {}, ME = {}, FI = {};
function walkT(ts, f, par) {
  for (const t of ts) {
    t._f = f; t._p = par; TY[t.id] = t;
    for (const m of (t.fn || [])) { m._t = t; ME[m.id] = m; }
    if (t.nt) walkT(t.nt, f, t);
  }
}
for (const f of D.files) { FI[f.p] = f; if (f.types) walkT(f.types, f, null); }
function fileMethods(f) {
  if (f._nm != null) return f._nm;
  let n = 0;
  const rec = ts => { for (const t of ts) { n += (t.fn || []).length; if (t.nt) rec(t.nt); } };
  if (f.types) rec(f.types);
  if (f.sym) n += f.sym.filter(s => s.k === 'function' || s.k === 'method').length;
  return (f._nm = n);
}
const fname = p => p.slice(p.lastIndexOf('/') + 1);

/* ---------------- small renderers ---------------- */
const KIND_IC = {class:'C', interface:'I', enum:'E', record:'R', annotation:'@', anonymous:'A', 'enum constant':'e'};
function icon(k) { return `<span class="ic ${k === 'enum constant' ? 'enumc' : esc(k)}">${KIND_IC[k] || '?'}</span>`; }
function vis(mods, t) {
  mods = mods || [];
  if (mods.includes('public')) return ['pub', '+'];
  if (mods.includes('protected')) return ['prot', '#'];
  if (mods.includes('private')) return ['priv', '−'];
  if (t && (t.k === 'interface' || t.k === 'annotation')) return ['pub', '+'];
  return ['pkg', '~'];
}
const PRIM = new Set(['int', 'long', 'short', 'byte', 'char', 'boolean', 'float', 'double', 'void', 'var',
  'string', 'number', 'bigint', 'symbol', 'any', 'unknown', 'never', 'undefined', 'null', 'object']);
const VALUE = new Set(['String', 'Integer', 'Long', 'Short', 'Byte', 'Character', 'Boolean', 'Float', 'Double', 'Object',
  'Number', 'BigDecimal', 'BigInteger', 'CharSequence', 'UUID', 'Date', 'LocalDate', 'LocalDateTime', 'LocalTime',
  'Instant', 'ZonedDateTime', 'OffsetDateTime', 'Duration', 'Period', 'Class', 'Void', 'StringBuilder']);
const COLL = new Set(['List', 'ArrayList', 'LinkedList', 'Set', 'HashSet', 'TreeSet', 'LinkedHashSet', 'SortedSet',
  'Map', 'HashMap', 'TreeMap', 'LinkedHashMap', 'SortedMap', 'ConcurrentHashMap', 'ConcurrentMap', 'Collection',
  'Iterable', 'Iterator', 'Queue', 'Deque', 'ArrayDeque', 'Stream', 'Optional', 'Vector', 'Hashtable', 'Enumeration',
  'Page', 'Slice', 'Flux', 'Mono', 'CompletableFuture', 'Future', 'Array', 'Record', 'Promise', 'ReadonlyArray', 'Multimap']);
const DT_LABEL = {primitive: 'primitive', value: 'built-in value type', collection: 'collection / container',
  project: 'type defined in this repo', enum: 'enum defined in this repo', generic: 'generic type parameter',
  library: 'library / JDK type'};
let PROJ = null;
function projTypes() {
  if (PROJ) return PROJ;
  PROJ = new Map();
  for (const id in TY) { const t = TY[id]; if (t.k !== 'anonymous' && t.k !== 'enum constant') PROJ.set(t.n, t.k); }
  for (const f of D.files) for (const s of (f.sym || [])) if (['class', 'interface', 'type', 'enum'].includes(s.k)) PROJ.set(s.n, s.k);
  return PROJ;
}
function dtype(s) {
  let b = String(s || '').replace(/@[\w.]+(\([^)]*\))?\s*/g, '').trim();
  const arr = /\[\]|\.\.\.$/.test(b.replace(/<.*>/, ''));
  b = b.replace(/<.*$/, '').replace(/\[\]|\.\.\./g, '').trim();
  const simple = b.split('.').pop();
  let c;
  if (PRIM.has(simple)) c = 'primitive';
  else if (VALUE.has(simple)) c = 'value';
  else if (COLL.has(simple)) c = 'collection';
  else {
    const k = projTypes().get(simple);
    if (k) c = k === 'enum' ? 'enum' : 'project';
    else if (/^[A-Z][0-9]?$/.test(simple)) c = 'generic';
    else c = 'library';
  }
  return [c, arr];
}
const ty = s => { const d = dtype(s); return `<span class="ty dt-${d[0]}" title="data type: ${DT_LABEL[d[0]]}${d[1] ? ' (array)' : ''}">${esc(s)}</span>`; };
const KIND_LABEL = {class: 'class', interface: 'interface', enum: 'enum', record: 'record', annotation: '@annotation',
  anonymous: 'anonymous class', 'enum constant': 'enum value body'};
function typeIds(t) {
  const mods = t.m || [];
  let h = `<span class="id ${t.k === 'enum' || t.k === 'enum constant' ? 'id-enum' : 'id-type'}" title="data type: ${esc(t.k)}">${KIND_LABEL[t.k] || esc(t.k)}</span>`;
  if (mods.includes('static')) h += '<span class="id id-static" title="static nested type">static</span>';
  if (mods.includes('abstract')) h += '<span class="id id-abstract">abstract</span>';
  if (mods.includes('sealed')) h += '<span class="id id-abstract">sealed</span>';
  return h;
}
function methodIds(m) {
  const mods = m.m || [];
  let h;
  if (m.k === 'ctor') h = '<span class="id id-ctor" title="constructor">ctor</span>';
  else if (m.k === 'init') h = '<span class="id id-init" title="initializer block">init</span>';
  else h = '<span class="id id-fn" title="function / method">ƒ</span>';
  if (mods.includes('static')) h += '<span class="id id-static" title="static">static</span>';
  if (mods.includes('abstract')) h += '<span class="id id-abstract">abstract</span>';
  if (mods.includes('default')) h += '<span class="id id-abstract">default</span>';
  return h;
}
function fieldIds(fl, t) {
  const mods = fl.m || [];
  const iface = t && (t.k === 'interface' || t.k === 'annotation');
  const st = mods.includes('static') || iface, fin = mods.includes('final') || iface;
  if (st && fin) return '<span class="id id-const" title="constant (static final)">const</span>';
  let h = st ? '<span class="id id-static" title="static field">static</span>' : '';
  h += fin ? '<span class="id id-field" title="final field">final</span>' : '<span class="id id-field" title="field / variable">var</span>';
  return h;
}
function symIds(s) {
  if (s.k === 'function' || s.k === 'method') {
    let h = '<span class="id id-fn" title="function">ƒ</span>';
    if (s.decl === 'const') h += '<span class="id id-const" title="declared with const">const</span>';
    if (s.static) h += '<span class="id id-static">static</span>';
    return h;
  }
  if (s.k === 'const') return '<span class="id id-const" title="constant">const</span>';
  if (s.k === 'enum') return '<span class="id id-enum">enum</span>';
  return `<span class="id id-type" title="data type">${esc(s.k)}</span>`;
}
function paramsHTML(m) {
  const ps = m.p || [];
  if (!ps.length) return '<span class="none">none</span>';
  return ps.map(p => `${ty(p[1])} <span class="pn">${esc(p[0])}</span>`).join('<span class="sep">, </span>');
}
function outHTML(m) {
  if (m.k === 'ctor') return `<span class="ty">new ${esc(m._t ? m._t.n : m.n)}</span>`;
  if (m.k === 'init') return '<span class="none">—</span>';
  return ty(m.r || 'void');
}
function testBadge(m) {
  if (m.test) return `<span class="chip test">TEST${m.tt ? ' → ' + m.tt.length : ''}</span>`;
  if (m.tb && m.tb.length) {
    const strong = m.tb.some(x => x[1] === 'strong');
    return `<span class="chip ok" title="${strong ? 'Called directly in a matching test class (name + call)' : 'Called directly from a test (call only)'}">✓ tested ×${m.tb.length}${strong ? '' : ' (call)'}</span>`;
  }
  return '';
}
function annoChips(a, max) {
  if (!a || !a.length) return '';
  const shown = a.slice(0, max || 3).map(x => `<span class="chip anno">@${esc(x)}</span>`).join('');
  return shown + (a.length > (max || 3) ? `<span class="chip anno">+${a.length - (max || 3)}</span>` : '');
}

/* ---------------- lazy tree primitives ---------------- */
function mk(key, summaryHTML, fill, cls) {
  const d = document.createElement('details');
  d.dataset.key = key;
  if (cls) d.className = cls;
  d.innerHTML = '<summary>' + summaryHTML + '</summary>';
  d._fill = fill;
  d.addEventListener('toggle', () => { if (d.open) ensure(d); });
  return d;
}
function ensure(d) { if (d._fill) { const f = d._fill; d._fill = null; f(d); } }
function openD(d) { ensure(d); d.open = true; }

/* ---------------- structure: folder view ---------------- */
function buildFolderTree() {
  const root = {name: M.project, path: '', dirs: new Map(), files: []};
  for (const f of D.files) {
    const parts = f.p.split('/');
    let n = root, acc = '';
    for (let i = 0; i < parts.length - 1; i++) {
      acc = acc ? acc + '/' + parts[i] : parts[i];
      let c = n.dirs.get(parts[i]);
      if (!c) { c = {name: parts[i], path: acc, dirs: new Map(), files: []}; n.dirs.set(parts[i], c); }
      n = c;
    }
    n.files.push(f);
  }
  (function cnt(n) {
    n.nf = n.files.length; n.nm = 0; n.nj = 0;
    for (const f of n.files) { n.nm += fileMethods(f); if (f.lang === 'java') n.nj++; }
    for (const c of n.dirs.values()) { cnt(c); n.nf += c.nf; n.nm += c.nm; n.nj += c.nj; }
  })(root);
  (function comp(n) {
    for (const [k, c] of [...n.dirs]) {
      let x = c;
      while (x.files.length === 0 && x.dirs.size === 1) {
        const y = [...x.dirs.values()][0]; y.name = x.name + '/' + y.name; x = y;
      }
      if (x !== c) { n.dirs.delete(k); n.dirs.set(x.name, x); }
      comp(x);
    }
  })(root);
  return root;
}
let FOLDER_ROOT = null;
function dirEl(n, isRoot) {
  const d = mk('d:' + n.path,
    `<span class="ic dir">▣</span><b>${esc(n.name)}/</b><span class="cnt">${fmtN(n.nf)} files${n.nj ? ' · ' + fmtN(n.nj) + ' java' : ''}${n.nm ? ' · ' + fmtN(n.nm) + ' methods' : ''}</span>`,
    el => {
      [...n.dirs.values()].sort((a, b) => a.name.localeCompare(b.name)).forEach(c => el.appendChild(dirEl(c)));
      n.files.slice().sort((a, b) => a.p.localeCompare(b.p)).forEach(f => el.appendChild(fileEl(f)));
    });
  d.dataset.path = n.path;
  if (isRoot) openD(d);
  return d;
}
function fileEl(f) {
  const nt = f.types ? f.types.length : 0, nm = fileMethods(f);
  const meta = [];
  meta.push(f.lang || '(no extension)');
  if (nt) meta.push(nt + (nt > 1 ? ' types' : ' type'));
  if (nm) meta.push(nm + (nm > 1 ? ' methods' : ' method'));
  if (f.ln) meta.push(fmtN(f.ln) + ' lines');
  const d = mk('f:' + f.p,
    `<span class="ic file">▤</span><span class="fn">${esc(fname(f.p))}</span>${f.test ? '<span class="chip test">test</span>' : ''}${f.err || f.skipped ? '<span class="chip warn">⚠</span>' : ''}<span class="cnt">${esc(meta.join(' · '))}</span>`,
    el => fillFile(el, f), f.test ? 'file testfile' : 'file');
  d.dataset.path = f.p;
  return d;
}
function fillFile(el, f) {
  if (f.types && f.types.length) { f.types.forEach(t => el.appendChild(typeEl(t))); return; }
  if (f.sym && f.sym.length) { f.sym.forEach(s => el.appendChild(symRow(s))); return; }
  const n = document.createElement('div'); n.className = 'note';
  n.textContent = f.err ? f.err : f.skipped ? f.skipped : f.binary ? 'Binary file (' + fmtN(f.s) + ' bytes)' : 'Read: ' + fmtN(f.ln || 0) + ' lines, ' + fmtN(f.s || 0) + ' bytes (no code outline for .' + (f.x || '?') + ' files)';
  el.appendChild(n);
}
function symRow(s) {
  const r = document.createElement('div'); r.className = 'm';
  const io = (s.k === 'function' || s.k === 'method')
    ? `<span class="io"><span class="tag in">IN</span>${s.p ? esc(s.p) : '<span class="none">none</span>'}</span><span class="io"><span class="tag out">OUT</span>${s.r ? ty(s.r) : '<span class="none">not declared</span>'}</span>`
    : (s.ext ? `<span class="muted">extends ${esc(s.ext)}</span>` : '');
  const typ = s.k === 'const' && s.r ? ty(s.r) : '';
  r.innerHTML = `${symIds(s)}<span class="nm">${esc(s.n)}</span>${typ}${io}<span class="ln">L${s.l}</span>`;
  return r;
}
function typeEl(t) {
  const nf = (t.f || []).length, nm = (t.fn || []).length;
  const heads = [];
  if (t.ext && t.ext.length && t.k !== 'anonymous') heads.push('extends ' + t.ext.join(', '));
  if (t.impl && t.impl.length) heads.push('implements ' + t.impl.join(', '));
  let badge = '';
  if (t.test) badge = '<span class="chip test">test class</span>';
  else if (t.tc && t.tc.length) badge = '<span class="chip ok">✓ has test class</span>';
  const v = vis(t.m, t._p);
  const d = mk('t:' + t.id,
    `${icon(t.k)}${typeIds(t)} <b>${esc(t.n)}</b>${t.tp ? '<span class="ty">' + esc(t.tp) + '</span>' : ''}${heads.length ? ' <span class="muted mono" style="font-size:11.5px">' + esc(heads.join(' ')) + '</span>' : ''}${annoChips(t.a)}${badge}<span class="cnt">L${t.l}${nf ? ' · ' + nf + ' fields' : ''}${nm ? ' · ' + nm + ' methods' : ''}</span>`,
    el => {
      if (t.c && t.c.length) {
        t.c.forEach(name => {
          const c = document.createElement('div'); c.className = 'fld enumval';
          c.innerHTML = `<span class="vis pub">+</span><span class="id id-const" title="constant">const</span><span class="id id-enum">enum value</span><span class="ty dt-enum">${esc(t.n)}</span> <span>${esc(name)}</span>`;
          el.appendChild(c);
        });
      }
      if (nf) {
        const g = mk('F:' + t.id, `Fields (${nf})`, ge => t.f.forEach(fl => ge.appendChild(fieldRow(fl, t))), 'fields');
        el.appendChild(g);
        if (nf <= 12) openD(g);
      }
      (t.fn || []).forEach(m => el.appendChild(methodRow(m)));
      (t.nt || []).forEach(c => el.appendChild(typeEl(c)));
    });
  if (v[0] === 'priv') d.classList.add('priv-row');
  if (t.test) d.classList.add('testclass');
  d.querySelector('summary').dataset.type = t.id;
  return d;
}
function fieldRow(fl, t) {
  const r = document.createElement('div');
  const v = vis(fl.m, t);
  r.className = 'fld' + (v[0] === 'priv' ? ' priv-row' : '');
  r.innerHTML = `<span class="vis ${v[0]}">${v[1]}</span>${fieldIds(fl, t)}${ty(fl.t)} <span>${esc(fl.n)}</span>${annoChips(fl.a, 2)}${fl.mock ? '<span class="chip">mock</span>' : ''}${fl.comp ? '<span class="chip">component</span>' : ''}<span class="ln">L${fl.l}</span>`;
  return r;
}
/* ---------- all callers (direct + indirect) with number of steps, and entry points ---------- */
const MAX_STEPS = 15, MAX_CALLERS = 3000;
const ENDPOINT_ANNOS = new Set(['GetMapping', 'PostMapping', 'PutMapping', 'DeleteMapping', 'PatchMapping', 'RequestMapping',
  'GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS', 'Path', 'WebMethod', 'MessageMapping', 'SubscribeMapping', 'QueryMapping', 'MutationMapping']);
const FRAMEWORK_ANNOS = new Set(['Scheduled', 'EventListener', 'TransactionalEventListener', 'KafkaListener', 'JmsListener',
  'RabbitListener', 'SqsListener', 'StreamListener', 'PostConstruct', 'PreDestroy', 'Bean', 'Observes', 'Schedule', 'Startup']);
function callersOf(id) {
  if (id[0] === 'T') {          // field initializers run when an object of the type is created
    const t = TY[id]; const out = new Set();
    (t ? t.fn || [] : []).forEach(c => { if (c.k === 'ctor') (c.cb || []).forEach(x => out.add(x)); });
    return [...out];
  }
  const m = ME[id];
  return m ? (m.cb || []).filter(x => x !== id) : [];
}
function nodeName(id) {
  if (id[0] === 'T') return TY[id] ? TY[id].n + ' (field initializer)' : id;
  const m = ME[id]; if (!m) return id;
  const overloaded = (m._t.fn || []).filter(x => x.n === m.n).length > 1;
  const sig = overloaded ? '(' + (m.p || []).map(p => _simpleType(p[1])).join(', ') + ')' : '';
  return m._t.n + '.' + m.n + sig;
}
function _simpleType(t) {
  return String(t || '').replace(/@[\w.]+(\([^)]*\))?\s*/g, '').replace(/<.*>/, '').split('.').pop();
}
function nodeLink(id) {
  return id[0] === 'T' ? `<a class="go" data-t="${id}">${esc(nodeName(id))}</a>` : `<a class="go" data-m="${id}">${esc(nodeName(id))}</a>`;
}
function entryKind(id) {
  if (id[0] === 'T') return 'field initializer';
  const m = ME[id]; if (!m) return '';
  const a = m.a || [];
  if (m.test) return 'test';
  const ep = a.find(x => ENDPOINT_ANNOS.has(x)); if (ep) return 'web endpoint @' + ep;
  const fw = a.find(x => FRAMEWORK_ANNOS.has(x)); if (fw) return 'framework @' + fw;
  if (m.n === 'main' && ((m.m || []).includes('static') || m._t.implicit)) return 'main()';
  if (m.k === 'init') return 'initializer';
  if (a.includes('Override')) return 'override (may be called by a library)';
  return 'no caller in this repo';
}
function allCallers(start) {
  const dist = new Map([[start, 0]]), via = new Map(), queue = [start];
  let capped = false;
  for (let qi = 0; qi < queue.length && !capped; qi++) {
    const cur = queue[qi], d = dist.get(cur);
    if (d >= MAX_STEPS) { if (callersOf(cur).some(c => !dist.has(c))) capped = true; continue; }
    for (const c of callersOf(cur)) {
      if (dist.has(c)) continue;
      dist.set(c, d + 1); via.set(c, cur); queue.push(c);
      if (dist.size > MAX_CALLERS) { capped = true; break; }
    }
  }
  dist.delete(start);
  const rows = [...dist.entries()].map(([id, d]) => ({id, d, entry: callersOf(id).length === 0}));
  rows.sort((a, b) => a.d - b.d || (b.entry - a.entry) || nodeName(a.id).localeCompare(nodeName(b.id)));
  const path = id => {            // caller ... up to (not including) the selected method
    const p = [id];
    while (via.has(p[p.length - 1])) p.push(via.get(p[p.length - 1]));
    return p.slice(0, -1);
  };
  return {rows, capped, path};
}
function allCallersSection(m) {
  const res = allCallers(m.id), rows = res.rows, entries = rows.filter(r => r.entry);
  const maxD = rows.length ? rows[rows.length - 1].d : 0;
  let h = `<h4>All callers — direct &amp; indirect (${rows.length}${res.capped ? '+' : ''}) <span style="text-transform:none;letter-spacing:0;font-weight:400">— ${entries.length} entry point${entries.length === 1 ? '' : 's'}${rows.length ? ' · up to ' + maxD + ' step' + (maxD === 1 ? '' : 's') + ' away' : ''}</span></h4>`;
  if (!rows.length) {
    return h + `<p class="muted">Nothing in this repo reaches this ${m.k === 'ctor' ? 'constructor' : 'method'}${m.k === 'method' ? ' — it is itself an entry point (' + esc(entryKind(m.id)) + ')' : ''}.</p>`;
  }
  const rowHTML = r => {
    const p = res.path(r.id).map(nodeName);
    return `<tr class="${r.entry ? 'entry' : 'mid'}"><td style="width:52px"><span class="steps" title="${r.d} step${r.d === 1 ? '' : 's'} before the call">${r.d}</span></td><td>${nodeLink(r.id)}`
      + (r.id[0] === 'M' && ME[r.id].test ? ' <span class="chip test">test</span>' : '')
      + (r.entry ? ` <span class="chip ep" title="Nothing in this repo calls this — the chain starts here">entry point · ${esc(entryKind(r.id))}</span>` : '')
      + `<div class="callsig path mono" title="call path">${p.map(esc).join(' → ')} → <b>${esc(nodeName(m.id))}</b></div></td></tr>`;
  };
  const LIMIT = 300;
  h += `<label class="muted" style="font-size:12px;display:flex;gap:5px;align-items:center;margin-bottom:4px"><input type="checkbox" class="onlyEntry"> only entry points</label>`;
  h += `<table class="callerTable"><tr><th>Steps</th><th>Caller (and call path)</th></tr>${rows.slice(0, LIMIT).map(rowHTML).join('')}</table>`;
  if (rows.length > LIMIT) h += `<div class="more"><button class="btn moreCallers">Show all ${fmtN(rows.length)} callers</button></div>`;
  if (res.capped) h += `<p class="muted">Stopped after ${MAX_STEPS} steps or ${fmtN(MAX_CALLERS)} callers.</p>`;
  h += `<p class="muted" style="font-size:11.5px">Steps = how many calls happen before this one. 1 = calls it directly. Calls through an interface count every implementation, so some paths are “possible” rather than certain.</p>`;
  ALL_CALLER_ROWS = {rows, rowHTML};
  return h;
}
let ALL_CALLER_ROWS = null;
function wireCallerToggles() {
  P.querySelectorAll('.onlyEntry').forEach(cb => cb.onchange = () => P.querySelector('.callerTable').classList.toggle('only-entry', cb.checked));
  const more = P.querySelector('.moreCallers');
  if (more) more.onclick = () => {
    const tb = P.querySelector('.callerTable');
    tb.insertAdjacentHTML('beforeend', ALL_CALLER_ROWS.rows.slice(300).map(ALL_CALLER_ROWS.rowHTML).join(''));
    more.parentElement.remove();
  };
}
function isUnusedPrivate(m) {
  return m.k === 'method' && (m.m || []).includes('private') && !m.test && !(m.a && m.a.length)
    && !(m.cb || []).some(id => id !== m.id);
}
function callChip(m) {
  const nc = (m.cl || []).filter(c => c[2] === 'i').length, nb = (m.cb || []).length;
  const unused = isUnusedPrivate(m);
  return `<span class="cc${unused ? ' zero' : ''}" title="${nc} call(s) into this repo · called by ${nb}${unused ? ' — private and never called' : ''}">→${nc} ←${nb}</span>`;
}
function callRow(c) {
  const ln = c[0], label = c[1], k = c[2], tg = c[3] || [], im = c[4] || [];
  let who;
  if (k === 'i') {
    who = tg.length ? tg.map(id => {
      if (id[0] === 'T') return `<a class="go" data-t="${id}">${esc(label)}</a> <span class="callsig">(default constructor)</span>`;
      const x = ME[id];
      const sig = x ? ' <span class="callsig mono">(' + (x.p || []).map(p => esc(p[1])).join(', ') + ') → ' + esc(x.k === 'ctor' ? 'new' : (x.r || 'void')) + '</span>' : '';
      return `<a class="go" data-m="${id}">${esc(label)}</a>${sig}`;
    }).join('<br>') : esc(label);
    if (im.length) who += '<div class="callsig">implemented by: ' + im.map(mLink).join(', ') + '</div>';
  } else if (k === 'x') {
    const builtin = /\((record accessor|enum built-in)\)|super\(…\)/.test(label);
    who = `<span class="mono">${esc(label)}</span> <span class="chip" title="${builtin ? 'generated by Java for this type' : 'method of a library or JDK type'}">${builtin ? 'built-in' : 'library/JDK'}</span>`;
  } else {
    who = `<span class="mono">${esc(label)}</span> <span class="chip warn" title="The type of the object this is called on could not be worked out">? unresolved</span>`;
  }
  return `<tr class="call-${k}"><td class="muted" style="width:46px">L${ln}</td><td>${who}</td></tr>`;
}
function callsSection(cl, title) {
  const ni = cl.filter(c => c[2] === 'i').length, nx = cl.filter(c => c[2] === 'x').length, nu = cl.filter(c => c[2] === 'u').length;
  return `<h4>${title} (${cl.length}) <span style="text-transform:none;letter-spacing:0;font-weight:400">— ${ni} in this repo · ${nx} library/JDK/built-in · ${nu} unresolved</span></h4>` +
    (cl.length ? `<label class="muted" style="font-size:12px;display:flex;gap:5px;align-items:center;margin-bottom:4px"><input type="checkbox" class="onlyRepo"> only calls into this repo</label><table class="callTable"><tr><th>Line</th><th>Calls</th></tr>${cl.map(callRow).join('')}</table>` : '<p class="muted">No calls</p>');
}
function wireCallToggles() {
  P.querySelectorAll('.onlyRepo').forEach(cb => cb.onchange = () => cb.parentElement.nextElementSibling.classList.toggle('only-repo', cb.checked));
}
function methodRow(m) {
  const r = document.createElement('div');
  const v = vis(m.m, m._t);
  r.className = 'm' + (v[0] === 'priv' ? ' priv-row' : '');
  r.dataset.m = m.id;
  const annos = (m.a || []).filter(a => a !== 'Override');
  r.innerHTML = `<span class="vis ${v[0]}">${v[1]}</span>${methodIds(m)}<span class="nm">${esc(m.n)}</span>`
    + (m.k !== 'init' ? `<span class="io"><span class="tag in">IN</span>${paramsHTML(m)}</span><span class="io"><span class="tag out">OUT</span>${outHTML(m)}</span>` : '')
    + (m.th ? `<span class="muted">throws ${esc(m.th.join(', '))}</span>` : '')
    + annoChips(annos, 2) + testBadge(m) + callChip(m) + `<span class="ln">L${m.l}</span>`;
  return r;
}

/* ---------------- structure: package view ---------------- */
function packageRoot(el) {
  const pk = new Map();
  for (const f of D.files) {
    if (f.lang !== 'java' || !f.types) continue;
    const k = f.pkg || '(default package)';
    if (!pk.has(k)) pk.set(k, []);
    pk.get(k).push(f);
  }
  [...pk.keys()].sort().forEach(k => {
    const fl = pk.get(k);
    const nt = fl.reduce((a, f) => a + f.types.length, 0), nm = fl.reduce((a, f) => a + fileMethods(f), 0);
    const tests = fl.filter(f => f.test).length;
    const d = mk('p:' + k, `<span class="ic dir">▣</span><b>${esc(k)}</b><span class="cnt">${fl.length} files · ${nt} types · ${nm} methods${tests ? ' · ' + tests + ' test files' : ''}</span>`,
      e => fl.slice().sort((a, b) => a.p.localeCompare(b.p)).forEach(f => f.types.forEach(t => e.appendChild(typeEl(t)))));
    el.appendChild(d);
  });
  const nonJava = D.files.filter(f => f.lang !== 'java').length;
  if (nonJava) {
    const n = document.createElement('div'); n.className = 'note';
    n.textContent = nonJava + ' non-Java files are shown in the Folders view.';
    el.appendChild(n);
  }
}
let VIEW = 'folder';
function renderTree() {
  const tree = $('#tree'); tree.innerHTML = '';
  if (VIEW === 'folder') { FOLDER_ROOT = FOLDER_ROOT || buildFolderTree(); tree.appendChild(dirEl(FOLDER_ROOT, true)); }
  else packageRoot(tree);
}

/* ---------------- reveal (open the tree down to an item) ---------------- */
function kids(el) { return [...el.children].filter(c => c.tagName === 'DETAILS'); }
function reveal(fp, typeId, methodId) {
  showTab('structure');
  const tree = $('#tree');
  let el = null;
  if (VIEW === 'folder') {
    el = tree.firstElementChild; if (!el) return;
    openD(el);
    for (let guard = 0; guard < 200; guard++) {
      const ks = kids(el);
      const file = ks.find(c => c.dataset.key === 'f:' + fp);
      if (file) { el = file; break; }
      const dir = ks.find(c => c.dataset.key.startsWith('d:') && (c.dataset.path === '' || fp.startsWith(c.dataset.path + '/')));
      if (!dir) return;
      openD(dir); el = dir;
    }
    if (typeId || methodId) openD(el);
  } else {
    const f = FI[fp];
    if (!f || f.lang !== 'java') { VIEW = 'folder'; syncSeg(); renderTree(); return reveal(fp, typeId, methodId); }
    el = kids(tree).find(c => c.dataset.key === 'p:' + (f.pkg || '(default package)'));
    if (!el) return;
    openD(el);
  }
  let target = el;
  if (typeId) {
    const chain = []; let t = TY[typeId];
    while (t) { chain.unshift(t); t = t._p; }
    for (const t2 of chain) {
      const c = kids(el).find(x => x.dataset.key === 't:' + t2.id);
      if (!c) break;
      openD(c); el = c; target = c;
    }
    if (methodId) {
      const r = el.querySelector(':scope > .m[data-m="' + methodId + '"]');
      if (r) target = r;
    }
  }
  const s = target.tagName === 'DETAILS' ? target.querySelector('summary') : target;
  document.querySelectorAll('.sel').forEach(x => x.classList.remove('sel'));
  s.classList.add('sel');
  s.scrollIntoView({block: 'center'});
  s.classList.remove('flash'); void s.offsetWidth; s.classList.add('flash');
}
const revealType = id => { const t = TY[id]; if (t) reveal(t._f.p, id); };
const revealMethod = id => { const m = ME[id]; if (m) reveal(m._t._f.p, m._t.id, id); };

/* ---------------- detail panel ---------------- */
const P = $('#panel');
const mLink = id => { const m = ME[id]; return m ? `<a class="go" data-m="${id}">${esc(m._t.n + '.' + m.n)}</a>` : esc(id); };
const tLink = id => { const t = TY[id]; return t ? `<a class="go" data-t="${id}">${esc(t.q)}</a>` : esc(id); };
const fLink = p => `<a class="go" data-f="${esc(p)}">${esc(p)}</a>`;
const list = (arr, fmt, empty) => arr && arr.length ? '<ul>' + arr.map(x => '<li>' + fmt(x) + '</li>').join('') + '</ul>' : `<p class="muted">${empty}</p>`;
function sigText(m) {
  const t = m._t;
  const mods = (m.m || []).join(' ');
  const ps = (m.p || []).map(p => (p[2] ? p[2].map(a => '@' + a).join(' ') + ' ' : '') + p[1] + ' ' + p[0]).join(', ');
  if (m.k === 'init') return m.n;
  const head = (mods ? mods + ' ' : '') + (m.tp ? m.tp + ' ' : '') + (m.k === 'ctor' ? t.n : (m.r || 'void') + ' ' + m.n);
  return head + '(' + ps + ')' + (m.th ? ' throws ' + m.th.join(', ') : '');
}
function showMethod(id) {
  const m = ME[id]; if (!m) return;
  const t = m._t, f = t._f, lines = (m.e || m.l) - m.l + 1;
  const rows = (m.p || []).map((p, i) => `<tr><td>${i + 1}</td><td class="mono">${ty(p[1])}</td><td class="mono">${esc(p[0])}</td><td>${p[2] ? p[2].map(a => '@' + esc(a)).join(' ') : ''}</td></tr>`).join('');
  let tests = '';
  if (m.test) {
    tests = `<h4>Tests what (technique 3: calls in this test)</h4>` + list((m.tt || []), x => mLink(x[0]) + ` <span class="chip ${x[1] === 'strong' ? 'ok' : ''}">${x[1]}</span>`, 'No main-code method found among the calls in this test');
  } else if (m.k !== 'init') {
    const cls = t.tc && t.tc.length;
    tests = `<h4>Tested by</h4>` + list((m.tb || []), x => mLink(x[0]) + ` <span class="chip ${x[1] === 'strong' ? 'ok' : ''}">${x[1]}</span>`,
      cls ? 'Its class has a test class (' + t.tc.map(tLink).join(', ') + ') but no test calls this method directly.' : 'No test calls this method directly.');
  }
  P.innerHTML = `<div class="muted">${esc(m.k === 'ctor' ? 'constructor' : m.k === 'init' ? 'initializer' : 'method')} in ${tLink(t.id)}</div>
    <h3>${methodIds(m)} ${esc(t.n)}.${esc(m.n)}</h3>
    <div class="muted">${fLink(f.p)} : L${m.l}${m.e && m.e !== m.l ? '–' + m.e : ''} · ${lines} line${lines > 1 ? 's' : ''}${m.b ? '' : ' · no body'}</div>
    <pre class="sig">${esc(sigText(m))}</pre>
    ${m.a ? '<div>' + annoChips(m.a, 20) + '</div>' : ''}
    ${m.d ? `<p class="muted">${esc(m.d)}</p>` : ''}
    ${m.k !== 'init' ? `<h4><span class="tag in">IN</span> Inputs (${(m.p || []).length})</h4>${rows ? '<table><tr><th>#</th><th>Data type (object)</th><th>Name</th><th>Annotations</th></tr>' + rows + '</table>' : '<p class="muted">No parameters</p>'}
    <h4><span class="tag out">OUT</span> Output</h4><div class="mono">${outHTML(m)}</div>
    ${m.th ? `<h4>Throws</h4><div class="mono">${esc(m.th.join(', '))}</div>` : ''}` : ''}
    ${callsSection(m.cl || [], 'Calls')}
    <h4>Called by (${(m.cb || []).length})</h4>${isUnusedPrivate(m) && (m.cb || []).length ? '<p class="chip warn" style="display:inline-block">⚠ Only calls itself — this private method looks unused.</p>' : ''}${list(m.cb || [], id => id[0] === 'T'
      ? tLink(id) + ' <span class="muted">(field initializer)</span>'
      : mLink(id) + (ME[id] && ME[id].test ? ' <span class="chip test">test</span>' : '') + (ME[id] ? ' <span class="muted" style="font-size:11.5px">' + esc(ME[id]._t._f.p) + ':' + ME[id].l + '</span>' : ''),
      m.k === 'ctor' ? 'Not created with <code>new</code> anywhere in the scanned code.'
      : m.k === 'init' ? 'Runs automatically when the class is loaded / created.'
      : (m.m || []).includes('private') ? '⚠ Never called — this private method looks unused.'
      : 'Not called anywhere in the scanned code (it may be called by a framework, by reflection, or by code outside this repo).')}
    ${m.k !== 'init' ? allCallersSection(m) : ''}
    ${tests}`;
  wireCallToggles();
  wireCallerToggles();
}
function showType(id) {
  const t = TY[id]; if (!t) return;
  const f = t._f;
  const fr = (t.f || []).map(x => `<tr><td>${fieldIds(x, t)}</td><td class="mono">${(x.m || []).map(esc).join(' ')}</td><td class="mono">${ty(x.t)}</td><td class="mono">${esc(x.n)}</td></tr>`).join('');
  const mr = (t.fn || []).map(m => `<tr><td>${methodIds(m)} ${mLink(m.id)}${testBadge(m)}</td><td class="mono" style="font-size:11.5px">${m.k === 'init' ? '' : paramsHTML(m)}</td><td class="mono" style="font-size:11.5px">${outHTML(m)}</td></tr>`).join('');
  let tests = '';
  if (t.test) tests = `<h4>Class under test (technique 1: name)</h4>` + (t.tf ? tLink(t.tf) : `<p class="muted">${t.tf_missing ? 'No class named ' + esc(t.tf_missing) + ' found in main code' : 'Name does not follow a test naming pattern'}</p>`);
  else if (t.k !== 'interface' && t.k !== 'annotation') tests = `<h4>Test classes (technique 1: name)</h4>` + list(t.tc, tLink, 'No test class with a matching name');
  P.innerHTML = `<div class="muted">${esc(t.k)}${t._p ? ' nested in ' + tLink(t._p.id) : ''}${f.pkg ? ' · package ' + esc(f.pkg) : ''}</div>
    <h3>${icon(t.k)}${typeIds(t)} ${esc(t.n)}${t.tp ? '<span class="ty">' + esc(t.tp) + '</span>' : ''}</h3>
    <div class="muted">${fLink(f.p)} : L${t.l}–${t.e || t.l}</div>
    ${(t.m && t.m.length) ? '<div class="muted mono" style="margin-top:6px">' + esc(t.m.join(' ')) + '</div>' : ''}
    ${t.a ? '<div style="margin-top:4px">' + annoChips(t.a, 20) + '</div>' : ''}
    ${t.d ? `<p class="muted">${esc(t.d)}</p>` : ''}
    ${t.ext && t.ext.length ? `<h4>${t.k === 'anonymous' ? 'Creates' : 'Extends'}</h4><div class="mono">${t.ext.map(ty).join(', ')}</div>` : ''}
    ${t.impl && t.impl.length ? `<h4>Implements</h4><div class="mono">${t.impl.map(ty).join(', ')}</div>` : ''}
    ${t.perm && t.perm.length ? `<h4>Permits</h4><div class="mono">${t.perm.map(ty).join(', ')}</div>` : ''}
    ${t.c ? `<h4>Constants (${t.c.length})</h4><div class="mono">${esc(t.c.join(', '))}</div>` : ''}
    ${tests}
    <h4>Fields (${(t.f || []).length})</h4>${fr ? '<table><tr><th>Kind</th><th>Modifiers</th><th>Data type</th><th>Name</th></tr>' + fr + '</table>' : '<p class="muted">None</p>'}
    <h4>Methods (${(t.fn || []).length})</h4>${mr ? '<table><tr><th>Name</th><th><span class="tag in">IN</span></th><th><span class="tag out">OUT</span></th></tr>' + mr + '</table>' : '<p class="muted">None</p>'}
    ${t.nt && t.nt.length ? '<h4>Nested types</h4>' + list(t.nt.map(x => x.id), tLink, '') : ''}
    ${t.fcl ? callsSection(t.fcl, 'Calls in field initializers') : ''}`;
  wireCallToggles();
}
function showFile(p) {
  const f = FI[p]; if (!f) return;
  const types = [];
  if (f.types) (function r(ts) { for (const t of ts) { types.push(t.id); if (t.nt) r(t.nt); } })(f.types);
  P.innerHTML = `<div class="muted">file${f.test ? ' · <span class="chip test">test</span>' : ''}</div><h3>${esc(fname(p))}</h3><div class="muted">${esc(p)}</div>
    <p><span class="chip">${esc(f.lang)}</span>${f.s != null ? '<span class="chip">' + fmtN(f.s) + ' bytes</span>' : ''}${f.ln ? '<span class="chip">' + fmtN(f.ln) + ' lines</span>' : ''}${f.pkg ? '<span class="chip">package ' + esc(f.pkg) + '</span>' : ''}</p>
    ${f.err ? `<p class="chip warn">⚠ ${esc(f.err)}</p>` : ''}${f.skipped ? `<p class="chip warn">${esc(f.skipped)}</p>` : ''}
    ${types.length ? '<h4>Types (' + types.length + ')</h4>' + list(types, tLink, '') : ''}
    ${f.sym ? '<h4>Outline (' + f.sym.length + ')</h4>' + list(f.sym.slice(0, 200), s => esc(s.k + ' ' + s.n) + ' <span class="muted">L' + s.l + '</span>', '') : ''}
    ${f.imp && f.imp.length ? '<h4>Imports (' + f.imp.length + ')</h4>' + list(f.imp.slice(0, 300), x => '<span class="mono" style="font-size:11.5px">' + esc(x) + '</span>', '') : ''}`;
}
function introPanel() {
  P.innerHTML = `<h3>How to read this report</h3>
  <p class="muted"><b>Structure</b>: expand folders → files → classes → methods. Each method shows <span class="tag in">IN</span> parameter types (objects) and names, and <span class="tag out">OUT</span> the return type.</p>
  <p class="muted">Visibility: <span class="vis pub mono">+</span> public · <span class="vis prot mono">#</span> protected · <span class="vis pkg mono">~</span> package · <span class="vis priv mono">−</span> private.</p>
  <p class="muted"><b>Expand functions</b> opens folders, files and classes so every function is visible — field lists, test files and test classes stay closed. <b>Expand all</b> also opens the field lists; <b>Collapse all</b> closes everything.</p>
  <p class="muted"><b>All callers</b> lists every function that leads to the selected one — directly or through other functions — with the <b>number of steps</b> before the call and the call path. <b>Entry points</b> (tests, web endpoints, main, scheduled jobs, or functions nothing else calls) are highlighted.</p>
  <p class="muted">Click a method to see <b>Calls</b> (every function it calls, in order, with line numbers — calls into this repo are clickable, library/JDK calls are grey) and <b>Called by</b> (every function that calls it). On each row, <span class="cc">→3 ←2</span> means 3 calls into this repo and called by 2.</p>
  <p class="muted"> The coloured labels (ƒ function, const, static, enum, class…) and the colours of data types are explained above the tree.</p>
  <p class="muted">Click any class, method or file to see details here. Switch <b>Folders / Packages</b> at the top of the Structure tab.</p>
  <p class="muted"><b>Tests</b>: <span class="chip ok">✓ tested</span> = a test method calls this method directly (technique 3). “strong” = that test also belongs to the matching test class by name (technique 1 + 3).</p>`;
}

/* ---------------- search ---------------- */
let SIDX = null;
function buildIdx() {
  SIDX = [];
  for (const f of D.files) SIDX.push([f.p.toLowerCase(), 'file', f.p]);
  for (const id in TY) { const t = TY[id]; if (t.k === 'anonymous') continue; SIDX.push([(t.q + ' ' + (t._f.pkg || '')).toLowerCase(), 'type', id]); }
  for (const id in ME) { const m = ME[id]; SIDX.push([(m._t.n + '.' + m.n + ' ' + (m.r || '') + ' ' + (m.p || []).map(p => p[1]).join(' ')).toLowerCase(), 'method', id]); }
}
let sTimer = null;
$('#q').addEventListener('input', e => { clearTimeout(sTimer); sTimer = setTimeout(() => doSearch(e.target.value), 160); });
function doSearch(q) {
  const box = $('#results');
  q = q.trim().toLowerCase();
  if (q.length < 2) { box.classList.add('hide'); box.innerHTML = ''; return; }
  if (!SIDX) buildIdx();
  const hits = [];
  for (const e of SIDX) { if (e[0].includes(q)) { hits.push(e); if (hits.length >= 300) break; } }
  const rank = e => (e[1] === 'type' ? 0 : e[1] === 'method' ? 1 : 2);
  hits.sort((a, b) => rank(a) - rank(b));
  box.innerHTML = hits.length ? hits.map(e => {
    if (e[1] === 'file') return `<div class="row" data-f="${esc(e[2])}"><span class="ic file">▤</span>${esc(fname(e[2]))}<span class="p">${esc(e[2])}</span></div>`;
    if (e[1] === 'type') { const t = TY[e[2]]; return `<div class="row" data-t="${t.id}">${icon(t.k)}<b>${esc(t.q)}</b><span class="p">${esc(t._f.p)}</span></div>`; }
    const m = ME[e[2]];
    return `<div class="row mono" data-m="${m.id}" style="font-size:12px"><span class="tag in">IN</span>${paramsHTML(m)} <span class="tag out">OUT</span>${outHTML(m)} &nbsp;<b>${esc(m._t.n)}.${esc(m.n)}</b><span class="p">${esc(m._t._f.p)}</span></div>`;
  }).join('') + (hits.length >= 300 ? '<div class="row muted">First 300 matches shown — refine your search.</div>' : '') : '<div class="row muted">No matches</div>';
  box.classList.remove('hide');
}

/* ---------------- tabs ---------------- */
const RENDERED = {};
function showTab(name) {
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x.id === 't-' + name));
  if (!RENDERED[name]) { RENDERED[name] = true; ({overview: renderOverview, structure: renderTree, odd: renderOdd})[name](); }
}
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => { showTab(b.dataset.tab); $('#left').scrollTop = 0; });
function syncSeg() { document.querySelectorAll('#viewseg button').forEach(b => b.classList.toggle('on', b.dataset.view === VIEW)); }
document.querySelectorAll('#viewseg button').forEach(b => b.onclick = () => { VIEW = b.dataset.view; syncSeg(); renderTree(); });
$('#optPriv').onchange = e => $('#tree').classList.toggle('hide-priv', !e.target.checked);
$('#optFields').onchange = e => $('#tree').classList.toggle('hide-fields', !e.target.checked);
$('#expand').onclick = () => expandTree(false);
$('#expandFn').onclick = () => expandTree(true);
function expandTree(onlyFunctions) {
  const tree = $('#tree'), btn = $(onlyFunctions ? '#expandFn' : '#expand'), label = btn.textContent;
  const total = M.methods || 0;
  if (total > 40000 && !confirm('This repo has ' + fmtN(total) + ' methods. Expanding may take a while. Continue?')) return;
  const btns = [$('#expand'), $('#expandFn')];
  btns.forEach(b => b.disabled = true); btn.textContent = 'Expanding…';
  // "Expand functions" skips field lists, test files and test classes (and closes them if open)
  const SKIP = 'details.fields, details.testfile, details.testclass';
  const sel = onlyFunctions ? 'details:not([open]):not(.fields):not(.testfile):not(.testclass)' : 'details:not([open])';
  const done = () => { btns.forEach(b => b.disabled = false); btn.textContent = label; };
  (function step() {
    if (onlyFunctions) tree.querySelectorAll(SKIP).forEach(d => { if (d.open) d.open = false; });
    const closed = tree.querySelectorAll(sel);
    let n = 0;
    for (const d of closed) { if (d.closest('.hide')) continue; openD(d); if (++n >= 500) break; }
    if (!n) { if (onlyFunctions) tree.querySelectorAll(SKIP).forEach(d => { if (d.open) d.open = false; }); return done(); }
    requestAnimationFrame(step);
  })();
}
$('#collapse').onclick = () => document.querySelectorAll('#tree details').forEach((d, i) => { if (i > 0) d.open = false; });

/* ---------------- global click handling ---------------- */
document.addEventListener('click', e => {
  const a = e.target.closest('[data-m],[data-t],[data-f]');
  if (!a) return;
  const inTree = a.closest('#tree');
  if (a.dataset.m) { showMethod(a.dataset.m); if (!inTree) revealMethod(a.dataset.m); else selectEl(a); return; }
  if (a.dataset.t) { showType(a.dataset.t); if (!inTree) revealType(a.dataset.t); else selectEl(a); return; }
  if (a.dataset.f) { showFile(a.dataset.f); if (!inTree) reveal(a.dataset.f); return; }
});
$('#tree').addEventListener('click', e => {
  const s = e.target.closest('summary'); if (!s) return;
  const d = s.parentElement;
  if (d.classList.contains('file')) { showFile(d.dataset.path); selectEl(s); }
  else if (d.dataset.key && d.dataset.key.startsWith('t:')) { showType(d.dataset.key.slice(2)); selectEl(s); }
});
function selectEl(el) { document.querySelectorAll('.sel').forEach(x => x.classList.remove('sel')); el.classList.add('sel'); }

/* ---------------- overview ---------------- */
function card(v, l) { return `<div class="card"><div class="v">${v}</div><div class="l">${l}</div></div>`; }
function bars(obj, total, cls) {
  return '<table class="grid">' + Object.entries(obj).sort((a, b) => b[1] - a[1]).map(([k, v]) =>
    `<tr><td style="width:180px">${esc(k)}</td><td style="width:70px">${fmtN(v)}</td><td><div class="barwrap"><div class="bg"><div class="bar ${cls || ''}" style="width:${pct(v, total)}%"></div></div><span class="muted">${pct(v, total)}%</span></div></td></tr>`).join('') + '</table>';
}
function renderOverview() {
  const ty = M.types || {}, ct = M.coverage_total || {};
  const top = new Map();
  for (const f of D.files) {
    const k = f.p.includes('/') ? f.p.slice(0, f.p.indexOf('/')) + '/' : '(root files)';
    if (!top.has(k)) top.set(k, {n: 0, j: 0, m: 0, t: 0});
    const r = top.get(k); r.n++; if (f.lang === 'java') { r.j++; if (f.test) r.t++; } r.m += fileMethods(f);
  }
  $('#t-overview').innerHTML = `
    <div class="cards">
      ${card(fmtN(M.files), 'files')}${card(fmtN(M.folders), 'folders')}${card(fmtN(M.java_files), 'Java files')}
      ${card(fmtN(M.main_java), 'main Java files')}${card(fmtN(M.test_java), 'test Java files')}${card(fmtN(M.java_lines), 'Java lines')}
      ${card(fmtN(Object.values(ty).reduce((a, b) => a + b, 0)), 'types')}${card(fmtN(M.methods), 'methods / ctors')}${card(fmtN(M.fields), 'fields')}
      ${card(ct.classes ? pct(ct.with_test || 0, ct.classes) + '%' : '—', 'classes with a test')}
    </div>
    <div class="legend">Organisation: <b>${esc(M.organisation)}</b> (${M.layer_share}% of main Java files sit in layer-named packages).
      ${M.build_files.length ? '<br>Build files: ' + M.build_files.slice(0, 8).map(esc).join(', ') : ''}
      ${M.call_stats ? '<br>Calls inside functions: <b>' + fmtN(M.call_stats.in_repo) + '</b> resolved to code in this repo · ' + fmtN(M.call_stats.library) + ' library/JDK · ' + fmtN(M.call_stats.unresolved) + ' unresolved' : ''}
      <br>Scanned ${esc(M.scanned)} in batches of ${fmtN(M.batch_size)} files · analyzer v${esc(M.version)}</div>
    <h2>Types by kind</h2>${bars(ty, Object.values(ty).reduce((a, b) => a + b, 0))}
    <h2>Files by type (extension)</h2>${bars(M.langs, M.files)}
    <h2>Java source roots</h2>${bars(M.source_roots, M.java_files)}
    <h2>Top-level folders</h2>
    <table class="grid"><tr><th>Folder</th><th>Files</th><th>Java</th><th>Test Java</th><th>Methods</th></tr>
      ${[...top.entries()].sort((a, b) => b[1].n - a[1].n).map(([k, r]) => `<tr><td><a class="go" data-f="" onclick="event.stopPropagation();showTab('structure')">${esc(k)}</a></td><td>${fmtN(r.n)}</td><td>${fmtN(r.j)}</td><td>${fmtN(r.t)}</td><td>${fmtN(r.m)}</td></tr>`).join('')}
    </table>`;
}

function revealFolder(d) {
  const f = D.files.find(x => (x.p.includes('/') ? x.p.slice(0, x.p.lastIndexOf('/')) : '(root)') === d);
  if (f) { if (VIEW !== 'folder') { VIEW = 'folder'; syncSeg(); RENDERED.structure = false; } reveal(f.p); }
}

/* ---------------- odd ones out ---------------- */
function renderOdd() {
  const cats = {};
  D.anomalies.forEach(a => cats[a.cat] = (cats[a.cat] || 0) + 1);
  const el = $('#t-odd');
  el.innerHTML = `
    <div class="legend">Things that break the patterns found in this repo. <span class="sev warn">!</span> = worth fixing · <span class="sev info">i</span> = for information. Click an item to jump to it.
      Thresholds: ${M.thresholds.max_methods} methods/class, ${M.thresholds.max_class_lines} lines/class, ${M.thresholds.max_method_lines} lines/method, ${M.thresholds.max_params} parameters.</div>
    <div style="margin:8px 0">${Object.entries(cats).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<a class="chip go oc" data-c="${esc(k)}" style="cursor:pointer;font-size:12px;padding:2px 7px">${esc(k)} ${v}</a>`).join('')} <a class="chip go oc" data-c="" style="cursor:pointer;font-size:12px;padding:2px 7px">all ${D.anomalies.length}</a></div>
    <div class="toolbar" style="position:static"><input id="oq" placeholder="Filter by text or path…"><select id="osev"><option value="">all severities</option><option value="warn">! worth fixing</option><option value="info">i information</option></select></div>
    <div id="olist"></div>`;
  let cat = '', limit = 400;
  function draw() {
    const q = ($('#oq').value || '').toLowerCase(), sev = $('#osev').value;
    const rs = D.anomalies.filter(a => (!cat || a.cat === cat) && (!sev || a.sev === sev) && (!q || (a.msg + ' ' + a.p).toLowerCase().includes(q)));
    $('#olist').innerHTML = '<table class="grid"><tr><th></th><th>Category</th><th>What</th><th>Where</th></tr>' + rs.slice(0, limit).map((a, i) =>
      `<tr class="row" data-oi="${D.anomalies.indexOf(a)}" style="display:table-row"><td><span class="sev ${a.sev}">${a.sev === 'warn' ? '!' : 'i'}</span></td><td style="white-space:nowrap">${esc(a.cat)}</td><td>${esc(a.msg)}</td><td class="mono muted" style="font-size:11.5px">${esc(a.p)}${a.l ? ':' + a.l : ''}</td></tr>`).join('') + '</table>' +
      (rs.length > limit ? `<div class="more"><button class="btn" id="moreO">Show more (${fmtN(rs.length - limit)})</button></div>` : '');
    const b = $('#moreO'); if (b) b.onclick = () => { limit += 500; draw(); };
  }
  el.addEventListener('click', e => {
    const c = e.target.closest('.oc'); if (c) { cat = c.dataset.c; limit = 400; draw(); return; }
    const r = e.target.closest('[data-oi]'); if (!r) return;
    const a = D.anomalies[+r.dataset.oi];
    if (a.ref && a.ref[0] === 'M') { showMethod(a.ref); revealMethod(a.ref); }
    else if (a.ref && a.ref[0] === 'T') { showType(a.ref); revealType(a.ref); }
    else if (FI[a.p]) { showFile(a.p); reveal(a.p); }
    else revealFolder(a.p);
  });
  $('#oq').oninput = () => { limit = 400; draw(); };
  $('#osev').onchange = () => { limit = 400; draw(); };
  draw();
}

/* ---------------- boot ---------------- */
$('#pname').textContent = M.project;
const ct0 = M.coverage_total || {};
$('#hstats').innerHTML = `<b>${fmtN(M.files)}</b> files · <b>${fmtN(M.java_files)}</b> Java (${fmtN(M.test_java)} test) · <b>${fmtN(Object.values(M.types).reduce((a, b) => a + b, 0))}</b> types · <b>${fmtN(M.methods)}</b> methods · <b>${pct(ct0.tested || 0, ct0.methods)}%</b> methods directly tested · <b>${fmtN(D.anomalies.length)}</b> odd ones out · scanned ${esc(M.scanned)} · offline report`;
introPanel();
showTab('overview');
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
