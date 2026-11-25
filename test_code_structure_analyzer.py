#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for code_structure_analyzer.py  — run after EVERY change:

    python test_code_structure_analyzer.py

Standard library only (unittest). Builds a sample repository in a temporary folder
covering Java 1.4 … 21 syntax, tests (JUnit 3/4/5), JS/TS and all kinds of other
files, runs the analyzer on it and checks the results.

Optional extra checks (skipped automatically when not available):
  * `node`        -> the report's JavaScript is syntax-checked
  * `playwright`  -> the report is opened in a headless browser and clicked through
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import code_structure_analyzer as csa  # noqa: E402

B = "src/main/java/com/acme/shop/"
T = "src/test/java/com/acme/shop/"
REPO = {
    "pom.xml": "<project><artifactId>shop</artifactId></project>\n",
    "README.md": "# Shop\nline 2\n",
    "Dockerfile": "FROM eclipse-temurin:21\n",
    "notes.weirdext": "some\ntext\nhere\n",
    ".github/workflows/ci.yml": "on: push\n",
    "docs/diagram.PNG": b"\x89PNG\r\n\x1a\n\x00\x00\x00binary",
    "src/main/resources/application.properties": "server.port=8080\n",
    "src/main/resources/templates/orders.html": "<html><body><form action=\"/orders\"></form></body></html>\n",
    "src/main/resources/static/js/app.js": (
        "import { api } from './api.js';\n"
        "export function loadOrders(userId, page) { return api.get('/orders'); }\n"
        "const formatPrice = (amount, currency) => `${amount} ${currency}`;\n"
        "export const API_URL = '/api';\n"
        "export class OrderTable extends BaseTable {\n  render(rows) { return rows.length; }\n"
        "  static create(x) { return x; }\n}\n"),
    "src/main/resources/static/js/order-api.ts": (
        "export interface OrderDto { id: string; total: number; }\n"
        "export type Status = 'new' | 'paid';\n"
        "export enum Color { Red, Green }\n"
        "export const MAX_ROWS: number = 5;\n"
        "export async function fetchOrder(id: string, opts?: RequestInit): Promise<OrderDto> { return {} as OrderDto; }\n"),
    "src/main/java/module-info.java": "module com.acme.shop { requires java.sql; exports com.acme.shop.order; }\n",
    "src/main/java/Main.java": 'String greeting = "hi";\nvoid main() {\n    System.out.println(greeting);\n}\nint add(int a, int b) { return a + b; }\n',
    B + "order/OrderService.java": '''package com.acme.shop.order;

import java.util.*;
import static java.util.stream.Collectors.toList;

/**
 * Handles orders. Second sentence ignored.
 * @author x
 */
@Service
public class OrderService implements OrderOperations<Order> {
    private static final int MAX_ITEMS = 50;
    static final String defaultCurrency = "EUR";
    private final OrderRepository repository;
    private final Map<String, List<Order>> cache = new HashMap<String, List<Order>>();
    private Runnable hook = new Runnable() { public void run() { System.out.println("x"); } };
    int[] counts, totals[];
    static int instances = 0;

    static { System.out.println("loaded"); }

    public OrderService(OrderRepository repository) { this.repository = repository; }

    /** Places an order for the user. */
    @Override
    @Transactional(readOnly = false)
    public Order placeOrder(final User user, List<? extends OrderItem> items) throws IllegalStateException, java.io.IOException {
        if (items.size() > MAX_ITEMS) throw new IllegalStateException("too many");
        Order o = new Order("id", user, OrderStatus.NEW);
        List<String> names = items.stream().map(i -> i.toString()).collect(toList());
        Comparator<Order> c = new Comparator<Order>() {
            @Override public int compare(Order a, Order b) { return a.id().compareTo(b.id()); }
        };
        return repository.save(o);
    }

    public Order placeOrder(User user) { return placeOrder(user, Collections.<OrderItem>emptyList()); }

    public <T extends Comparable<? super T>> T max(T... values) { return values[0]; }

    protected Optional<Order> find(String id) { return repository.findById(id); }

    private double total(Map<String, Map<Integer, List<int[]>>> deep, int qty, double price) { return qty * price; }

    public boolean cancel(String id, String reason) {
        for (Order o : repository.findAll()) { if (o.id().equals(id)) return true; }
        return false;
    }

    class Inner { void touch() {} }
    static class Builder<T> { private T value; Builder<T> with(T v) { value = v; return this; } T build() { return value; } }
}
''',
    B + "order/OrderOperations.java": '''package com.acme.shop.order;
public interface OrderOperations<T> {
    T placeOrder(User user, java.util.List<? extends OrderItem> items) throws java.io.IOException;
    default String describe() { return "ops"; }
    static <X> X identity(X x) { return x; }
    private void helper() {}
}
''',
    B + "order/OrderRepository.java": '''package com.acme.shop.order;
import java.util.Optional;
import java.util.List;
public interface OrderRepository {
    Order save(Order o);
    Optional<Order> findById(String id);
    List<Order> findAll();
}
''',
    B + "order/Order.java": '''package com.acme.shop.order;
public record Order(String id, User user, OrderStatus status) implements java.io.Serializable {
    public Order {
        if (id == null) throw new IllegalArgumentException();
    }
    public Order(String id) { this(id, null, OrderStatus.NEW); }
    public record Line(String sku, int qty) {}
    public static Order empty() { return new Order("0"); }
}
''',
    B + "order/JpaOrderRepository.java": """package com.acme.shop.order;
import java.util.*;
public class JpaOrderRepository implements OrderRepository {
    public Order save(Order o) { return o; }
    public Optional<Order> findById(String id) { return Optional.empty(); }
    public List<Order> findAll() { return new ArrayList<>(); }
}
""",
    B + "order/CallSamples.java": """package com.acme.shop.order;
import java.util.*;
import static com.acme.shop.order.OrderStatus.fromCode;
public class CallSamples extends BaseSamples {
    private final OrderRepository repo;
    private final List<String> names = new ArrayList<>();
    private final int start = initialCount();
    public CallSamples(OrderRepository repo) { super(); this.repo = repo; }
    public String run(String id) {
        Order o = repo.findById(id).orElse(null);
        names.add(id);
        helper(o);
        inherited();
        OrderStatus s = fromCode("n");
        new User().getName();
        Runnable r = () -> log(id);
        mystery.call();
        System.out.println(o.status());
        this.repo.findAll();
        return id.trim();
    }
    private void helper(Order o) { log("x"); }
    private void unusedHelper() { unusedHelper(); }
    private int initialCount() { return 0; }
    private static void log(String m) {}
    void ping(int n) { pong(n); } void pong(int n) { ping(n - 1); }
    class Inner { void go() { helper(null); repo.save(null); } }
}
class BaseSamples { protected void inherited() {} }
""",
    B + "order/User.java": "package com.acme.shop.order;\npublic class User { private String name; public String getName() { return name; } }\n",
    B + "order/OrderItem.java": "package com.acme.shop.order;\npublic class OrderItem { }\n",
    B + "order/OrderStatus.java": '''package com.acme.shop.order;
public enum OrderStatus {
    NEW("n") { @Override public boolean isFinal() { return false; } },
    PAID("p") { public boolean isFinal() { return false; } },
    @Deprecated CANCELLED("c") { public boolean isFinal() { return true; } };
    private final String code;
    OrderStatus(String code) { this.code = code; }
    public abstract boolean isFinal();
    public String code() { return code; }
    public static OrderStatus fromCode(String c) { for (OrderStatus s : values()) if (s.code.equals(c)) return s; return null; }
}
''',
    B + "order/Shape.java": '''package com.acme.shop.order;
public sealed interface Shape permits Shape.Circle, Shape.Square, Shape.Blob {
    record Circle(double r) implements Shape {}
    record Square(double side) implements Shape {}
    non-sealed class Blob implements Shape {}
    static double area(Shape s) {
        var label = """
            Area of "shape" { not code }
            """;
        double a = switch (s) {
            case Circle c when c.r() > 10 -> { yield Math.PI * c.r() * c.r(); }
            case Circle c -> Math.PI * c.r() * c.r();
            case Square(double side) -> side * side;
            default -> 0;
        };
        if (s instanceof Circle c2 && c2.r() > 0) { System.out.println(label); }
        return a;
    }
}
''',
    B + "legacy/OldStuff.java": '''package com.acme.shop.legacy;
import java.util.*;
public class OldStuff {
    Vector items = new Vector();
    public static void main(String args[]) {
        Enumeration enum = new Vector().elements();
        label: for (int i = 0; i < 10; i++) { if (i > 5) break label; }
    }
    public int lengths()[] { return new int[] {1, 2}; }
    public synchronized strictfp double calc(int a, int b) { return a < b ? a : b; }
    public String process_data(String in) { return in; }
}
''',
    B + "web/OrderController.java": '''package com.acme.shop.web;
import com.acme.shop.order.*;
@RestController
@RequestMapping("/orders")
public class OrderController {
    @Autowired private OrderService orderService;
    @PostMapping
    public ResponseEntity<Order> create(@RequestBody @Valid CreateOrderRequest req, @RequestHeader("X-User") String user) {
        return ResponseEntity.ok(orderService.placeOrder(new User()));
    }
    public static class CreateOrderRequest { public String sku; }
}
''',
    B + "util/Helpers.java": '''package com.acme.shop.helpers;
public class StringHelper {
    public static String slug(String s) { return s.toLowerCase(); }
    public static String Trim(String s) { return s.trim(); }
}
''',
    B + "anno/Audit.java": '''package com.acme.shop.anno;
import java.lang.annotation.*;
@Retention(RetentionPolicy.RUNTIME)
public @interface Audit {
    String value() default "";
    int level() default 1;
    Class<?>[] groups() default {};
}
''',
    B + "broken/Broken.java": "package com.acme.shop.broken;\npublic class Broken { void a( { int x = ; } }}} class \n",
    T + "order/OrderServiceTest.java": '''package com.acme.shop.order;
import org.junit.jupiter.api.*;
import static org.mockito.Mockito.*;
class OrderServiceTest {
    @Mock OrderRepository repository;
    @InjectMocks OrderService service;
    private User user = new User();
    @BeforeEach void setUp() { MockitoAnnotations.openMocks(this); }
    @Test void placeOrder_savesOrder() {
        when(repository.save(any())).thenReturn(Order.empty());
        Order o = service.placeOrder(user, java.util.List.of());
        verify(repository).save(any());
    }
    @Test @DisplayName("cancels") void rejectsUnknown() {
        Assertions.assertFalse(service.cancel("x", "because"));
    }
    @Test void builderWorks() {
        var svc = new OrderService(mock(OrderRepository.class));
        checkFind(svc);
    }
    private void checkFind(OrderService s) { s.find("1"); }
    @ParameterizedTest void statusFromCode(String c) { OrderStatus.fromCode(c); }
}
''',
    T + "legacy/OldStuffTest.java": '''package com.acme.shop.legacy;
import junit.framework.TestCase;
public class OldStuffTest extends TestCase {
    public void testCalc() { OldStuff s = new OldStuff(); assertEquals(1.0, s.calc(1, 2)); }
    public void testNothing() { }
    private void notATest() {}
}
''',
    T + "order/PaymentGatewayTest.java": "package com.acme.shop.order;\nclass PaymentGatewayTest { @org.junit.Test public void pays() { new User().getName(); } }\n",
}


def write_repo(root, files):
    for rel, content in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(p, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as fh:
            fh.write(content)


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = csa.main(argv)
    return rc, buf.getvalue()


def walk(types):
    for t in types:
        yield t
        for c in walk(t.get("nt", [])):
            yield c


class AnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="csa_test_")
        cls.repo = os.path.join(cls.tmp, "shop")
        write_repo(cls.repo, REPO)
        # a file bigger than the size limit
        write_repo(cls.repo, {"big/huge.log": "x" * 5000})
        cls.report = os.path.join(cls.tmp, "out", "my_report.html")
        rc, cls.log = run([cls.repo, "--report-file", cls.report, "--batch-size", "7",
                           "--workers", "1", "--max-file-size", "4000"])
        assert rc == 0
        with open(os.path.join(cls.tmp, "out", "structure.json"), encoding="utf-8") as fh:
            cls.data = json.load(fh)
        cls.files = {f["p"]: f for f in cls.data["files"]}
        cls.types = {}
        cls.methods = {}
        for f in cls.data["files"]:
            for t in walk(f.get("types", [])):
                cls.types.setdefault(t["q"], t)
                for m in t.get("fn", []):
                    cls.methods[m["id"]] = (t, m)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------- helpers
    def T(self, q):
        self.assertIn(q, self.types, "type %s not found" % q)
        return self.types[q]

    def M(self, q, name, nparams=None):
        ms = [m for m in self.T(q).get("fn", []) if m["n"] == name and
              (nparams is None or len(m.get("p", [])) == nparams)]
        self.assertTrue(ms, "method %s.%s not found" % (q, name))
        return ms[0]

    def is_test_method_file(self, mid):
        for f in self.data["files"]:
            for t in walk(f.get("types", [])):
                if any(m["id"] == mid for m in t.get("fn", [])):
                    return bool(f.get("test"))
        return False

    def mid(self, q, name, nparams=None):
        return self.M(q, name, nparams)["id"]

    # ---------- configuration
    def test_config_parameters_exist(self):
        for name in ("REPO_FOLDER_PATH", "OUTPUT_REPORT_FILE", "BATCH_SIZE", "WORKERS", "EXCLUDE_NAMES",
                     "INCLUDE_HIDDEN_FOLDERS", "MAX_FILE_SIZE_BYTES"):
            self.assertTrue(hasattr(csa, name), name)
        with open(os.path.join(HERE, "code_structure_analyzer.py"), encoding="utf-8") as fh:
            head = fh.read(800)
        self.assertIn("REPO_FOLDER_PATH", head, "config must be at the top of the file")
        self.assertIn("OUTPUT_REPORT_FILE", head, "config must be at the top of the file")

    def test_config_values_used_as_defaults(self):
        old = (csa.REPO_FOLDER_PATH, csa.OUTPUT_REPORT_FILE)
        rep = os.path.join(self.tmp, "cfg", "from_config.html")
        try:
            csa.REPO_FOLDER_PATH, csa.OUTPUT_REPORT_FILE = self.repo, rep
            rc, _ = run(["--quiet", "--workers", "1"])  # no repo / report given
        finally:
            csa.REPO_FOLDER_PATH, csa.OUTPUT_REPORT_FILE = old
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(rep))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "cfg", "structure.json")))

    def test_missing_repo_gives_clear_error(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            sys_stderr, sys.stderr = sys.stderr, err
            try:
                csa.main([os.path.join(self.tmp, "does_not_exist"), "--quiet"])
            finally:
                sys.stderr = sys_stderr
        self.assertIn("REPO_FOLDER_PATH", err.getvalue())

    def test_language_tables_removed(self):
        for name in ("LANG_BY_EXT", "TEXT_LANGS", "SPECIAL_NAMES"):
            self.assertFalse(hasattr(csa, name), "%s should be removed" % name)

    # ---------- all files considered
    def test_every_file_listed(self):
        expected = set(REPO) | {"big/huge.log"}
        self.assertEqual(set(self.files), expected)

    def test_unknown_and_extensionless_files_are_read(self):
        self.assertEqual(self.files["notes.weirdext"]["lang"], "weirdext")
        self.assertEqual(self.files["notes.weirdext"]["ln"], 4)
        self.assertEqual(self.files["Dockerfile"]["lang"], "(no extension)")
        self.assertIn("ln", self.files["Dockerfile"])
        self.assertIn("ln", self.files[".github/workflows/ci.yml"])  # hidden folder included

    def test_binary_and_oversize_files(self):
        self.assertTrue(self.files["docs/diagram.PNG"].get("binary"))
        self.assertEqual(self.files["docs/diagram.PNG"]["lang"], "png")
        self.assertIn("skipped", self.files["big/huge.log"])

    def test_excludes_and_hidden_switches(self):
        write_repo(self.repo, {".git/HEAD": "ref: x\n"})
        try:
            files, _ = csa.scan_repo(self.repo, set(csa.EXCLUDE_NAMES), True)
            self.assertNotIn(".git/HEAD", files)
            files, _ = csa.scan_repo(self.repo, set(), True)
            self.assertIn(".git/HEAD", files)
            files, _ = csa.scan_repo(self.repo, set(), False)
            self.assertFalse(any(f.startswith(".") for f in files))
        finally:
            shutil.rmtree(os.path.join(self.repo, ".git"))

    # ---------- batch queue / resume
    def test_batches_written(self):
        bdir = os.path.join(self.tmp, "out", "_batches")
        n = len([x for x in os.listdir(bdir) if x.startswith("batch_")])
        self.assertEqual(n, -(-len(self.files) // 7))
        self.assertIn("Batch 1/", self.log)

    def test_resume_skips_finished_batches(self):
        out = os.path.join(self.tmp, "resume", "r.html")
        run([self.repo, "--report-file", out, "--batch-size", "5", "--workers", "1", "--quiet"])
        bdir = os.path.join(self.tmp, "resume", "_batches")
        last = sorted(x for x in os.listdir(bdir) if x.startswith("batch_"))[-1]
        os.remove(os.path.join(bdir, last))  # simulate an interruption
        rc, log = run([self.repo, "--report-file", out, "--batch-size", "5", "--workers", "1", "--resume"])
        self.assertEqual(rc, 0)
        self.assertIn("already done (resumed)", log)
        self.assertEqual(log.count("already done"), len(os.listdir(bdir)) - 2)  # minus manifest & redone batch
        # changing the file list invalidates the resume
        write_repo(self.repo, {"extra.txt": "x"})
        try:
            rc, log = run([self.repo, "--report-file", out, "--batch-size", "5", "--workers", "1", "--resume"])
            self.assertIn("--resume ignored", log)
        finally:
            os.remove(os.path.join(self.repo, "extra.txt"))

    def test_parallel_workers_same_result(self):
        out = os.path.join(self.tmp, "par", "p.html")
        run([self.repo, "--report-file", out, "--batch-size", "4", "--workers", "3", "--quiet",
             "--max-file-size", "4000"])
        with open(os.path.join(self.tmp, "par", "structure.json"), encoding="utf-8") as fh:
            par = json.load(fh)
        strip = lambda d: json.dumps(d["files"], sort_keys=True)
        self.assertEqual(strip(par), strip(self.data))

    # ---------- Java parsing
    def test_classic_class_members(self):
        t = self.T("OrderService")
        self.assertEqual(self.files[B + "order/OrderService.java"]["pkg"], "com.acme.shop.order")
        self.assertEqual(t["impl"], ["OrderOperations<Order>"])
        self.assertEqual(t["a"], ["Service"])
        self.assertEqual(t.get("d"), "Handles orders.")
        fields = {f["n"]: f["t"] for f in t["f"]}
        self.assertEqual(fields["cache"], "Map<String, List<Order>>")
        self.assertEqual(fields["totals"], "int[][]")
        self.assertIn("counts", fields)
        kinds = [m["k"] for m in t["fn"]]
        self.assertIn("init", kinds)
        self.assertIn("ctor", kinds)

    def test_method_inputs_and_outputs(self):
        m = self.M("OrderService", "placeOrder", 2)
        self.assertEqual(m["p"], [["user", "User"], ["items", "List<? extends OrderItem>"]])
        self.assertEqual(m["r"], "Order")
        self.assertEqual(m["th"], ["IllegalStateException", "java.io.IOException"])
        self.assertEqual(m["a"], ["Override", "Transactional"])
        self.assertEqual(m.get("d"), "Places an order for the user.")
        self.assertEqual(self.M("OrderService", "max")["p"], [["values", "T..."]])
        self.assertEqual(self.M("OrderService", "max")["tp"], "<T extends Comparable<? super T>>")
        self.assertEqual(self.M("OrderService", "total")["p"][0][1], "Map<String, Map<Integer, List<int[]>>>")
        self.assertIn("private", self.M("OrderService", "total")["m"])
        self.assertEqual(self.M("OrderService", "find")["r"], "Optional<Order>")

    def test_parameter_annotations(self):
        m = self.M("OrderController", "create")
        self.assertEqual(m["p"][0], ["req", "CreateOrderRequest", ["RequestBody", "Valid"]])
        self.assertEqual(m["p"][1], ["user", "String", ["RequestHeader"]])
        self.assertEqual(m["r"], "ResponseEntity<Order>")

    def test_nested_anonymous_and_inner(self):
        names = {t["n"]: t["k"] for t in walk([self.T("OrderService")])}
        self.assertEqual(names.get("new Runnable() {…}"), "anonymous")
        self.assertEqual(names.get("new Comparator() {…}"), "anonymous")
        self.assertEqual(names.get("Inner"), "class")
        self.assertEqual(names.get("Builder"), "class")
        anon = [t for t in walk([self.T("OrderService")]) if t["n"] == "new Comparator() {…}"][0]
        self.assertEqual(anon["fn"][0]["p"], [["a", "Order"], ["b", "Order"]])

    def test_interface_methods(self):
        t = self.T("OrderOperations")
        self.assertEqual(t["tp"], "<T>")
        mods = {m["n"]: m.get("m", []) for m in t["fn"]}
        self.assertEqual(mods["describe"], ["default"])
        self.assertEqual(mods["identity"], ["static"])
        self.assertEqual(mods["helper"], ["private"])

    def test_record_and_compact_constructor(self):
        t = self.T("Order")
        self.assertEqual(t["k"], "record")
        self.assertEqual([f["n"] for f in t["f"]], ["id", "user", "status"])
        ctors = [m for m in t["fn"] if m["k"] == "ctor"]
        self.assertEqual(len(ctors), 2)
        self.assertTrue(ctors[0].get("compact"))
        self.assertEqual(self.T("Order.Line")["k"], "record")

    def test_enum_with_constant_bodies(self):
        t = self.T("OrderStatus")
        self.assertEqual(t["c"], ["NEW", "PAID", "CANCELLED"])
        self.assertEqual(len([x for x in t["nt"] if x["k"] == "enum constant"]), 3)
        self.assertIn("abstract", self.M("OrderStatus", "isFinal")["m"])

    def test_sealed_switch_patterns_text_block(self):
        t = self.T("Shape")
        self.assertIn("sealed", t["m"])
        self.assertEqual(t["perm"], ["Shape.Circle", "Shape.Square", "Shape.Blob"])
        self.assertIn("non-sealed", self.T("Shape.Blob")["m"])
        self.assertEqual(self.M("Shape", "area")["e"], 18)  # body end found despite text block / switch
        self.assertEqual(self.files[B + "order/Shape.java"].get("recoveries"), 0)

    def test_java_1_4_legacy(self):
        self.assertEqual(self.M("OldStuff", "main")["p"], [["args", "String[]"]])
        self.assertEqual(self.M("OldStuff", "lengths")["r"], "int[]")
        self.assertEqual(self.files[B + "legacy/OldStuff.java"].get("recoveries"), 0)

    def test_annotation_type(self):
        t = self.T("Audit")
        self.assertEqual(t["k"], "annotation")
        self.assertEqual([m["n"] for m in t["fn"]], ["value", "level", "groups"])
        self.assertEqual(self.M("Audit", "groups")["r"], "Class<?>[]")

    def test_implicit_class_and_module_info(self):
        t = self.T("(implicit class)")
        self.assertEqual([m["n"] for m in t["fn"]], ["main", "add"])
        self.assertEqual(self.files["src/main/java/module-info.java"]["module"], "com.acme.shop")

    def test_broken_file_does_not_crash(self):
        f = self.files[B + "broken/Broken.java"]
        self.assertNotIn("err", f)
        self.assertEqual(f["types"][0]["n"], "Broken")

    def test_js_ts_outline(self):
        js = {s["n"]: s for s in self.files["src/main/resources/static/js/app.js"]["sym"]}
        self.assertEqual(js["loadOrders"]["p"], "userId, page")
        self.assertEqual(js["formatPrice"]["k"], "function")
        self.assertEqual(js["OrderTable"]["ext"], "BaseTable")
        ts = {s["n"]: s for s in self.files["src/main/resources/static/js/order-api.ts"]["sym"]}
        self.assertEqual(ts["fetchOrder"]["r"], "Promise<OrderDto>")
        self.assertEqual(ts["OrderDto"]["k"], "interface")

    # ---------- test detection & linking (technique 1 + 3)
    def test_test_detection(self):
        self.assertTrue(self.files[T + "order/OrderServiceTest.java"]["test"])
        self.assertFalse(self.files[B + "order/OrderService.java"]["test"])
        tests = [m["n"] for m in self.T("OrderServiceTest")["fn"] if m.get("test")]
        self.assertEqual(tests, ["placeOrder_savesOrder", "rejectsUnknown", "builderWorks", "statusFromCode"])
        j3 = [m["n"] for m in self.T("OldStuffTest")["fn"] if m.get("test")]
        self.assertEqual(j3, ["testCalc", "testNothing"])

    def test_technique1_name_link(self):
        self.assertEqual(self.T("OrderServiceTest")["tf"], self.T("OrderService")["id"])
        self.assertIn(self.T("OrderServiceTest")["id"], self.T("OrderService")["tc"])
        self.assertEqual(self.T("PaymentGatewayTest").get("tf_missing"), "PaymentGateway")

    def test_technique3_call_links(self):
        tb = lambda m: {x[0]: x[1] for x in m.get("tb", [])}
        test_id = lambda n: self.mid("OrderServiceTest", n)
        self.assertEqual(tb(self.M("OrderService", "placeOrder", 2)), {test_id("placeOrder_savesOrder"): "strong"})
        self.assertEqual(tb(self.M("OrderService", "cancel")), {test_id("rejectsUnknown"): "strong"})
        self.assertIn(test_id("builderWorks"), tb(self.M("OrderService", "find")))  # via helper method
        self.assertIn(test_id("builderWorks"), tb([m for m in self.T("OrderService")["fn"] if m["k"] == "ctor"][0]))
        self.assertEqual(tb(self.M("OrderStatus", "fromCode")), {test_id("statusFromCode"): "call"})  # static
        self.assertEqual(list(tb(self.M("OldStuff", "calc")).values()), ["strong"])  # JUnit 3
        self.assertTrue(tb(self.M("User", "getName")))  # new User().getName()

    def test_calls_on_mocks_are_ignored(self):
        self.assertNotIn("tb", self.M("OrderRepository", "save"))
        self.assertNotIn("tb", self.M("OrderService", "placeOrder", 1))

    def test_coverage_totals(self):
        ct = self.data["meta"]["coverage_total"]
        self.assertEqual(ct["tested"], 7)
        self.assertGreater(ct["methods"], ct["tested"])

    # ---------- odd ones out
    def test_anomalies(self):
        cats = {}
        for a in self.data["anomalies"]:
            cats.setdefault(a["cat"], []).append(a["msg"])
        self.assertIn("Public type 'StringHelper' is in file 'Helpers.java'", cats["File/class name mismatch"])
        self.assertTrue(any("com.acme.shop.helpers" in x for x in cats["Package/folder mismatch"]))
        self.assertTrue(any("process_data" in x for x in cats["Naming: method"]))
        self.assertTrue(any("Trim" in x for x in cats["Naming: method"]))
        self.assertTrue(any("defaultCurrency" in x for x in cats["Naming: constant"]))
        self.assertTrue(any("PaymentGateway" in x for x in cats["Test without matching class"]))
        self.assertIn("Skipped file", cats)
        self.assertFalse(any("placeOrder_savesOrder" in x for x in cats.get("Naming: method", [])))

    def test_overview_meta(self):
        meta = self.data["meta"]
        self.assertEqual(meta["build_files"], ["pom.xml"])
        self.assertEqual(meta["langs"]["java"], len([p for p in self.files if p.endswith(".java")]))
        self.assertEqual(meta["files"], len(self.files))

    # ---------- HTML report
    def test_report_written_to_configured_file(self):
        self.assertTrue(os.path.isfile(self.report))
        with open(self.report, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn('<script id="data" type="application/json">', html)
        self.assertNotIn("__DATA__", html)
        self.assertIsNone(re.search(r'(src|href)\s*=\s*["\']https?://', html), "report must not load anything online")
        payload = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
        self.assertEqual(json.loads(payload)["meta"]["files"], len(self.files))

    def test_tests_and_patterns_tabs_removed(self):
        with open(self.report, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn('data-tab="tests"', html)
        self.assertNotIn('data-tab="patterns"', html)
        self.assertNotIn("renderTests", html)
        self.assertNotIn("renderPatterns", html)
        payload = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
        for key in ("coverage", "untested", "folders", "packages"):
            self.assertNotIn(key, payload)          # only used by the removed tabs
            self.assertIn(key, self.data)           # still available in structure.json

    def test_report_javascript_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with open(self.report, encoding="utf-8") as fh:
            js = re.findall(r"<script>(.*?)</script>", fh.read(), re.S)[-1]
        path = os.path.join(self.tmp, "report.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_js_ts_identifiers(self):
        js = {s["n"]: s for s in self.files["src/main/resources/static/js/app.js"]["sym"]}
        self.assertEqual(js["API_URL"]["k"], "const")
        self.assertEqual(js["formatPrice"].get("decl"), "const")
        self.assertTrue(js["create"].get("static"))
        self.assertFalse(js["render"].get("static", False))
        ts = {s["n"]: s for s in self.files["src/main/resources/static/js/order-api.ts"]["sym"]}
        self.assertEqual(ts["Color"]["k"], "enum")
        self.assertEqual(ts["MAX_ROWS"]["k"], "const")
        self.assertEqual(ts["MAX_ROWS"]["r"], "number")

    # ---------- A: calls inside each function,  B: called by
    def calls(self, q, name, nparams=None):
        return {c[1]: c for c in self.M(q, name, nparams).get("cl", [])}

    def test_calls_resolved_inside_function(self):
        c = self.calls("CallSamples", "run")
        mid = self.mid
        self.assertEqual(c["OrderRepository.findById"][2:4], ["i", [mid("OrderRepository", "findById")]])
        self.assertEqual(c["OrderRepository.findById"][4], [mid("JpaOrderRepository", "findById")])  # implementation
        self.assertEqual(c["OrderRepository.findById"][0], 10)                                        # line number
        self.assertEqual(c["Optional.orElse"][2], "x")                                    # chained, library
        self.assertEqual(c["List.add"][2], "x")                                           # field of library type
        self.assertEqual(c["CallSamples.helper"][3], [mid("CallSamples", "helper")])      # own private method
        self.assertEqual(c["BaseSamples.inherited"][3], [mid("BaseSamples", "inherited")])  # inherited
        self.assertEqual(c["OrderStatus.fromCode"][3], [mid("OrderStatus", "fromCode")])  # static import
        self.assertEqual(c["User.getName"][3], [mid("User", "getName")])                  # new User().getName()
        self.assertEqual(c["CallSamples.log"][3], [mid("CallSamples", "log")])            # inside a lambda
        self.assertEqual(c["mystery.call"][2], "u")                                       # unknown receiver
        self.assertEqual(c["System.out.println"][2], "x")
        self.assertEqual(c["Order.status() (record accessor)"][2], "x")
        self.assertEqual(c["OrderRepository.findAll"][3], [mid("OrderRepository", "findAll")])  # this.repo
        self.assertEqual(c["String.trim"][2], "x")                                        # parameter type
        self.assertEqual(c["new User"][3], [self.T("User")["id"]])                         # default constructor
        lines = [x[0] for x in self.M("CallSamples", "run")["cl"]]
        self.assertEqual(lines, sorted(lines))                                            # in source order

    def test_calls_constructors_and_delegation(self):
        c = self.calls("OrderService", "placeOrder", 2)
        ctor3 = [m for m in self.T("Order")["fn"] if m["k"] == "ctor" and len(m.get("p", [])) == 3][0]
        self.assertEqual(c["new Order"][3], [ctor3["id"]])
        self.assertEqual(c["OrderRepository.save"][4], [self.mid("JpaOrderRepository", "save")])
        one_arg = [m for m in self.T("Order")["fn"] if m["k"] == "ctor" and len(m.get("p", [])) == 1][0]
        self.assertEqual([x[3] for x in one_arg["cl"] if x[1].startswith("new Order (this")], [[ctor3["id"]]])
        sup = {x[1]: x for x in [m for m in self.T("CallSamples")["fn"] if m["k"] == "ctor"][0]["cl"]}
        self.assertIn("new BaseSamples (super(…))", sup)

    def test_calls_java21_patterns_and_enum_builtins(self):
        c = self.calls("Shape", "area")
        self.assertEqual(c["Circle.r() (record accessor)"][2], "x")      # case Circle c -> c.r()
        self.assertNotIn("Square", c)                                     # record pattern is not a call
        self.assertFalse([x for x in self.M("Shape", "area")["cl"] if x[2] == "u"])
        e = self.calls("OrderStatus", "fromCode")
        self.assertEqual(e["OrderStatus.values (enum built-in)"][2], "x")

    def test_calls_from_inner_class_and_overloads(self):
        c = self.calls("CallSamples.Inner", "go")
        self.assertEqual(c["CallSamples.helper"][3], [self.mid("CallSamples", "helper")])   # outer method
        self.assertEqual(c["OrderRepository.save"][3], [self.mid("OrderRepository", "save")])  # outer field
        c1 = self.calls("OrderService", "placeOrder", 1)
        self.assertEqual(c1["OrderService.placeOrder"][3], [self.mid("OrderService", "placeOrder", 2)])

    def test_called_by(self):
        cb = lambda q, n, k=None: set(self.M(q, n, k).get("cb", []))
        mid = self.mid
        self.assertEqual(cb("CallSamples", "helper"), {mid("CallSamples", "run"), mid("CallSamples.Inner", "go")})
        self.assertIn(mid("OrderService", "placeOrder", 2), cb("JpaOrderRepository", "save"))   # via interface
        self.assertIn(mid("CallSamples.Inner", "go"), cb("JpaOrderRepository", "save"))
        callers = cb("OrderService", "placeOrder", 2)
        self.assertIn(mid("OrderService", "placeOrder", 1), callers)
        self.assertIn(mid("OrderServiceTest", "placeOrder_savesOrder"), callers)             # tests count too
        self.assertIn(mid("OrderController", "create"), cb("OrderService", "placeOrder", 1))
        self.assertIn(self.T("CallSamples")["id"], cb("CallSamples", "initialCount"))        # field initializer
        self.assertEqual(cb("OrderService", "max"), set())

    def test_unused_private_methods(self):
        msgs = [a["msg"] for a in self.data["anomalies"] if a["cat"] == "Unused private method"]
        self.assertIn("CallSamples.unusedHelper is private and never called", msgs)       # self-recursion only
        self.assertFalse(any("CallSamples.helper " in x for x in msgs))
        self.assertFalse(any("initialCount" in x for x in msgs))                           # used by a field
        self.assertFalse(any("checkFind" in x for x in msgs))                              # used by a test

    def test_call_stats_and_raw_data_removed(self):
        cs = self.data["meta"]["call_stats"]
        self.assertGreater(cs["in_repo"], 20)
        self.assertGreater(cs["library"], 5)
        for t, m in self.methods.values():
            for key in ("calls", "loc", "news", "mk"):
                self.assertNotIn(key, m)
        self.assertNotIn("fi", self.T("CallSamples"))

    def test_report_calls_panel_and_expand_functions(self):
        sync_playwright = self._browser()
        errors = []
        with sync_playwright() as p:
            exe = shutil.which("google-chrome") or shutil.which("chromium") or None
            try:
                b = p.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else p.chromium.launch()
            except Exception as e:  # noqa
                self.skipTest("no browser available: %s" % e)
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto("file://" + os.path.abspath(self.report).replace("\\", "/"))
            pg.click("#tabs button[data-tab=structure]")
            # ---- expand functions only
            pg.click("#expandFn")
            pg.wait_for_function("!document.querySelector('#expandFn').disabled")
            self.assertEqual(pg.inner_text("#expandFn"), "Expand functions")
            non_test = [m["id"] for t, m in self.methods.values() if not t.get("test") and not self.is_test_method_file(m["id"])]
            shown = pg.eval_on_selector_all("#tree .m[data-m]", "els => els.map(e => e.dataset.m)")
            self.assertEqual(sorted(shown), sorted(non_test))                          # every non-test function
            test_ids = [m["id"] for t, m in self.methods.values() if m.get("test")]
            self.assertTrue(test_ids)
            self.assertFalse(set(test_ids) & set(shown))                             # no test functions
            self.assertEqual(pg.locator("#tree details.fields[open]").count(), 0)
            self.assertEqual(pg.locator("#tree details.testfile[open], #tree details.testclass[open]").count(), 0)
            self.assertEqual(pg.locator("#tree details.testfile").count(), 3)          # test files still listed
            self.assertEqual(pg.locator("#tree details:not(.fields):not(.testfile):not(.testclass):not([open])").count(), 0)
            # after Expand all, Expand functions closes the test files again
            pg.click("#expand")
            pg.wait_for_function("!document.querySelector('#expand').disabled")
            self.assertGreater(pg.locator("#tree details.testfile[open]").count(), 0)
            pg.click("#expandFn")
            pg.wait_for_function("!document.querySelector('#expandFn').disabled")
            self.assertEqual(pg.locator("#tree details.testfile[open], #tree details.testclass[open]").count(), 0)
            self.assertEqual(pg.locator("#tree details.fields[open]").count(), 0)
            # ---- calls / called by in the panel
            run_id = self.mid("CallSamples", "run")
            pg.locator(".m[data-m=%s]" % run_id).click()
            panel = pg.inner_text("#panel")
            self.assertIn("CALLS (", panel.upper())
            self.assertIn("CALLED BY", panel.upper())
            self.assertIn("OrderRepository.findById", panel)
            self.assertIn("implemented by", panel)
            self.assertIn("? unresolved", panel)
            n_rows = pg.locator("#panel .callTable tr.call-x").count()
            self.assertGreater(n_rows, 0)
            pg.check("#panel .onlyRepo")
            self.assertFalse(pg.locator("#panel .callTable tr.call-x").first.is_visible())
            self.assertTrue(pg.locator("#panel .callTable tr.call-i").first.is_visible())
            self.assertIn("→", pg.locator(".m[data-m=%s] .cc" % run_id).inner_text())
            pg.locator("#panel a.go[data-m=%s]" % self.mid("CallSamples", "helper")).first.click()
            pg.wait_for_timeout(200)
            self.assertIn("CallSamples.helper", pg.inner_text("#panel h3"))
            self.assertIn("CallSamples.run", pg.inner_text("#panel"))                   # called by
            pg.locator(".m[data-m=%s]" % self.mid("CallSamples", "unusedHelper")).click()
            self.assertIn("looks unused", pg.inner_text("#panel"))
            # ---- expand all still opens the field lists
            pg.click("#expand")
            pg.wait_for_function("!document.querySelector('#expand').disabled")
            self.assertEqual(pg.locator("#tree details:not([open])").count(), 0)
            b.close()
        self.assertEqual(errors, [])

    def _py_all_callers(self, start):
        """Independent breadth-first search over the 'called by' data (same rules as the report)."""
        by_id = {m["id"]: (t, m) for t, m in self.methods.values()}
        types = {t["id"]: t for t in self.types.values()}
        for f in self.data["files"]:
            for t in walk(f.get("types", [])):
                types[t["id"]] = t
        def callers(i):
            if i[0] == "T":
                return sorted({c for m in types[i].get("fn", []) if m["k"] == "ctor" for c in m.get("cb", [])})
            return [c for c in by_id[i][1].get("cb", []) if c != i]
        dist, queue = {start: 0}, [start]
        for cur in queue:
            for c in callers(cur):
                if c not in dist:
                    dist[c] = dist[cur] + 1
                    queue.append(c)
        del dist[start]
        return dist, {i for i in dist if not callers(i)}

    def test_report_all_callers_with_steps(self):
        sync_playwright = self._browser()
        errors = []
        target = self.mid("JpaOrderRepository", "save")
        dist, entries = self._py_all_callers(target)
        mid = self.mid
        # expected chain:  OrderController.create -> placeOrder(User) -> placeOrder(User, items) -> repo.save
        self.assertEqual(dist[mid("OrderService", "placeOrder", 2)], 1)
        self.assertEqual(dist[mid("OrderService", "placeOrder", 1)], 2)
        self.assertEqual(dist[mid("OrderController", "create")], 3)
        self.assertEqual(dist[mid("OrderServiceTest", "placeOrder_savesOrder")], 1)   # calls repository.save itself
        self.assertEqual(dist[mid("OrderServiceTest", "rejectsUnknown")] if mid("OrderServiceTest", "rejectsUnknown") in dist else None, None)
        self.assertIn(mid("OrderController", "create"), entries)
        with sync_playwright() as p:
            exe = shutil.which("google-chrome") or shutil.which("chromium") or None
            try:
                b = p.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else p.chromium.launch()
            except Exception as e:  # noqa
                self.skipTest("no browser available: %s" % e)
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto("file://" + os.path.abspath(self.report).replace("\\", "/"))
            # report's own search gives exactly the same steps and entry points
            js = pg.evaluate("id => allCallers(id).rows.map(r => [r.id, r.d, r.entry])", target)
            self.assertEqual({r[0]: r[1] for r in js}, dist)
            self.assertEqual({r[0] for r in js if r[2]}, entries)
            self.assertEqual(pg.evaluate("id => entryKind(id)", mid("OrderController", "create")), "web endpoint @PostMapping")
            self.assertEqual(pg.evaluate("id => entryKind(id)", mid("OrderServiceTest", "placeOrder_savesOrder")), "test")
            path = pg.evaluate("id => { const r = allCallers(id); return r.path(%r).map(nodeName); }" % mid("OrderController", "create"), target)
            self.assertEqual(path, ["OrderController.create", "OrderService.placeOrder(User)",
                                    "OrderService.placeOrder(User, List)"])          # overloads told apart
            # cycles terminate: ping <-> pong
            self.assertEqual(pg.evaluate("id => allCallers(id).rows.map(r => [nodeName(r.id), r.d])", mid("CallSamples", "ping")),
                             [["CallSamples.pong", 1]])
            # field initializer as a caller, continuing to the callers of the constructor
            init_rows = pg.evaluate("id => allCallers(id).rows.map(r => nodeName(r.id))", mid("CallSamples", "initialCount"))
            self.assertEqual(init_rows, ["CallSamples (field initializer)"])
            # ---- panel UI
            pg.click("#tabs button[data-tab=structure]")
            pg.fill("#q", "JpaOrderRepository")
            pg.wait_for_timeout(400)
            pg.locator("#results .row[data-m=%s]" % target).click()
            pg.wait_for_timeout(300)
            panel = pg.inner_text("#panel")
            self.assertIn("ALL CALLERS", panel.upper())
            rows = pg.locator("#panel .callerTable tr.entry, #panel .callerTable tr.mid")
            self.assertEqual(rows.count(), len(dist))
            first_steps = pg.locator("#panel .callerTable .steps").all_inner_texts()
            self.assertEqual(first_steps, sorted(first_steps, key=int))
            create_row = pg.locator("#panel .callerTable tr", has_text="OrderController.create").first
            self.assertEqual(create_row.locator(".steps").inner_text(), "3")
            self.assertIn("entry point", create_row.inner_text())
            self.assertIn("OrderController.create → OrderService.placeOrder(User) → OrderService.placeOrder(User, List) → JpaOrderRepository.save",
                          create_row.inner_text())
            pg.check("#panel .onlyEntry")
            self.assertEqual(pg.locator("#panel .callerTable tr.mid").first.is_visible(), False)
            self.assertTrue(create_row.is_visible())
            # a method nobody calls is itself an entry point
            pg.locator("#panel a.go[data-m=%s]" % mid("OrderController", "create")).first.click()
            pg.wait_for_timeout(200)
            self.assertIn("itself an entry point (web endpoint @PostMapping)", pg.inner_text("#panel"))
            b.close()
        self.assertEqual(errors, [])

    def _browser(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright not installed")
        return sync_playwright

    def test_report_expand_all_and_identifiers(self):
        sync_playwright = self._browser()
        errors = []
        n_methods = len(self.methods)
        with sync_playwright() as p:
            exe = shutil.which("google-chrome") or shutil.which("chromium") or None
            try:
                b = p.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else p.chromium.launch()
            except Exception as e:  # noqa
                self.skipTest("no browser available: %s" % e)
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto("file://" + os.path.abspath(self.report).replace("\\", "/"))
            pg.click("#tabs button[data-tab=structure]")
            # ---- expand all
            self.assertEqual(pg.inner_text("#expand"), "Expand all")
            pg.click("#expand")
            pg.wait_for_function("document.querySelector('#expand').textContent === 'Expand all' && !document.querySelector('#expand').disabled")
            self.assertEqual(pg.locator("#tree details:not([open])").count(), 0)
            self.assertEqual(pg.locator("#tree .m[data-m]").count(), n_methods)  # every Java method rendered
            # ---- identifiers
            row = lambda sel, text: pg.locator(sel, has_text=text).first.inner_html()
            self.assertIn("id-const", row(".fld", "MAX_ITEMS"))                 # static final -> const
            self.assertIn("id-const", row(".fld", "defaultCurrency"))           # static final String -> const
            self.assertIn("id-static", row(".fld", "instances"))                # static, not final -> static
            self.assertNotIn("id-const", row(".fld", "instances"))
            self.assertIn("id-field", row(".fld", "cache"))                     # private final -> final field
            self.assertIn("id-fn", row(".m", "cancel"))                          # method -> function
            self.assertIn("id-static", pg.locator(".m[data-m=%s]" % self.mid("OrderStatus", "fromCode")).inner_html())
            self.assertIn("id-ctor", pg.locator(".m[data-m=%s]" % [m for m in self.T("Order")["fn"] if m["k"] == "ctor"][0]["id"]).inner_html())
            self.assertEqual(pg.locator(".fld.enumval").count(), 3)            # NEW, PAID, CANCELLED
            self.assertIn("id-enum", pg.locator("details[data-key='t:%s'] > summary" % self.T("OrderStatus")["id"]).inner_html())
            self.assertIn("id-static", pg.locator("details[data-key='t:%s'] > summary" % self.T("OrderService.Builder")["id"]).inner_html())
            self.assertGreater(pg.locator("#tree .m .id-const").count(), 0)    # JS const function
            # ---- data type identifiers
            dt = lambda t: pg.evaluate("t => dtype(t)[0]", t)
            self.assertEqual(dt("int"), "primitive")
            self.assertEqual(dt("int[][]"), "primitive")
            self.assertEqual(dt("String"), "value")
            self.assertEqual(dt("List<? extends OrderItem>"), "collection")
            self.assertEqual(dt("Order"), "project")
            self.assertEqual(dt("OrderStatus"), "enum")
            self.assertEqual(dt("T..."), "generic")
            self.assertEqual(dt("java.io.IOException"), "library")
            self.assertEqual(dt("@Valid CreateOrderRequest"), "project")
            self.assertGreater(pg.locator("#tree .dt-primitive").count(), 0)
            self.assertGreater(pg.locator("#tree .dt-enum").count(), 0)
            self.assertIn("Identifiers", pg.inner_text("#idlegend"))
            # ---- collapse all still works
            pg.click("#collapse")
            self.assertEqual(pg.locator("#tree details[open]").count(), 1)
            b.close()
        self.assertEqual(errors, [])

    def test_report_in_browser(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright not installed")
        errors = []
        with sync_playwright() as p:
            exe = shutil.which("google-chrome") or shutil.which("chromium") or None
            try:
                b = p.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else p.chromium.launch()
            except Exception as e:  # noqa
                self.skipTest("no browser available: %s" % e)
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.on("request", lambda r: errors.append("NETWORK " + r.url) if not r.url.startswith(("file:", "data:")) else None)
            pg.goto("file://" + os.path.abspath(self.report).replace("\\", "/"))
            self.assertEqual(pg.locator("#tabs button").all_inner_texts(), ["Overview", "Structure", "Odd ones out"])
            self.assertEqual(pg.locator("#t-tests, #t-patterns").count(), 0)
            for tab in ("structure", "odd", "overview", "structure"):
                pg.click("#tabs button[data-tab=%s]" % tab)
                pg.wait_for_timeout(100)
            pg.fill("#q", "placeOrder")
            pg.wait_for_timeout(400)
            self.assertGreater(pg.locator("#results .row[data-m]").count(), 0)
            pg.locator("#results .row[data-m]").first.click()
            pg.wait_for_timeout(200)
            self.assertIn("INPUTS", pg.inner_text("#panel").upper())
            self.assertGreater(pg.locator("#tree .m").count(), 0)
            pg.fill("#q", "")
            pg.click("#viewseg button[data-view=package]")
            pg.locator("#tree details summary").first.click()
            pg.click("#tabs button[data-tab=odd]")
            pg.locator("[data-oi]").first.click()
            pg.click("#tabs button[data-tab=structure]")
            pg.click("#expandFn")
            pg.wait_for_function("!document.querySelector('#expandFn').disabled")
            self.assertGreater(pg.locator("#tree .chip.ok", has_text="tested").count(), 0)   # test badges still shown
            pg.click("#tabs button[data-tab=overview]")
            self.assertIn("weirdext", pg.inner_text("#t-overview"))
            b.close()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
