"""Sealed-build helper: summarize JUnit XML results of every Gradle test task.

Prints per-task counts, a grand total, and every skipped test grouped by its
skip reason. Gradle's JUnit XML records no reason for JUnit 5 condition/
assumption skips, so the reason is read from the test source: the skip
annotation on the method (or its class / base class), or the assumption in
the method body.
"""
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

SKIP_ANN = re.compile(
    r"@(Disabled|DisabledOn\w+|EnabledOn\w+|DisabledIf\w*|EnabledIf\w*|EnabledInNativeImage|DisabledInNativeImage)\b(\([^)]*\))?",
    re.S,
)

sources = defaultdict(list)  # simple class name -> paths
for path in glob.glob("**/src/*/java/**/*.java", recursive=True):
    if "/build/" in path.replace(os.sep, "/"):
        continue
    sources[os.path.basename(path)[:-5]].append(path)
_cache = {}


def read(path):
    if path not in _cache:
        with open(path, encoding="utf-8", errors="replace") as fh:
            _cache[path] = fh.read()
    return _cache[path]


def find_file(qualified):
    outer = qualified.split("$")[0]
    simple = outer.rsplit(".", 1)[-1]
    suffix = outer.replace(".", "/") + ".java"
    cands = sources.get(simple, [])
    for p in cands:
        if p.replace(os.sep, "/").endswith(suffix):
            return p
    return cands[0] if cands else None


def norm(ann):
    return " ".join(ann.split())


def class_reason(text, simple):
    m = re.search(r"((?:@[^\n]*\n|\s)*)\s*(?:public\s+|abstract\s+|final\s+)*class\s+" + re.escape(simple) + r"\b", text)
    if not m:
        return None
    anns = m.group(1)
    a = SKIP_ANN.search(anns)
    if a:
        return "class annotated " + norm(a.group(0))
    if "disabledWithoutDocker = true" in anns:
        return "Docker not available on this runner (@Testcontainers(disabledWithoutDocker = true))"
    return None


def method_reason(path, simple, method, depth=0):
    if not path or depth > 4:
        return None
    text = read(path)
    sig = re.search(r"\bvoid\s+" + re.escape(method) + r"\s*\(", text)
    if sig:
        head = text[: sig.start()]
        ends = list(re.finditer(r"[;{}][ \t]*\n", head))
        cut = ends[-1].end() if ends else 0
        a = SKIP_ANN.search(head[cut:])
        if a:
            return "method annotated " + norm(a.group(0))
        body = text[sig.end() : sig.end() + 3000]
        end = re.search(r"\n  }\n", body)
        body = body[: end.start()] if end else body
        if "assume" in body:
            lines = body.splitlines()
            for i, line in enumerate(lines):
                if "assume" in line:
                    j = i
                    while j > 0 and lines[j - 1].strip().startswith("//"):
                        j -= 1
                    comment = " ".join(x.strip().lstrip("/ ") for x in lines[j:i])
                    why = comment or norm(" ".join(lines[i : i + 3]))[:160]
                    return "assumption not met at runtime: " + why
    cr = class_reason(text, simple)
    if cr:
        return cr
    ext = re.search(r"class\s+" + re.escape(simple) + r"\b[^{]*?\bextends\s+(\w+)", text)
    if ext:
        base = ext.group(1)
        for p in sources.get(base, []):
            r = method_reason(p, base, method, depth + 1)
            if r:
                return r
    return None


def skip_reason(classname, name, sk):
    msg = (sk.get("message") or sk.text or "").strip()
    if msg:
        return msg.splitlines()[0][:200]
    m = re.match(r"\w+", name or "")
    method = m.group(0) if m else ""
    simple = classname.split("$")[-1].rsplit(".", 1)[-1]
    r = method_reason(find_file(classname), simple, method)
    return r or "no reason recorded"


per_task = defaultdict(lambda: [0, 0, 0, 0])  # tests, failures, errors, skipped
skipped = defaultdict(list)
for path in glob.glob("**/build/test-results/*/TEST-*.xml", recursive=True):
    task = os.path.dirname(path).replace(os.sep, "/").replace("/build/test-results/", ":")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        continue
    for case in root.iter("testcase"):
        c = per_task[task]
        c[0] += 1
        if case.find("failure") is not None:
            c[1] += 1
        elif case.find("error") is not None:
            c[2] += 1
        else:
            sk = case.find("skipped")
            if sk is not None:
                c[3] += 1
                cls, name = case.get("classname") or "", case.get("name") or ""
                skipped[skip_reason(cls, name, sk)].append(f"{cls.rsplit('.', 1)[-1]}.{name}")

print("==================== SEAL TEST SUMMARY ====================")
tot = [0, 0, 0, 0]
for task in sorted(per_task):
    c = per_task[task]
    tot = [a + b for a, b in zip(tot, c)]
    print(f"{task}: tests={c[0]} passed={c[0]-c[1]-c[2]-c[3]} failed={c[1]+c[2]} skipped={c[3]}")
print(f"TOTAL: tests={tot[0]} passed={tot[0]-tot[1]-tot[2]-tot[3]} failed={tot[1]+tot[2]} skipped={tot[3]}")
print("Skipped tests by reason (one line per reason):")
for reason in sorted(skipped):
    names = sorted(set(skipped[reason]))
    shown = names[:40]
    more = f" ... and {len(names) - 40} more" if len(names) > 40 else ""
    print(f"SKIPPED ({len(skipped[reason])}) reason: {reason} -> {', '.join(shown)}{more}")
print("============================================================")
sys.exit(0)
