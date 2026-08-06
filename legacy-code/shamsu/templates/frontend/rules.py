"""Reusable frontend prompt rules for Django template generation."""
from __future__ import annotations

FRONTEND_PROMPT_RULES = """Frontend rules for Django templates:
- Use DaisyUI semantic classes for layout and controls, such as btn, btn-primary,
  card, card-body, navbar, menu, alert, badge, table, input, select, textarea,
  modal, tabs, stats, and hero.
- Prefer HTMX attributes for lightweight interactions when the request needs
  partial updates; do not require Node, npm, bundlers, or a frontend build step.
- Use crispy forms rendering for Django forms. Do not hand-code raw <input>,
  <select>, or <textarea> fields when {{ form|crispy }} or a crispy form helper
  can render them.
- Keep templates accessible with semantic HTML, labels where fields are manual,
  and clear button text.
- Extend base.html for app pages and preserve {% block title %} and
  {% block content %} structure.
"""


def frontend_prompt_rules() -> str:
    return FRONTEND_PROMPT_RULES
