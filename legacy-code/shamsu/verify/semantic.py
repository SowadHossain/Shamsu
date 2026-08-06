"""Behavioural checks: did the change actually work, not just parse.

`py_compile` passing is not evidence a feature exists. Observed live on
2026-08-02: a route was appended *outside* `urlpatterns`, which is valid Python,
so the syntax stage passed and SHAMSU reported "the members route has been
added ... Verification passed". The route did not exist. Every guard in the
harness governs how a change is produced; nothing checked whether it did what
was asked.

These probes run inside the target project so they can boot Django and use the
real resolver: every `name=` written into a urls.py must reverse, and every
routed page must not 500.
"""
from __future__ import annotations

import json
from pathlib import Path

# Kept as one string so it can run through the ordinary command verifier.
# Reads DJANGO_SETTINGS_MODULE out of manage.py, so it needs no configuration.
DJANGO_PROBE = r'''
import os, re, sys, pathlib
src = pathlib.Path("manage.py").read_text(encoding="utf-8", errors="replace")
m = re.search(r"DJANGO_SETTINGS_MODULE[\"']\s*,\s*[\"']([^\"']+)", src)
if not m:
    print("semantic: no DJANGO_SETTINGS_MODULE in manage.py"); sys.exit(0)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", m.group(1))
sys.path.insert(0, ".")
import django
django.setup()
from django.conf import settings
settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS or []) + ["testserver"]
settings.DEBUG = False
from django.urls import reverse, NoReverseMatch
from django.test import Client

# Every name the project's own urls.py files declare must be routable. A
# `path(...)` line that landed outside `urlpatterns` is invisible to the
# resolver - which is exactly the silent failure this catches.
declared = set()
for path in pathlib.Path(".").rglob("urls.py"):
    if "site-packages" in str(path) or "__pycache__" in str(path):
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    # `\b` matters: without it this also matched `app_name = 'library'` and
    # reported the namespace itself as an unresolvable route.
    for name in re.findall(r"\bname\s*=\s*[\"']([A-Za-z0-9_]+)[\"']", text):
        declared.add(name)

failures = []
client = Client()
checked = 0
for name in sorted(declared):
    url = None
    for candidate in (name, "library:" + name, "core:" + name):
        try:
            url = reverse(candidate)
            break
        except NoReverseMatch:
            continue
        except Exception:
            break
    if url is None:
        # Routes that need arguments cannot be reversed bare; only report a
        # name that appears in no namespace at all.
        failures.append("route '%s' is declared but does not resolve" % name)
        continue
    try:
        response = client.get(url)
        checked += 1
        if response.status_code >= 500:
            failures.append("%s returned HTTP %d" % (url, response.status_code))
    except Exception as exc:
        failures.append("%s raised %s: %s" % (url, type(exc).__name__, exc))

if failures:
    print("SEMANTIC FAILURES:")
    for item in failures:
        print(" - " + item)
    sys.exit(1)
print("semantic ok: %d route(s) resolve, %d page(s) served" % (len(declared), checked))
'''


# Templates are code, and nothing compiled them. A child template that opens
# with `<!DOCTYPE html>` before `{% extends %}` is a TemplateSyntaxError at
# render time - Django requires extends to be the first tag - but it is a
# perfectly good text file, so `py_compile` says nothing and the gate passed a
# page that could never render. Observed on every child template a 7B wrote for
# the OpenBazaar build (2026-08-03): three for three.
DJANGO_TEMPLATE_PROBE = r'''
import os, re, sys, pathlib
src = pathlib.Path("manage.py").read_text(encoding="utf-8", errors="replace")
m = re.search(r"DJANGO_SETTINGS_MODULE[\"']\s*,\s*[\"']([^\"']+)", src)
if not m:
    print("templates: no DJANGO_SETTINGS_MODULE in manage.py"); sys.exit(0)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", m.group(1))
sys.path.insert(0, ".")
import django
django.setup()
from django.template.loader import get_template

roots = [p for p in pathlib.Path(".").rglob("templates") if p.is_dir()
         and "site-packages" not in str(p)]
names = []
for root in roots:
    for path in root.rglob("*.html"):
        names.append((root, str(path.relative_to(root)).replace("\\", "/")))

failures = []
for _root, name in sorted(set(n for n in names)):
    try:
        get_template(name)
    except Exception as exc:
        failures.append("%s: %s: %s" % (name, type(exc).__name__, exc))

if failures:
    print("TEMPLATE FAILURES:")
    for item in failures:
        print(" - " + item)
    sys.exit(1)
print("templates ok: %d template(s) compile" % len(set(n for _r, n in names)))
'''


def django_template_probe_command(python_bin: str) -> str:
    return f'{python_bin} -c "exec(open(\'.shamsu_template_probe.py\').read())"'


def write_template_probe(project_root: Path) -> Path:
    target = Path(project_root) / ".shamsu_template_probe.py"
    target.write_text(DJANGO_TEMPLATE_PROBE, encoding="utf-8")
    return target


def should_probe_templates(changed: list[str], project_root: Path) -> bool:
    if not (Path(project_root) / "manage.py").is_file():
        return False
    return any(str(item).replace("\\", "/").lower().endswith(".html") for item in changed)


# The Node equivalent. Same contract: boot the real application, ask its own
# router what it declares, and require those routes to serve. A `app.get(...)`
# registered after `listen`, on a router that is never mounted, or on a second
# app instance is valid JavaScript and passes every syntax check - and serves
# nothing.
NODE_PROBE = r'''
import { pathToFileURL } from "node:url";
import { existsSync } from "node:fs";

const CANDIDATES = [
  "./src/app.js", "./app.js", "./src/server.js", "./server.js",
  "./src/app.mjs", "./app.mjs", "./backend/src/app.js", "./backend/app.js",
];

function unwrap(mod) {
  for (const value of [mod?.default, mod?.app, mod]) {
    if (typeof value === "function" && typeof value.listen === "function") return value;
  }
  return null;
}

let app = null;
let source = "";
for (const candidate of CANDIDATES) {
  if (!existsSync(candidate)) continue;
  try {
    app = unwrap(await import(pathToFileURL(candidate).href));
  } catch (error) {
    console.log(`semantic: ${candidate} failed to load: ${error.message}`);
    process.exit(1);
  }
  if (app) { source = candidate; break; }
}

if (!app) {
  // Nothing to probe is not a failure: it means the app is not exported yet.
  console.log("semantic: no module exports an Express app (export it with `export default app`)");
  process.exit(0);
}

// Express 4 exposes the router as `_router` and defines `router` as a getter
// that THROWS ('app.router' is deprecated). Express 5 renamed it back. Read
// both defensively or the probe dies before it checks anything.
function stackOf(instance) {
  for (const key of ["_router", "router"]) {
    try {
      const stack = instance?.[key]?.stack;
      if (Array.isArray(stack)) return stack;
    } catch { /* deprecated getter */ }
  }
  return [];
}

// `app.use("/api", router)` stores its mount point as a regexp; without the
// prefix every mounted route would be fetched at the wrong URL and 404 rather
// than prove anything.
function mountPrefix(layer) {
  const source = layer?.regexp?.source;
  if (typeof source !== "string" || source === "^\\/?(?=\\/|$)") return "";
  const match = source.match(/^\^\\\/((?:[^\\(?]|\\.)*?)\\\/\?\(\?=/);
  return match ? "/" + match[1].replace(/\\(.)/g, "$1") : "";
}

function routesOf(stack, prefix, found) {
  for (const layer of stack) {
    if (layer?.route?.path && layer.route.methods?.get) {
      const path = layer.route.path;
      if (typeof path === "string" && !path.includes(":") && !path.includes("*")) {
        found.push((prefix + path).replace(/\/{2,}/g, "/"));
      }
      continue;
    }
    const nested = layer?.handle?.stack;
    if (Array.isArray(nested)) routesOf(nested, prefix + mountPrefix(layer), found);
  }
  return found;
}

const routes = [...new Set(routesOf(stackOf(app), "", []))];
const server = app.listen(0);
await new Promise((resolve, reject) => {
  server.once("listening", resolve);
  server.once("error", reject);
});
const { port } = server.address();

const failures = [];
let checked = 0;
for (const route of routes) {
  try {
    const response = await fetch(`http://127.0.0.1:${port}${route}`, { redirect: "manual" });
    checked += 1;
    if (response.status >= 500) failures.push(`${route} returned HTTP ${response.status}`);
  } catch (error) {
    failures.push(`${route} raised ${error.name}: ${error.message}`);
  }
}
server.close();

if (failures.length) {
  console.log("SEMANTIC FAILURES:");
  for (const item of failures) console.log(" - " + item);
  process.exit(1);
}
console.log(`semantic ok (${source}): ${routes.length} route(s) declared, ${checked} served`);
process.exit(0);
'''

_DJANGO_SURFACE = ("urls.py", "views.py", ".html")
_NODE_SURFACE = (".js", ".mjs", ".ts", ".jsx", ".tsx")


def _relevant(changed: list[str]) -> bool:
    """True when a change could alter routing, views, or templates."""
    for item in changed:
        posix = str(item).replace("\\", "/").lower()
        if posix.endswith(_DJANGO_SURFACE):
            return True
    return False


def _node_relevant(changed: list[str]) -> bool:
    for item in changed:
        posix = str(item).replace("\\", "/").lower()
        if posix.endswith(_NODE_SURFACE):
            return True
    return False


def node_probe_command() -> str:
    """The shell command that runs the Node probe from the project root."""
    return "node .shamsu_probe.mjs"


def write_node_probe(project_root: Path) -> Path:
    target = Path(project_root) / ".shamsu_probe.mjs"
    target.write_text(NODE_PROBE, encoding="utf-8")
    return target


# Frameworks whose apps this probe knows how to boot. A Vite/React project is
# also "node", but it has no server to mount routes on - probing it would add a
# step that can only ever report "nothing to probe".
_SERVER_FRAMEWORKS = ("express", "fastify", "koa", "@nestjs/core", "@hapi/hapi", "hapi")


def _declares_server_framework(project_root: Path) -> bool:
    try:
        package = json.loads((Path(project_root) / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(package, dict):
        return False
    declared: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = package.get(key)
        if isinstance(section, dict):
            declared.update(str(name).lower() for name in section)
    return any(name in declared for name in _SERVER_FRAMEWORKS)


def should_probe_node(changed: list[str], project_root: Path) -> bool:
    return (
        bool(changed)
        and _node_relevant(changed)
        and (Path(project_root) / "package.json").is_file()
        and _declares_server_framework(project_root)
    )


def django_probe_command(python_bin: str) -> str:
    """The shell command that runs the probe from the project root."""
    return f'{python_bin} -c "exec(open(\'.shamsu_probe.py\').read())"'


def write_probe(project_root: Path) -> Path:
    """Materialize the probe next to manage.py and return its path."""
    target = Path(project_root) / ".shamsu_probe.py"
    target.write_text(DJANGO_PROBE, encoding="utf-8")
    return target


def should_probe(changed: list[str], project_root: Path) -> bool:
    return bool(changed) and _relevant(changed) and (Path(project_root) / "manage.py").is_file()
