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


def _relevant(changed: list[str]) -> bool:
    """True when a change could alter routing, views, or templates."""
    for item in changed:
        posix = str(item).replace("\\", "/").lower()
        if posix.endswith(("urls.py", "views.py", ".html")):
            return True
    return False


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
