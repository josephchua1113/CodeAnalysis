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

VERSION = "1.1.0"

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
                    self.scan(i + 1, close, Ctx(), owner)
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
                self.scan(j + 1, end, Ctx(), owner)
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
            return v[p]
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
                if x == "instanceof":
                    t, j = self.parse_type(i + 1)
                    if t and k[j] == ID and v[j] not in NOT_TYPE:
                        ctx.locals[v[j]] = t
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
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^=]*>)?\s*=", re.M)),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*"
                            r"([A-Za-z_$][\w$]*)\s*(<[^>(]*>)?\s*\(([^)]*)\)\s*(?::\s*([^{;=]+))?", re.M)),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
                            r"(?:async\s+)?(?:function\s*)?\(([^)]*)\)\s*(?::\s*([^=>{]+))?\s*(?:=>|\{)", re.M)),
    ("method", re.compile(r"^\s+(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*"
                          r"(?!if\b|for\b|while\b|switch\b|catch\b|return\b|function\b)"
                          r"([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*(?::\s*([^{;]+))?\s*\{", re.M)),
]
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
            if not rec["test"] and not keep_calls:
                _strip_body_info(rec["types"])
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
        "layer_share": round(layer_share), "link_stats": dict(link_stats),
        "coverage_total": dict(tot), "keep_calls": opts.keep_calls,
        "thresholds": {"max_methods": opts.max_methods, "max_class_lines": opts.max_class_lines,
                       "max_method_lines": opts.max_method_lines, "max_params": opts.max_params},
    }
    return {"meta": meta, "folders": folder_rows, "packages": package_rows,
            "coverage": cov_rows, "untested": untested[:5000], "anomalies": anomalies}


def prune_for_output(files, keep_calls):
    for f in files:
        for t in _walk_types(f.get("types", [])):
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
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
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
  <button data-tab="tests">Tests</button>
  <button data-tab="patterns">Patterns</button>
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
        <button id="collapse">Collapse all</button>
      </div>
      <div id="results" class="hide"></div>
      <div id="tree"></div>
    </div>
    <div class="tab" id="t-tests"></div>
    <div class="tab" id="t-patterns"></div>
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
const ty = s => `<span class="ty">${esc(s)}</span>`;
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
    el => fillFile(el, f), 'file');
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
  r.innerHTML = `<span class="k">${esc(s.k)}</span><span class="nm">${esc(s.n)}</span>${io}<span class="ln">L${s.l}</span>`;
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
    `${icon(t.k)}<b>${esc(t.n)}</b>${t.tp ? '<span class="ty">' + esc(t.tp) + '</span>' : ''}${heads.length ? ' <span class="muted mono" style="font-size:11.5px">' + esc(heads.join(' ')) + '</span>' : ''}${annoChips(t.a)}${badge}<span class="cnt">L${t.l}${nf ? ' · ' + nf + ' fields' : ''}${nm ? ' · ' + nm + ' methods' : ''}</span>`,
    el => {
      if (t.c && t.c.length) {
        const c = document.createElement('div'); c.className = 'note mono';
        c.textContent = 'constants: ' + t.c.join(', '); el.appendChild(c);
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
  d.querySelector('summary').dataset.type = t.id;
  return d;
}
function fieldRow(fl, t) {
  const r = document.createElement('div');
  const v = vis(fl.m, t);
  r.className = 'fld' + (v[0] === 'priv' ? ' priv-row' : '');
  const mods = (fl.m || []).filter(x => x === 'static' || x === 'final').join(' ');
  r.innerHTML = `<span class="vis ${v[0]}">${v[1]}</span>${mods ? '<span class="k">' + esc(mods) + '</span>' : ''}${ty(fl.t)} <span>${esc(fl.n)}</span>${annoChips(fl.a, 2)}${fl.mock ? '<span class="chip">mock</span>' : ''}${fl.comp ? '<span class="chip">component</span>' : ''}<span class="ln">L${fl.l}</span>`;
  return r;
}
function methodRow(m) {
  const r = document.createElement('div');
  const v = vis(m.m, m._t);
  r.className = 'm' + (v[0] === 'priv' ? ' priv-row' : '');
  r.dataset.m = m.id;
  const kind = m.k === 'ctor' ? 'ctor' : m.k === 'init' ? 'init' : ((m.m || []).includes('static') ? 'static' : '');
  const annos = (m.a || []).filter(a => a !== 'Override');
  r.innerHTML = `<span class="vis ${v[0]}">${v[1]}</span>${kind ? '<span class="k">' + kind + '</span>' : ''}<span class="nm">${esc(m.n)}</span>`
    + (m.k !== 'init' ? `<span class="io"><span class="tag in">IN</span>${paramsHTML(m)}</span><span class="io"><span class="tag out">OUT</span>${outHTML(m)}</span>` : '')
    + (m.th ? `<span class="muted">throws ${esc(m.th.join(', '))}</span>` : '')
    + annoChips(annos, 2) + testBadge(m) + `<span class="ln">L${m.l}</span>`;
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
    <h3>${esc(t.n)}.${esc(m.n)}</h3>
    <div class="muted">${fLink(f.p)} : L${m.l}${m.e && m.e !== m.l ? '–' + m.e : ''} · ${lines} line${lines > 1 ? 's' : ''}${m.b ? '' : ' · no body'}</div>
    <pre class="sig">${esc(sigText(m))}</pre>
    ${m.a ? '<div>' + annoChips(m.a, 20) + '</div>' : ''}
    ${m.d ? `<p class="muted">${esc(m.d)}</p>` : ''}
    ${m.k !== 'init' ? `<h4><span class="tag in">IN</span> Inputs (${(m.p || []).length})</h4>${rows ? '<table><tr><th>#</th><th>Type (object)</th><th>Name</th><th>Annotations</th></tr>' + rows + '</table>' : '<p class="muted">No parameters</p>'}
    <h4><span class="tag out">OUT</span> Output</h4><div class="mono">${outHTML(m)}</div>
    ${m.th ? `<h4>Throws</h4><div class="mono">${esc(m.th.join(', '))}</div>` : ''}` : ''}
    ${tests}`;
}
function showType(id) {
  const t = TY[id]; if (!t) return;
  const f = t._f;
  const fr = (t.f || []).map(x => `<tr><td class="mono">${(x.m || []).map(esc).join(' ')}</td><td class="mono">${ty(x.t)}</td><td class="mono">${esc(x.n)}</td></tr>`).join('');
  const mr = (t.fn || []).map(m => `<tr><td>${mLink(m.id)}${testBadge(m)}</td><td class="mono" style="font-size:11.5px">${m.k === 'init' ? '' : paramsHTML(m)}</td><td class="mono" style="font-size:11.5px">${outHTML(m)}</td></tr>`).join('');
  let tests = '';
  if (t.test) tests = `<h4>Class under test (technique 1: name)</h4>` + (t.tf ? tLink(t.tf) : `<p class="muted">${t.tf_missing ? 'No class named ' + esc(t.tf_missing) + ' found in main code' : 'Name does not follow a test naming pattern'}</p>`);
  else if (t.k !== 'interface' && t.k !== 'annotation') tests = `<h4>Test classes (technique 1: name)</h4>` + list(t.tc, tLink, 'No test class with a matching name');
  P.innerHTML = `<div class="muted">${esc(t.k)}${t._p ? ' nested in ' + tLink(t._p.id) : ''}${f.pkg ? ' · package ' + esc(f.pkg) : ''}</div>
    <h3>${icon(t.k)}${esc(t.n)}${t.tp ? '<span class="ty">' + esc(t.tp) + '</span>' : ''}</h3>
    <div class="muted">${fLink(f.p)} : L${t.l}–${t.e || t.l}</div>
    ${(t.m && t.m.length) ? '<div class="muted mono" style="margin-top:6px">' + esc(t.m.join(' ')) + '</div>' : ''}
    ${t.a ? '<div style="margin-top:4px">' + annoChips(t.a, 20) + '</div>' : ''}
    ${t.d ? `<p class="muted">${esc(t.d)}</p>` : ''}
    ${t.ext && t.ext.length ? `<h4>${t.k === 'anonymous' ? 'Creates' : 'Extends'}</h4><div class="mono">${t.ext.map(ty).join(', ')}</div>` : ''}
    ${t.impl && t.impl.length ? `<h4>Implements</h4><div class="mono">${t.impl.map(ty).join(', ')}</div>` : ''}
    ${t.perm && t.perm.length ? `<h4>Permits</h4><div class="mono">${t.perm.map(ty).join(', ')}</div>` : ''}
    ${t.c ? `<h4>Constants (${t.c.length})</h4><div class="mono">${esc(t.c.join(', '))}</div>` : ''}
    ${tests}
    <h4>Fields (${(t.f || []).length})</h4>${fr ? '<table><tr><th>Modifiers</th><th>Type</th><th>Name</th></tr>' + fr + '</table>' : '<p class="muted">None</p>'}
    <h4>Methods (${(t.fn || []).length})</h4>${mr ? '<table><tr><th>Name</th><th><span class="tag in">IN</span></th><th><span class="tag out">OUT</span></th></tr>' + mr + '</table>' : '<p class="muted">None</p>'}
    ${t.nt && t.nt.length ? '<h4>Nested types</h4>' + list(t.nt.map(x => x.id), tLink, '') : ''}`;
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
  if (!RENDERED[name]) { RENDERED[name] = true; ({overview: renderOverview, structure: renderTree, tests: renderTests, patterns: renderPatterns, odd: renderOdd})[name](); }
}
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => { showTab(b.dataset.tab); $('#left').scrollTop = 0; });
function syncSeg() { document.querySelectorAll('#viewseg button').forEach(b => b.classList.toggle('on', b.dataset.view === VIEW)); }
document.querySelectorAll('#viewseg button').forEach(b => b.onclick = () => { VIEW = b.dataset.view; syncSeg(); renderTree(); });
$('#optPriv').onchange = e => $('#tree').classList.toggle('hide-priv', !e.target.checked);
$('#optFields').onchange = e => $('#tree').classList.toggle('hide-fields', !e.target.checked);
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
      <br>Scanned ${esc(M.scanned)} in batches of ${fmtN(M.batch_size)} files · analyzer v${esc(M.version)}</div>
    <h2>Types by kind</h2>${bars(ty, Object.values(ty).reduce((a, b) => a + b, 0))}
    <h2>Files by type (extension)</h2>${bars(M.langs, M.files)}
    <h2>Java source roots</h2>${bars(M.source_roots, M.java_files)}
    <h2>Top-level folders</h2>
    <table class="grid"><tr><th>Folder</th><th>Files</th><th>Java</th><th>Test Java</th><th>Methods</th></tr>
      ${[...top.entries()].sort((a, b) => b[1].n - a[1].n).map(([k, r]) => `<tr><td><a class="go" data-f="" onclick="event.stopPropagation();showTab('structure')">${esc(k)}</a></td><td>${fmtN(r.n)}</td><td>${fmtN(r.j)}</td><td>${fmtN(r.t)}</td><td>${fmtN(r.m)}</td></tr>`).join('')}
    </table>`;
}

/* ---------------- tests ---------------- */
function renderTests() {
  const ct = M.coverage_total || {}, ls = M.link_stats || {};
  const rows = D.coverage.slice();
  let sortMode = 'name';
  const el = $('#t-tests');
  const testClasses = Object.values(TY).filter(t => t.test).length;
  el.innerHTML = `
    <div class="cards">
      ${card(fmtN(M.test_java), 'test files')}${card(fmtN(testClasses), 'test classes')}${card(fmtN(M.test_methods), 'test methods')}
      ${card(fmtN(ls.test_methods_linked || 0), 'test methods linked to code')}
      ${card(pct(ct.with_test || 0, ct.classes) + '%', 'classes with a test (' + fmtN(ct.with_test || 0) + '/' + fmtN(ct.classes || 0) + ')')}
      ${card(pct(ct.tested || 0, ct.methods) + '%', 'non-private methods directly tested (' + fmtN(ct.tested || 0) + '/' + fmtN(ct.methods || 0) + ')')}
    </div>
    <div class="legend"><b>How tests are linked</b> — Technique 1 (name): <code>OrderServiceTest</code> → <code>OrderService</code>. Technique 3 (calls): inside each test method, calls on the object under test (fields, <code>@InjectMocks</code>, <code>@Autowired</code>, <code>new X()</code>, locals) are matched to methods by name + argument count; calls on mocks are ignored.<br>
    <span class="chip ok">strong</span> = called directly by a test in the matching test class (1 + 3) · <span class="chip">call</span> = called directly by another test class (3 only).
    Methods only reached indirectly (e.g. private helpers) count as not directly tested.</div>
    <h2>Coverage by package <span class="muted" style="font-weight:400">— sort: <a class="go" id="sName">name</a> · <a class="go" id="sLow">lowest first</a></span></h2>
    <div id="covTable"></div>
    <h2>Methods with no direct test <span class="muted" style="font-weight:400">(non-private, with a body)</span></h2>
    <div class="toolbar" style="position:static"><input id="uq" placeholder="Filter by class, method or package…"></div>
    <div id="untested"></div>`;
  function drawCov() {
    const rs = rows.slice();
    if (sortMode === 'low') rs.sort((a, b) => pct(a.tested || 0, a.methods) - pct(b.tested || 0, b.methods));
    $('#covTable').innerHTML = '<table class="grid"><tr><th>Package</th><th>Classes</th><th>With test</th><th>Methods</th><th>Directly tested</th><th>Strong</th><th style="width:220px">%</th></tr>' +
      rs.map(r => { const p = pct(r.tested || 0, r.methods); return `<tr><td class="mono" style="font-size:12px">${esc(r.p)}</td><td>${r.classes || 0}</td><td>${r.with_test || 0}</td><td>${r.methods || 0}</td><td>${r.tested || 0}</td><td>${r.strong || 0}</td><td><div class="barwrap"><div class="bg"><div class="bar ${p >= 60 ? 'g' : p < 25 ? 'o' : ''}" style="width:${p}%"></div></div><span class="muted">${p}%</span></div></td></tr>`; }).join('') + '</table>';
  }
  drawCov();
  $('#sName').onclick = () => { sortMode = 'name'; drawCov(); };
  $('#sLow').onclick = () => { sortMode = 'low'; drawCov(); };
  let limit = 300;
  function drawUntested() {
    const q = ($('#uq').value || '').toLowerCase();
    const ids = D.untested.filter(id => { const m = ME[id]; if (!m) return false; return !q || (m._t.q + '.' + m.n + ' ' + (m._t._f.pkg || '')).toLowerCase().includes(q); });
    $('#untested').innerHTML = '<table class="grid"><tr><th>Method</th><th><span class="tag in">IN</span></th><th><span class="tag out">OUT</span></th><th>Class has test class?</th></tr>' +
      ids.slice(0, limit).map(id => { const m = ME[id]; return `<tr><td>${mLink(id)}</td><td class="mono" style="font-size:11.5px">${paramsHTML(m)}</td><td class="mono" style="font-size:11.5px">${outHTML(m)}</td><td>${m._t.tc && m._t.tc.length ? '<span class="chip ok">yes</span>' : '<span class="chip">no</span>'}</td></tr>`; }).join('') + '</table>' +
      (ids.length > limit ? `<div class="more"><button class="btn" id="moreU">Show more (${fmtN(ids.length - limit)} remaining)</button></div>` : '') +
      (D.untested.length >= 5000 ? '<p class="muted">List capped at 5,000 methods in this report.</p>' : '');
    const b = $('#moreU'); if (b) b.onclick = () => { limit += 500; drawUntested(); };
  }
  $('#uq').oninput = () => { limit = 300; drawUntested(); };
  drawUntested();
}

/* ---------------- patterns ---------------- */
function renderPatterns() {
  const styleTot = {};
  D.folders.forEach(r => Object.entries(r.styles).forEach(([k, v]) => styleTot[k] = (styleTot[k] || 0) + v));
  const el = $('#t-patterns');
  el.innerHTML = `
    <div class="legend">Organisation: <b>${esc(M.organisation)}</b> · Layout source roots: ${Object.keys(M.source_roots).slice(0, 6).map(esc).join(', ') || '—'}</div>
    <h2>File naming styles (all files)</h2>${bars(styleTot, M.files)}
    <h2>Folders <span class="muted" style="font-weight:400">— which files are in which folder, and their naming pattern</span></h2>
    <div class="toolbar" style="position:static"><input id="fq" placeholder="Filter folders…"></div>
    <div id="ftable"></div>
    <h2>Java packages <span class="muted" style="font-weight:400">— role of the classes in each package (by class-name suffix)</span></h2>
    <div class="toolbar" style="position:static"><input id="pq" placeholder="Filter packages…"></div>
    <div id="ptable"></div>`;
  let fl = 400, pl = 400;
  function drawF() {
    const q = ($('#fq').value || '').toLowerCase();
    const rs = D.folders.filter(r => !q || r.d.toLowerCase().includes(q));
    $('#ftable').innerHTML = '<table class="grid"><tr><th>Folder</th><th>Files</th><th>Main naming style</th><th>Consistency</th><th>Extensions</th><th>Java roles</th></tr>' +
      rs.slice(0, fl).map(r => `<tr><td class="mono" style="font-size:12px"><a class="go fold" data-d="${esc(r.d)}">${esc(r.d)}</a></td><td>${r.n}</td><td>${esc(r.style)}</td><td><span class="chip ${r.cons < 60 && r.n >= 4 ? 'warn' : ''}">${r.cons}%</span></td><td class="muted">${Object.entries(r.ext).map(([k, v]) => esc(k) + ' ' + v).join(', ')}</td><td class="muted">${Object.entries(r.roles).map(([k, v]) => '*' + esc(k) + ' ' + v).join(', ')}</td></tr>`).join('') + '</table>' +
      (rs.length > fl ? `<div class="more"><button class="btn" id="moreF">Show more (${fmtN(rs.length - fl)})</button></div>` : '');
    const b = $('#moreF'); if (b) b.onclick = () => { fl += 500; drawF(); };
    document.querySelectorAll('a.fold').forEach(a => a.onclick = () => revealFolder(a.dataset.d));
  }
  function drawP() {
    const q = ($('#pq').value || '').toLowerCase();
    const rs = D.packages.filter(r => !q || r.p.toLowerCase().includes(q));
    $('#ptable').innerHTML = '<table class="grid"><tr><th>Package</th><th>Files</th><th>Types</th><th>Kinds</th><th>Main role</th><th>Roles</th></tr>' +
      rs.slice(0, pl).map(r => `<tr><td class="mono" style="font-size:12px">${esc(r.p)}${r.test ? ' <span class="chip test">' + r.test + ' test</span>' : ''}</td><td>${r.files}</td><td>${r.types}</td><td class="muted">${Object.entries(r.kinds).map(([k, v]) => esc(k) + ' ' + v).join(', ')}</td><td>${r.main_role ? '*' + esc(r.main_role) : '—'}</td><td class="muted">${Object.entries(r.roles).map(([k, v]) => '*' + esc(k) + ' ' + v).join(', ')}</td></tr>`).join('') + '</table>' +
      (rs.length > pl ? `<div class="more"><button class="btn" id="moreP">Show more (${fmtN(rs.length - pl)})</button></div>` : '');
    const b = $('#moreP'); if (b) b.onclick = () => { pl += 500; drawP(); };
  }
  $('#fq').oninput = () => { fl = 400; drawF(); };
  $('#pq').oninput = () => { pl = 400; drawP(); };
  drawF(); drawP();
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
