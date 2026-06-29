# SHAMSU — Week 2 Milestone v2
## PRD → Full Web Project (Django + DRF + DaisyUI + HTMX)
> **3 developers · 12 days · Git milestone structure · v0.2.0**
> Model ceiling: 7B. Use smaller wherever possible.

---

## What Changed From v1 and Why

| Decision | v1 (old) | v2 (new) | Why changed |
|---|---|---|---|
| Backend | FastAPI + SQLAlchemy | **Django 5 + DRF** | DRF's ModelViewSet cuts route generation from ~60 lines to ~6. 4× less LLM output for the same app. |
| Auth | JWT + passlib + python-jose (~60 lines to generate) | **django.contrib.auth + simplejwt** | Built-in auth = 0 lines generated. JWT config = 4 settings lines. |
| Frontend | Vanilla HTML + Alpine.js | **Django Templates + HTMX** | Same server serves backend and frontend. No CORS. No JWT in localStorage. ~25 lines per page vs ~60. |
| Styling | Plain Tailwind CDN | **DaisyUI + Tailwind CDN** | Plain Tailwind = inconsistent design across pages. DaisyUI semantic classes (`btn btn-primary`) = consistent design every time. |
| Forms | Manual HTML input fields | **django-crispy-forms + crispy-tailwind** | `{{ form\|crispy }}` = one tag generates the entire form, styled, with error messages. Zero markup written by LLM. |
| Database | SQLite via SQLAlchemy | **SQLite via Django ORM** | `makemigrations && migrate` = automatic. No Alembic. No create_all(). |
| Total LLM lines (todo app) | ~326 lines | **~290 lines** | And those 290 lines are simpler, more constrained, lower failure rate. |

---

## The Full Stack

```
Language:   Python 3.11+
Backend:    Django 5 + Django REST Framework 3.15
Auth:       django.contrib.auth (sessions) + djangorestframework-simplejwt
Database:   SQLite (dev) → PostgreSQL (prod, config switch only)
Frontend:   Django Templates (built-in) + HTMX 1.9
Styling:    DaisyUI 4 + Tailwind CSS (both via CDN — no build step)
Forms:      django-crispy-forms + crispy-tailwind
Testing:    Django TestCase + DRF APIClient
Run:        python manage.py runserver (one command, serves everything)
```

**No node_modules. No build step. No separate frontend server. No CORS config.**

---

## Why This Stack Wins for a 7B Model

### The lines-per-file argument

For a todo app (3 entities, 9 endpoints, 4 pages):

| File | Django+DRF+DaisyUI | FastAPI+Alpine | Difference |
|---|---|---|---|
| Data models | ~8 lines/entity | ~12 lines/entity | Django simpler |
| Serializer/Schema | ~6 lines/entity (ModelSerializer) | ~15 lines/entity (Pydantic, must match model) | Django can't mismatch |
| CRUD routes | ~6 lines/resource (ModelViewSet) | ~60 lines/resource (5 route functions) | **10× fewer** |
| Auth setup | **0 lines** (built in) | ~60 lines (auth.py) | Django wins completely |
| Frontend page | ~25 lines (Django template + HTMX) | ~60 lines (HTML + Alpine.js + fetch + JWT) | Django simpler |
| Forms | **1 line** (`{{ form\|crispy }}`) | ~12 lines/field | Django wins completely |
| **Total** | **~290 lines** | **~326 lines FastAPI alone** | And Django's are simpler |

### The consistency argument

Plain Tailwind forces the LLM to invent styling decisions on every file:

```html
<!-- LLM invents different styles every time -->
<button class="bg-blue-500 px-4 py-2 text-white rounded">Save</button>      <!-- page 1 -->
<button class="bg-indigo-600 p-2 text-white rounded-md">Submit</button>      <!-- page 2 -->
<button class="bg-blue-600 px-3 py-1.5 text-white rounded-lg">Add</button>  <!-- page 3 -->
```

DaisyUI gives the LLM a single semantic name to use every time:

```html
<!-- Always the same. DaisyUI decides the visual. -->
<button class="btn btn-primary">Save</button>
<button class="btn btn-primary">Submit</button>
<button class="btn btn-primary">Add</button>
```

One decision vs eight decisions. One right answer vs infinite variations.

---

## Generated Project Structure

```
{project_name}/
  manage.py                     ← fixed template (no LLM)
  {project_name}/
    settings.py                 ← fixed template (3 substitutions)
    urls.py                     ← LLM: include(app.urls) per app
    wsgi.py                     ← fixed template (no LLM)
    asgi.py                     ← fixed template (no LLM)
  {app_name}/                   ← one Django app per PRD resource group
    models.py                   ← LLM: ~8 lines per entity
    serializers.py              ← LLM: ~6 lines per entity
    views.py                    ← LLM: ~6 lines per resource (ViewSet) + template views
    urls.py                     ← LLM: ~3 lines per resource
    forms.py                    ← LLM: ~4 lines per entity (ModelForm)
    admin.py                    ← LLM: ~2 lines per model
    templates/
      {app_name}/
        base.html               ← fixed template (DaisyUI + HTMX + navbar)
        login.html              ← fixed template (always same login form)
        register.html           ← fixed template (always same register form)
        dashboard.html          ← LLM: ~30 lines
        {resource}_list.html    ← LLM: ~25 lines per resource
        {resource}_detail.html  ← LLM: ~25 lines per resource
        _{resource}_item.html   ← LLM: ~10 lines (HTMX partial)
    tests/
      test_{resource}.py        ← LLM: ~25 lines per resource
  requirements.txt              ← fixed template (no LLM)
  .env.example                  ← fixed template (no LLM)
  README.md                     ← LLM (Phi-3 Mini, prose)
```

**Fixed templates (no LLM needed):** `manage.py`, `settings.py`, `wsgi.py`, `asgi.py`, `requirements.txt`, `.env.example`, `base.html`, `login.html`, `register.html`

That's 9 files SHAMSU generates through template substitution — zero model inference. The LLM only touches files where the content genuinely varies per project.

---

## Model Assignments

| Task | Model | Lines generated | Why |
|---|---|---|---|
| Router / intent classification | `phi3:mini` 3.8B | — | Always loaded. Fast. |
| PRD parsing → project plan | `phi3:mini` 3.8B | JSON output | Short structured JSON. Phi-3 reliable. |
| `models.py` | `qwen2.5-coder:7b` | ~8/entity | Django ORM fields + relationships |
| `serializers.py` | `phi3:mini` 3.8B | ~6/entity | ModelSerializer is simple enough |
| `views.py` (ViewSets) | `qwen2.5-coder:7b` | ~6/resource | ViewSet + filter logic |
| `views.py` (template views) | `phi3:mini` 3.8B | ~8/view | Simple render() calls |
| `urls.py` | `phi3:mini` 3.8B | ~3/resource | router.register() calls |
| `forms.py` | `phi3:mini` 3.8B | ~4/entity | ModelForm is trivial |
| `admin.py` | `phi3:mini` 3.8B | ~2/model | admin.site.register() |
| `settings.py` | **No LLM** | 0 | Fixed template |
| `base.html` | **No LLM** | 0 | Fixed template |
| `login.html` / `register.html` | **No LLM** | 0 | Fixed template |
| `dashboard.html` | `qwen2.5-coder:7b` | ~30 | DaisyUI stats + HTMX table |
| `{resource}_list.html` | `qwen2.5-coder:7b` | ~25/page | DaisyUI table + HTMX form |
| `_{resource}_item.html` | `phi3:mini` 3.8B | ~10/partial | Simple HTMX partial |
| `test_{resource}.py` | `qwen2.5-coder:7b` | ~25/file | APIClient test cases |
| `README.md` | `phi3:mini` 3.8B | prose | Already loaded, no swap |
| Bug fixing | `qwen2.5-coder:7b` | diffs | Read errors, targeted patches |
| Summary report | `phi3:mini` 3.8B | prose | Already loaded, no swap |

**Peak RAM:** Phi-3 Mini (1 GB) + Qwen2.5-Coder 7B (4.5 GB) + OS + tools ≈ 7 GB. Safe on 8 GB.

---

## The Generation Pipeline (16 Steps)

SHAMSU runs these steps in strict order for every PRD → project request.

```
PHASE 1: Parse (no LLM)
Step 1:  PRDParser.parse(prd_file)                     → ParsedPRD
Step 2:  PRDExtractor.extract_entities()               → list[EntitySpec]
Step 3:  PRDExtractor.extract_endpoints()              → list[EndpointSpec]
Step 4:  PRDExtractor.extract_pages()                  → list[PageSpec]
Step 5:  PRDExtractor.detect_relationships()           → ForeignKey, M2M links

PHASE 2: Plan (Phi-3 Mini)
Step 6:  PlannerAgent.create_project_plan()            → ProjectSpec + FileGenOrder
Step 7:  Show plan to user, ask approval               → CLI gate

PHASE 3: Fixed templates (no LLM — template substitution only)
Step 8:  Generate: manage.py, settings.py, wsgi.py, asgi.py
         Generate: requirements.txt, .env.example
         Generate: base.html, login.html, register.html
         Generate: project urls.py stub

PHASE 4: Backend (LLM — strict order, each file feeds the next)
Step 9:  Generate models.py                            → Qwen2.5-Coder 7B
Step 10: Generate serializers.py                       → Phi-3 Mini
Step 11: Generate forms.py                             → Phi-3 Mini
Step 12: Generate views.py (ViewSets + template views) → Qwen2.5-Coder 7B
Step 13: Generate app urls.py                          → Phi-3 Mini
Step 14: Generate admin.py                             → Phi-3 Mini

PHASE 5: Frontend (LLM — uses already-generated models/views for field names)
Step 15: Generate dashboard.html                       → Qwen2.5-Coder 7B
         Generate {resource}_list.html (per resource)  → Qwen2.5-Coder 7B
         Generate _{resource}_item.html (per resource) → Phi-3 Mini

PHASE 6: Tests + Run + Fix
Step 16: Generate test_{resource}.py (per resource)    → Qwen2.5-Coder 7B
Step 17: Run: pip install -r requirements.txt          → CommandRunner (approval)
Step 18: Run: python manage.py migrate                 → CommandRunner (approval)
Step 19: Run: python manage.py test                    → CommandRunner (approval)
Step 20: ErrorFeedbackLoop (if tests fail)             → BugFixer
Step 21: Generate README.md                            → Phi-3 Mini
Step 22: Final summary report                          → Phi-3 Mini (no swap)
```

**Why this exact order matters:**
- `settings.py` before everything — all other files import from it
- `models.py` before `serializers.py` — serializer reads model field names
- `models.py` before `forms.py` — ModelForm reads model fields
- `views.py` after models + serializers — ViewSet imports both
- `urls.py` after views — imports ViewSet class names
- Frontend after backend — HTML templates use `{% url 'view-name' %}` which must exist
- Tests after everything — tests import views, models, URLs

---

## Context Pack Strategy Per File

### `models.py` context pack
```
SYSTEM: You are SHAMSU's Django model generator. Output ONLY Python.
        Use Django ORM. Import from django.db import models.
        Use ForeignKey for relationships. Always include __str__.

CONTEXT:
- Entities from PRD: {entities_json}
  Example: [{"name": "Task", "fields": [
    {"name": "title", "type": "CharField", "max_length": 200},
    {"name": "status", "type": "CharField", "choices": [...]},
    {"name": "due_date", "type": "DateField", "null": true},
    {"name": "user", "type": "ForeignKey", "to": "User", "on_delete": "CASCADE"}
  ]}]
- Django field reference (10-line snippet from template library)

EXPECTED OUTPUT: ~8 lines per entity. Total: ~24 lines for 3 entities.
```

### `serializers.py` context pack
```
SYSTEM: You are SHAMSU's Django serializer generator. Output ONLY Python.
        Use ModelSerializer. Import from rest_framework import serializers.
        List fields explicitly in Meta. Never use fields = '__all__'.

CONTEXT:
- The full content of the just-generated models.py
  (model class definitions — serializer must match field names exactly)
- Entities list: {entities_json}

EXPECTED OUTPUT: ~6 lines per entity. Total: ~18 lines for 3 entities.
KEY TRICK: Serializer sees models.py content → field names CANNOT mismatch.
```

### `views.py` context pack
```
SYSTEM: You are SHAMSU's Django view generator. Output ONLY Python.
        Use ModelViewSet for API views. Use @login_required + render() for template views.
        Filter querysets by request.user always.

CONTEXT:
- Content of models.py (model classes)
- Content of serializers.py (serializer classes)
- Endpoint specs: {endpoints_json}
- Page specs: {pages_json}
- 15-line ModelViewSet example from template library

EXPECTED OUTPUT: ~6 lines per ViewSet + ~8 lines per template view.
```

### `{resource}_list.html` context pack
```
SYSTEM: You are SHAMSU's Django template generator. Output ONLY HTML.
        Always extend base.html. Use DaisyUI classes ONLY — never raw Tailwind.
        Use HTMX for all interactions. Use {% url %} for all links.
        Use {{ form|crispy }} for all forms. Never write <input> tags manually.

DaisyUI reference (include in EVERY frontend prompt):
  btn btn-primary | btn-secondary | btn-ghost | btn-error | btn-sm
  card bg-base-100 shadow-xl | card-body | card-title
  table table-zebra | overflow-x-auto
  badge badge-success | badge-warning | badge-error
  alert alert-success | alert-error
  modal | modal-box | modal-action
  stats shadow | stat | stat-title | stat-value

CONTEXT:
- Page spec: {page_spec_json} (purpose, data to show, actions)
- URL names from urls.py (just the names, not the full file)
- Field names from models.py for this resource
- base.html content (so LLM knows what blocks exist)

EXPECTED OUTPUT: ~25 lines inside {% block content %}.
```

---

## Fixed Templates (Generated Without LLM)

### `settings.py` — 3 substitution points only
```python
SECRET_KEY = '{{ secret_key }}'           # generated by SHAMSU (uuid4-based)
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'crispy_forms',
    'crispy_tailwind',
    '{{ app_name }}',                     # substituted from PRD project name
]
CRISPY_ALLOWED_TEMPLATE_PACKS = "tailwind"
CRISPY_TEMPLATE_PACK = "tailwind"
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/login/'
# ... rest is always identical boilerplate
```

### `base.html` — always identical, 2 CDN tags, HTMX, DaisyUI
```html
<!DOCTYPE html>
<html lang="en" data-theme="{{ theme|default:'corporate' }}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}{{ project_name }}{% endblock %}</title>
  <link href="https://cdn.jsdelivr.net/npm/daisyui@4/dist/full.min.css" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/htmx.org@1.9/dist/htmx.min.js"></script>
</head>
<body class="min-h-screen bg-base-200">
  <div class="navbar bg-base-300 shadow-md">
    <div class="navbar-start">
      <a href="{% url 'dashboard' %}" class="btn btn-ghost text-xl">{{ project_name }}</a>
    </div>
    <div class="navbar-center hidden lg:flex">
      <ul class="menu menu-horizontal px-1">
        <!-- Nav links injected by SHAMSU based on page list from PRD -->
        {% for page in nav_pages %}
          <li><a href="{% url page.url_name %}">{{ page.label }}</a></li>
        {% endfor %}
      </ul>
    </div>
    <div class="navbar-end">
      {% if user.is_authenticated %}
        <span class="text-sm mr-4 opacity-70">{{ user.username }}</span>
        <a href="{% url 'logout' %}" class="btn btn-ghost btn-sm">Logout</a>
      {% else %}
        <a href="{% url 'login' %}" class="btn btn-primary btn-sm">Login</a>
      {% endif %}
    </div>
  </div>

  {% if messages %}
    <div class="container mx-auto px-4 mt-4">
      {% for message in messages %}
        <div class="alert alert-{{ message.tags }} mb-2 shadow">
          <span>{{ message }}</span>
        </div>
      {% endfor %}
    </div>
  {% endif %}

  <main class="container mx-auto px-4 py-8">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

### `requirements.txt` — always identical
```
Django==5.0.6
djangorestframework==3.15.2
djangorestframework-simplejwt==5.3.1
django-crispy-forms==2.3
crispy-tailwind==1.0.3
Pillow==10.3.0
pytest-django==4.8.0
```

---

## What a Generated Page Looks Like

### `tasks/task_list.html` — what the LLM writes (~25 lines)
```html
{% extends "base.html" %}
{% load crispy_forms_tags %}

{% block title %}Tasks{% endblock %}

{% block content %}
<div class="flex justify-between items-center mb-6">
  <h1 class="text-3xl font-bold">My Tasks</h1>
  <button class="btn btn-primary" onclick="document.getElementById('add-modal').showModal()">
    + Add Task
  </button>
</div>

<!-- Add task modal -->
<dialog id="add-modal" class="modal">
  <div class="modal-box">
    <h3 class="font-bold text-lg mb-4">New Task</h3>
    <form hx-post="{% url 'task-list-create' %}"
          hx-target="#task-table-body"
          hx-swap="afterbegin"
          hx-on::after-request="this.reset(); document.getElementById('add-modal').close()">
      {% csrf_token %}
      {{ form|crispy }}
      <div class="modal-action">
        <button type="submit" class="btn btn-primary">Save</button>
        <button type="button" class="btn btn-ghost"
                onclick="document.getElementById('add-modal').close()">Cancel</button>
      </div>
    </form>
  </div>
</dialog>

<!-- Task table -->
<div class="overflow-x-auto">
  <table class="table table-zebra">
    <thead>
      <tr>
        <th>Title</th><th>Status</th><th>Due Date</th><th>Actions</th>
      </tr>
    </thead>
    <tbody id="task-table-body">
      {% for task in tasks %}
        {% include "tasks/_task_item.html" %}
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

### `tasks/_task_item.html` — the HTMX partial (~10 lines)
```html
<tr id="task-{{ task.id }}">
  <td>{{ task.title }}</td>
  <td>
    <span class="badge {% if task.status == 'done' %}badge-success
                        {% elif task.status == 'in_progress' %}badge-warning
                        {% else %}badge-ghost{% endif %}">
      {{ task.get_status_display }}
    </span>
  </td>
  <td>{{ task.due_date|default:"—" }}</td>
  <td>
    <button class="btn btn-error btn-xs"
            hx-delete="{% url 'task-detail' task.id %}"
            hx-target="#task-{{ task.id }}"
            hx-swap="outerHTML"
            hx-confirm="Delete this task?">
      Delete
    </button>
  </td>
</tr>
```

---

## GitHub Issues for Week 2 (28 new issues)

Label all as `milestone:v0.2.0`.

### Backend generators

| # | Issue | Owner | Branch |
|---|---|---|---|
| 51 | `ProjectSpec`, `EntitySpec`, `PageSpec`, `EndpointSpec` dataclasses | Dev A | `feature/dev-a/django-project-spec` |
| 52 | Django template library (settings, manage, wsgi, base.html, login, register) | Dev A | `feature/dev-a/django-templates` |
| 53 | `PRDExtractor` v2 — entity + endpoint + page + relationship extraction | Dev A | `feature/dev-a/prd-extractor-v2` |
| 54 | `DjangoProjectGenerator` — 22-step pipeline skeleton | Dev B | `feature/dev-b/django-pipeline` |
| 55 | `ModelFileGenerator` — Django ORM models.py | Dev A | `feature/dev-a/model-generator` |
| 56 | `SerializerFileGenerator` — DRF ModelSerializer | Dev B | `feature/dev-b/serializer-generator` |
| 57 | `FormFileGenerator` — Django ModelForm | Dev B | `feature/dev-b/form-generator` |
| 58 | `ViewFileGenerator` — ModelViewSet + template views | Dev A | `feature/dev-a/view-generator` |
| 59 | `URLFileGenerator` — DRF router + path() | Dev B | `feature/dev-b/url-generator` |
| 60 | `AdminFileGenerator` — admin.site.register() | Dev B | `feature/dev-b/admin-generator` |
| 61 | `SettingsGenerator` — template substitution (no LLM) | Dev C | `feature/dev-c/settings-generator` |
| 62 | `MigrationsRunner` — manage.py makemigrations + migrate | Dev C | `feature/dev-c/migrations-runner` |

### Frontend generators

| # | Issue | Owner | Branch |
|---|---|---|---|
| 63 | `BaseHTMLTemplate` — fixed DaisyUI + HTMX + navbar template | Dev C | `feature/dev-c/base-html-template` |
| 64 | `DashboardGenerator` — DaisyUI stats + HTMX table | Dev B | `feature/dev-b/dashboard-generator` |
| 65 | `ResourceListGenerator` — DaisyUI table + HTMX form + modal | Dev B | `feature/dev-b/resource-list-generator` |
| 66 | `HTMXPartialGenerator` — `_item.html` partials for HTMX swaps | Dev C | `feature/dev-c/htmx-partial-generator` |
| 67 | Frontend system prompt with DaisyUI class reference | Dev C | `feature/dev-c/frontend-system-prompt` |

### Testing + error loop

| # | Issue | Owner | Branch |
|---|---|---|---|
| 68 | `DjangoTestFileGenerator` — TestCase + APIClient | Dev A | `feature/dev-a/django-test-generator` |
| 69 | `MigrationsRunner` + `TestRunner` — manage.py test | Dev C | `feature/dev-c/test-runner-v2` |
| 70 | `ErrorFeedbackLoop` v2 — Django-specific error parsing | Dev A | `feature/dev-a/error-feedback-v2` |
| 71 | `ConsistencyChecker` v2 — URL names, template tags, model fields | Dev C | `feature/dev-c/consistency-checker-v2` |

### Integration tests (3 real PRDs)

| # | Issue | Owner | Branch |
|---|---|---|---|
| 72 | Integration test: Todo App PRD | Dev C | `test/dev-c/todo-app` |
| 73 | Integration test: Expense Tracker PRD | Dev B | `test/dev-b/expense-tracker` |
| 74 | Integration test: Blog PRD | Dev A | `test/dev-a/blog` |

### Polish + ship

| # | Issue | Owner | Branch |
|---|---|---|---|
| 75 | RAM profiling on medium PRD (5 entities) | Dev A | `fix/dev-a/pipeline-memory-v2` |
| 76 | All Week 1 workflows verified on Django projects | Dev B | `test/dev-b/workflows-on-django` |
| 77 | Safety tests on generated Django projects | Dev C | `test/dev-c/safety-django` |
| 78 | README update + demo recording (todo app) | Dev C | `docs/dev-c/v0.2-readme` |

---

## 12-Day Schedule

### Dev A — Backend generators: models, views, tests, pipeline
### Dev B — Backend generators: serializers, forms, URLs, dashboard, resource pages
### Dev C — Templates: settings, base.html, HTMX partials, consistency checker, test runner

---

### Day 1 — Django project spec + template library

**All 3 devs (morning, 2 hours together)**
- Update `shamsu/types.py`: add `ProjectSpec`, `EntitySpec`, `EndpointSpec`, `PageSpec`, `DjangoFileSpec`
- Update `shamsu/interfaces.py`: update `IFileGenerator` to match Django patterns
- Agree on: how entities map to Django models, how pages map to template views, what the `FileGenOrder` list looks like

**Dev A** → issue #51, #52
- `ProjectSpec` dataclasses finalized
- Django template library: write all fixed-template strings as Python constants in `shamsu/templates/django/`
  - `SETTINGS_TEMPLATE`, `MANAGE_TEMPLATE`, `WSGI_TEMPLATE`, `BASE_HTML_TEMPLATE`, `LOGIN_HTML_TEMPLATE`, `REGISTER_HTML_TEMPLATE`, `REQUIREMENTS_TEMPLATE`
- Substitution points: `{{ project_name }}`, `{{ app_name }}`, `{{ secret_key }}`, `{{ installed_apps }}`

**Dev B** → issue #54
- `DjangoProjectGenerator` pipeline skeleton: 22-step method stubs
- Each step is one method call on a generator class
- TaskStore saves state after each step
- Wire `show_plan → ask_approval` gate between Phase 2 and Phase 3

**Dev C** → issue #61, #63
- `SettingsGenerator`: reads `ProjectSpec` → substitutes into `SETTINGS_TEMPLATE` → writes file
- `BaseHTMLTemplate`: reads `ProjectSpec.pages` → generates nav links → substitutes into `BASE_HTML_TEMPLATE`
- Verify: generated `settings.py` is valid Python (`ast.parse()`), generated `base.html` is valid HTML

**End of Day 1:** Run pipeline Phase 1–3 on a simple PRD. All fixed template files should be generated and valid.

---

### Day 2 — PRD extractor v2 + models.py

**Dev A** → issues #53, #55
- `PRDExtractor` v2: extract entities with field names and Django field types
  - Map PRD types to Django field classes:
    - "text / string / name" → `CharField(max_length=200)`
    - "long text / description / body" → `TextField(blank=True)`
    - "number / count / integer" → `IntegerField()`
    - "price / amount / decimal" → `DecimalField(max_digits=10, decimal_places=2)`
    - "date" → `DateField(null=True, blank=True)`
    - "datetime / timestamp" → `DateTimeField(auto_now_add=True)`
    - "boolean / flag / active" → `BooleanField(default=True)`
    - "belongs to / FK to" → `ForeignKey('Model', on_delete=models.CASCADE)`
  - Detect M2M: "has many ... tags / categories" → `ManyToManyField`
- `ModelFileGenerator`: takes `list[EntitySpec]` → builds context pack → calls Qwen2.5-Coder 7B → validates `ast.parse()` → writes file

**Dev B** → issue #56
- `SerializerFileGenerator`:
  - Context pack: full `models.py` content + entity list (field names must match)
  - Model: Phi-3 Mini (ModelSerializer is simple enough)
  - Validation: `ast.parse()` + check every `fields` list entry exists as a field on the corresponding model class

**Dev C** → issue #62
- `MigrationsRunner`:
  - `run("python manage.py makemigrations", cwd=project_dir)` → check output for errors
  - `run("python manage.py migrate")` → check output for "OK"
  - Parse Django migration output: success vs error
  - On error: feed output to BugFixer with `models.py` in context

**End of Day 2:** Demo: PRD → `models.py` generated and migrations run successfully.

---

### Day 3 — forms.py + views.py (ViewSets) + admin.py

**Dev A** → issue #58
- `ViewFileGenerator` — the most complex generator:
  - Part 1: API ViewSets — takes model + serializer class names from generated files → builds context pack
    ```python
    class TaskViewSet(ModelViewSet):
        queryset = Task.objects.all()
        serializer_class = TaskSerializer
        permission_classes = [IsAuthenticated]
        def get_queryset(self):
            return Task.objects.filter(user=self.request.user)
    ```
  - Part 2: Template views — takes `PageSpec` list → generates `@login_required` + `render()` views
  - Model: Qwen2.5-Coder 7B (needs to correctly reference model and serializer class names)
  - Validation: `ast.parse()` + check all imported class names exist in `models.py` and `serializers.py`

**Dev B** → issues #57, #60
- `FormFileGenerator`:
  - Context pack: `models.py` content + entity list
  - Model: Phi-3 Mini
  - Output: one `ModelForm` per entity with `Meta.fields` list
  - Validation: `ast.parse()` + all listed fields exist in corresponding model

- `AdminFileGenerator`:
  - Template substitution mostly — `admin.site.register(ModelName)` per model
  - Model: Phi-3 Mini (trivial output)
  - Could even be pure template substitution (no LLM at all)

**Dev C** → consistency checker first pass
- `ConsistencyChecker` v2 — after views.py is generated:
  - Check: every `serializer_class = XSerializer` in views.py → `XSerializer` exists in serializers.py
  - Check: every `queryset = X.objects.all()` → `X` exists in models.py
  - Check: every `from .models import X` → `X` is a class in models.py
  - Run checks with `ast.parse()` + symbol extraction — no LLM needed

---

### Day 4 — urls.py + project-level wiring

**Dev A** → integration glue
- After views.py is generated and validated, run ConsistencyChecker
- If inconsistencies found: feed to BugFixer (targeted diff, Phi-3 Mini for simple name fixes)
- Write integration test: generate models → serializers → forms → views → run consistency check → verify all pass

**Dev B** → issue #59
- `URLFileGenerator`:
  - DRF router registration for ViewSets: `router.register('tasks', views.TaskViewSet, basename='task')`
  - `path()` entries for template views: `path('dashboard/', views.dashboard, name='dashboard')`
  - Project-level `urls.py`: `include('app.urls')` + auth URLs + DRF token URLs
  - Model: Phi-3 Mini
  - Validation: `ast.parse()` + all view names referenced exist in `views.py`

**Dev C** → `ConsistencyChecker` URL check
- After `urls.py` is generated:
  - Check: every `path('...', views.X, ...)` → `X` exists in `views.py`
  - Check: every `router.register('...', views.XViewSet, ...)` → `XViewSet` exists in `views.py`
  - Check: all URL names that will be used in templates are registered

**End of Day 4:** Full backend generated. Run `python manage.py check` — must pass with 0 errors. This is the backend done check.

---

### Day 5 — Frontend: dashboard + resource list pages

**Dev B** → issues #64, #65
- `DashboardGenerator`:
  - Context pack: `PageSpec` for dashboard + entity names + URL names from `urls.py`
  - System prompt: include the full DaisyUI class reference (btn, card, stats, table, badge names)
  - Model: Qwen2.5-Coder 7B
  - Output: `dashboard.html` with DaisyUI stats row + recent items table + HTMX loading

- `ResourceListGenerator` (one page per main resource):
  - Context pack: `PageSpec` + model field names + URL names + form class name
  - System prompt: DaisyUI class reference + "use {{ form|crispy }} for all forms"
  - Model: Qwen2.5-Coder 7B
  - Key: HTMX add modal (`hx-post`), HTMX delete (`hx-delete`), DaisyUI table

**Dev C** → issues #67, #66
- Write the frontend system prompt as a reusable constant:
  - Full DaisyUI component reference (all class names the LLM should use)
  - HTMX attribute reference (hx-get, hx-post, hx-delete, hx-target, hx-swap, hx-confirm)
  - Rules: "NEVER write raw <input> tags. ALWAYS use {{ form|crispy }}. ALWAYS use DaisyUI class names."
- `HTMXPartialGenerator`: `_item.html` partial per resource
  - Input: model field names + URL names
  - Model: Phi-3 Mini (partials are ~10 lines)

**Dev A** → frontend consistency check
- Check: every `{% url 'name' %}` in templates → `name` exists in `urls.py`
- Check: every `{{ field_name }}` → `field_name` exists in the corresponding model
- Check: every `hx-target="#id-{{ obj.id }}"` → matching `id="id-{{ obj.id }}"` exists somewhere in the same or partial template

---

### Day 6 — Test file generator + migrations + test runner

**Dev A** → issue #68
- `DjangoTestFileGenerator`:
  - Context pack: `views.py` ViewSet classes + `models.py` for field names + `urls.py` URL names
  - System prompt: "Use Django TestCase + DRF APIClient. Create user in setUp(). Test: list, create, retrieve, update, delete. Verify status codes and response fields."
  - Model: Qwen2.5-Coder 7B
  - Validation: `ast.parse()`

**Dev B** → wire Phase 4 (tests + run) into pipeline
- `DependencyInstaller`: `pip install -r requirements.txt` with approval gate
- Connect `MigrationsRunner` into pipeline (already built by Dev C on Day 2)
- `TestRunner` v2: `python manage.py test --verbosity=2`
  - Parse Django test output: `OK` vs `FAILED (errors=N, failures=M)`
  - Extract: test class name, test method name, assertion error message, traceback file:line
  - Return `TestRunResult` with structured failures

**Dev C** → issue #69, #70
- Wire `MigrationsRunner` between backend generation and frontend generation
- `ErrorFeedbackLoop` v2 for Django:
  - Django test failures look different from pytest: `FAIL: test_create_task (tasks.tests.TaskViewTests)`
  - Extract the test method → find the view it tests → find the model it uses
  - Build targeted context pack: test file + view function + model class + error message
  - BugFixer generates diff → PatchEngine applies → re-run `manage.py test`
  - Max 3 iterations

---

### Day 7 — Full pipeline integration test

**All 3 devs — first real end-to-end run**

Run the complete 22-step pipeline on the Todo App PRD:

```markdown
# Todo App PRD

## Entities
- User: built-in Django auth user
- Task: title (CharField), description (TextField), status (choices: todo/in_progress/done), due_date (DateField), user (FK to User)
- Category: name (CharField), color (CharField), user (FK to User)

## API Endpoints
- POST /api/auth/token/ — get JWT token
- GET/POST /api/tasks/ — list + create tasks
- GET/PUT/DELETE /api/tasks/{id}/ — retrieve, update, delete task
- GET/POST /api/categories/ — list + create categories

## Pages
- /login/ — login form (fixed template)
- /dashboard/ — task stats + recent tasks table
- /tasks/ — full task list with add/delete
- /categories/ — category list with add/delete
```

**Expected output:**
```
{project_name}/
  manage.py ✓
  settings.py ✓
  {app}/
    models.py ✓ (Task, Category)
    serializers.py ✓
    forms.py ✓
    views.py ✓ (TaskViewSet, CategoryViewSet, dashboard view, tasks view)
    urls.py ✓
    admin.py ✓
    templates/
      base.html ✓
      login.html ✓
      dashboard.html ✓
      tasks/task_list.html ✓
      tasks/_task_item.html ✓
      categories/category_list.html ✓
  tests/
    test_tasks.py ✓
    test_categories.py ✓
  requirements.txt ✓
  README.md ✓
```

**Pass criteria:**
- [ ] `python manage.py check` → 0 errors
- [ ] `python manage.py migrate` → OK
- [ ] `python manage.py test` → at least 70% pass first run
- [ ] After error feedback loop → at least 85% pass
- [ ] `python manage.py runserver` starts without errors
- [ ] `/admin/` loads and can add data
- [ ] `/login/` renders correctly
- [ ] `/dashboard/` loads after login
- [ ] `/tasks/` shows table with HTMX add + delete working
- [ ] DaisyUI components visible and consistent across pages

**File any failures as issues immediately. Assign owners. Fix before Day 8.**

---

### Day 8 — Fix Day 7 failures + second PRD test

**Dev A** → fix backend generator bugs from Day 7
- Common issues: model field type mapping wrong, ForeignKey `on_delete` missing, `get_queryset` filtering wrong

**Dev B** → fix frontend generator bugs from Day 7
- Common issues: wrong `{% url %}` names, missing `{% load crispy_forms_tags %}`, HTMX target IDs not matching

**Dev C** → run Expense Tracker PRD (more complex: 4 entities, M2M relationship)
```markdown
# Expense Tracker PRD

## Entities
- User: Django auth
- Expense: amount (Decimal), description (CharField), date (DateField), category (FK), user (FK)
- Budget: name (CharField), limit (Decimal), month (DateField), user (FK)
- Category: name (CharField), icon (CharField), user (FK)

## Pages
- /dashboard/ — monthly spending summary stats, recent expenses
- /expenses/ — expense list, add expense form
- /budgets/ — budget list with progress bars
```

This PRD tests: Decimal fields, budget vs expense relationship, progress bar UI component (DaisyUI `progress`)

---

### Day 9 — All 6 Week 1 workflows verified on Django projects

The Week 1 workflows (Q&A, code edit, bug fix, audit, test gen, doc gen) must all work on generated Django projects.

**Dev A** → verify Q&A + code edit
- `shamsu > How does the Task model relate to User?` → BM25 finds `models.py`, Q&A explains ForeignKey
- `shamsu > Add a priority field to Task with choices low/medium/high` → code edit generates diff touching `models.py` + `serializers.py` + `forms.py`
  - **Important:** after applying patch, must auto-run `makemigrations` to pick up the new field

**Dev B** → verify bug fix + audit
- `shamsu > The task creation endpoint returns 400 for valid data` → BugFixer reads `views.py`, `serializers.py`, the test output → generates targeted fix
- `shamsu > Audit this Django project for security issues` → Audit finds: DEBUG=True in settings, no rate limiting, no CSRF exemptions

**Dev C** → verify test gen + doc gen + safety
- `shamsu > Write tests for the CategoryViewSet` → TestAgent generates `test_categories.py` using APIClient
- `shamsu > Generate README for this project` → DocAgent reads models, views, URLs → generates accurate README
- Safety: verify path traversal blocked on Django project, secret detection finds `SECRET_KEY` in logs before it's added to `DEFAULT_IGNORE`

---

### Day 10 — Blog PRD + RAM profiling

**Dev A** → issue #75, #74 (Blog PRD integration test)

Blog PRD (most complex test: 4 entities, public + private pages, tags M2M):
```markdown
## Entities
- User: Django auth
- Post: title, body (TextField), published (Boolean), author (FK User), created_at (auto)
- Comment: body, post (FK), author (FK User), created_at (auto)
- Tag: name (CharField)
- Post.tags → ManyToManyField(Tag)

## Pages
- / — public post list (no login required)
- /posts/{id}/ — public post detail + comments
- /dashboard/ — my posts list (login required)
- /posts/new/ — create post form (login required)
```

This PRD tests: public (no `@login_required`) vs private views, ManyToManyField generation, the public-facing design.

**RAM profiling during Blog PRD generation:**
- Use `tracemalloc` throughout the 22-step pipeline
- Record peak RAM at each step
- Target: under 7 GB peak at any point
- Document results in `BENCHMARK.md`

**Dev B** → multi-model switching performance
- Measure time for each model swap (Phi-3 → Qwen2.5-Coder → Phi-3)
- Target: model swap under 8 seconds on 8 GB machine
- If swaps are slow: consider generating all Qwen2.5-Coder files in one batch before unloading

**Dev C** → theme selection logic
- SHAMSU reads the PRD's industry/domain and picks a DaisyUI theme:
  - Finance/business PRD → `data-theme="corporate"` or `"business"`
  - Creative/blog PRD → `data-theme="light"` or `"nord"`
  - Technical/developer PRD → `data-theme="dark"` or `"dracula"`
  - Health/wellness PRD → `data-theme="cupcake"`
- Theme selection: simple keyword matching from PRD → theme name, no LLM needed

---

### Day 11 — Integration testing + full MVP checklist

**Dev A — verifies core engine on all 3 generated projects:**
- [ ] `shamsu init` on each: file walker finds all `.py` and `.html` files
- [ ] Symbol lookup finds all model classes, view classes, URL names
- [ ] Incremental re-index: add a field to a model, re-index, confirm new field found
- [ ] Resume: kill SHAMSU mid-generation on Blog PRD, restart, verify resume works
- [ ] All files generated with valid syntax (run `ast.parse()` on all `.py` files, `html.parser` on all `.html` files)

**Dev B — verifies all LLM workflows on all 3 generated projects:**
- [ ] Q&A: `how does auth work?` → finds auth views, explains session auth
- [ ] Code edit: `add email field to User profile` → diff touches models + serializers + forms
- [ ] Bug fix: break a serializer field name intentionally → bug fix finds and repairs it
- [ ] Audit: finds at least 2 real issues (DEBUG=True, no input length limits)
- [ ] Test gen: generates working APIClient test for any ViewSet
- [ ] Doc gen: generates accurate README from indexed project

**Dev C — verifies safety + CLI on all 3 generated projects:**
- [ ] Path traversal attempt blocked
- [ ] `rm -rf` blocked
- [ ] `SECRET_KEY` value redacted in all log files
- [ ] `shamsu status` shows correct file + symbol counts
- [ ] `shamsu log` shows the generation history
- [ ] Friendly error when PRD file not found (not a Python traceback)
- [ ] All 3 generated projects: `python manage.py runserver` starts + `/admin/` loads + login works

---

### Day 12 — Ship v0.2.0

**Dev A**
- Final merge, conflict resolution
- Full test suite on clean Python env: `python -m venv .venv && pip install -e ".[dev]" && pytest`
- Benchmark report: time + peak RAM for each of the 3 test PRDs
- Tag: `git tag -a v0.2.0 -m "SHAMSU v0.2.0 — PRD to Django web project"`

**Dev B**
- Record the demo:
  1. Write `LIBRARY_PRD.md` (books, authors, borrowing)
  2. Run `shamsu > generate project from LIBRARY_PRD.md`
  3. Show all 22 steps with progress output
  4. `python manage.py runserver` → open `/admin/` → add a book → open `/books/` → show the table
  5. Record as `asciinema` terminal video
- This is the money shot for the README and any showcase

**Dev C**
- Update `README.md`:
  - Updated tech stack section (Django + DRF + DaisyUI + HTMX)
  - PRD format guide (what section headers SHAMSU looks for)
  - How to run the generated project
  - DaisyUI theme customization (one HTML attribute)
  - Troubleshooting: what to do if `makemigrations` fails
- `CHANGELOG.md` entry for v0.2.0
- `WEEK2_REPORT.md`: what worked, what was harder than expected, what changes for v0.3

---

## Milestone v0.2.0 Success Criteria

All must pass before tagging.

**Dev A verifies — generation + engine:**
- [ ] Todo App PRD → runnable Django project in under 10 minutes on 8 GB machine
- [ ] Expense Tracker PRD → runnable Django project
- [ ] Blog PRD → runnable Django project
- [ ] `python manage.py check` → 0 errors on all 3 generated projects
- [ ] `python manage.py test` → ≥80% pass after error feedback loop
- [ ] Peak RAM under 7 GB throughout generation pipeline
- [ ] Resume works: kill mid-generation, restart, pipeline continues from last step

**Dev B verifies — all workflows:**
- [ ] Q&A, code edit, bug fix, audit, test gen, doc gen all work on generated Django projects
- [ ] Code edit on generated project correctly auto-runs `makemigrations` after model changes
- [ ] PRD → project generation is fully autonomous (no manual intervention needed for clean PRDs)

**Dev C verifies — safety + design + CLI:**
- [ ] Generated projects use DaisyUI consistently — no raw Tailwind utilities in generated templates
- [ ] `{{ form|crispy }}` used for all forms — no manually written `<input>` tags
- [ ] All safety tests pass (path traversal, blocked commands, secret redaction)
- [ ] `python manage.py runserver` on generated project → `/admin/` loads, login works, main pages render
- [ ] DaisyUI theme correctly selected from PRD domain

---

## PRD Format Guide (what SHAMSU looks for)

Tell users to structure their PRD with these headers:

```markdown
# Project Name

## Overview
One paragraph. Domain, users, purpose.

## Entities / Data Models
- **Task**: title (text), description (long text), status (choices: todo/in_progress/done),
            due_date (date, optional), user (belongs to User)
- **Category**: name (text), color (text), user (belongs to User)

## API Endpoints (optional — SHAMSU infers from entities if missing)
- GET/POST /api/tasks/ — list and create
- GET/PUT/DELETE /api/tasks/{id}/ — retrieve, update, delete

## Pages / Screens
- **Dashboard**: show task count stats + recent tasks table
- **Task list**: show all tasks, add new task, delete tasks
- **Category list**: show all categories, add/delete

## Non-Functional Requirements (optional)
- Login required on all pages except the homepage
- Admin panel needed for data management
```

SHAMSU uses these sections to extract structured data without LLM calls:
- `## Entities` → `models.py` field types
- `## API Endpoints` → ViewSet + URL registration
- `## Pages` → template views + HTML pages
- `## Non-Functional Requirements` → `@login_required`, permissions, `DEBUG` setting

---

## v0.3 Backlog

- PostgreSQL option (change one settings.py line, same ORM code)
- Docker + docker-compose generation
- Celery + Redis for background tasks
- Django Channels for WebSockets
- React SPA option (for PRDs that explicitly ask for it)
- Multi-tenant architecture
- File upload handling (Pillow + Django FileField)
- Email sending (Django email backend)
- Deployment config (nginx + gunicorn + systemd)

---

*SHAMSU — Killer of API Bills from the Big Giants*
*v0.2.0 — PRD → Django + DRF + DaisyUI + HTMX full web project*
