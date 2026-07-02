"""Static consistency checks for generated Django backend files."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from shamsu.types import ProjectSpec


@dataclass
class ConsistencyDiagnostic:
    file_path: str
    symbol: str
    message: str


class BackendConsistencyChecker:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)

    def check(self, project: ProjectSpec) -> list[ConsistencyDiagnostic]:
        app = project.app_name
        diagnostics: list[ConsistencyDiagnostic] = []
        models_tree = self._parse(app, "models.py", diagnostics)
        serializers_tree = self._parse(app, "serializers.py", diagnostics)
        forms_tree = self._parse(app, "forms.py", diagnostics)
        views_tree = self._parse(app, "views.py", diagnostics)
        urls_tree = self._parse(app, "urls.py", diagnostics)
        admin_tree = self._parse(app, "admin.py", diagnostics)
        if diagnostics:
            return diagnostics

        model_fields = _model_fields(models_tree)
        model_names = set(model_fields)
        serializer_names = _class_names(serializers_tree)
        form_names = _class_names(forms_tree)
        view_names = _class_names(views_tree) | _function_names(views_tree)

        diagnostics.extend(_check_meta_fields(f"{app}/serializers.py", serializers_tree, model_fields))
        diagnostics.extend(_check_meta_fields(f"{app}/forms.py", forms_tree, model_fields))
        diagnostics.extend(_check_view_references(f"{app}/views.py", views_tree, model_names, serializer_names, form_names))
        diagnostics.extend(_check_url_references(f"{app}/urls.py", urls_tree, view_names))
        diagnostics.extend(_check_admin_references(f"{app}/admin.py", admin_tree, model_names))
        return diagnostics

    def _parse(
        self,
        app_name: str,
        file_name: str,
        diagnostics: list[ConsistencyDiagnostic],
    ) -> ast.Module:
        relative = f"{app_name}/{file_name}"
        path = self.project_root / relative
        try:
            return ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except FileNotFoundError:
            diagnostics.append(ConsistencyDiagnostic(relative, file_name, "Generated file is missing."))
        except SyntaxError as exc:
            diagnostics.append(ConsistencyDiagnostic(relative, file_name, f"Syntax error: {exc.msg}"))
        return ast.Module(body=[], type_ignores=[])


def _model_fields(tree: ast.Module) -> dict[str, set[str]]:
    models: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            fields = {"id"}
            for statement in node.body:
                if isinstance(statement, ast.Assign):
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            fields.add(target.id)
            models[node.name] = fields
    return models


def _class_names(tree: ast.Module) -> set[str]:
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def _function_names(tree: ast.Module) -> set[str]:
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def _check_meta_fields(
    file_path: str,
    tree: ast.Module,
    model_fields: dict[str, set[str]],
) -> list[ConsistencyDiagnostic]:
    diagnostics: list[ConsistencyDiagnostic] = []
    for class_node in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
        model_name = ""
        fields: list[str] = []
        for node in ast.walk(class_node):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "model":
                        model_name = _name(node.value)
                    if isinstance(target, ast.Name) and target.id == "fields":
                        fields = _string_list(node.value)
        if not model_name:
            continue
        if model_name not in model_fields:
            diagnostics.append(
                ConsistencyDiagnostic(file_path, class_node.name, f"References missing model {model_name}.")
            )
            continue
        for field in fields:
            if field not in model_fields[model_name]:
                diagnostics.append(
                    ConsistencyDiagnostic(
                        file_path,
                        class_node.name,
                        f"Field '{field}' is not defined on model {model_name}.",
                    )
                )
    return diagnostics


def _check_view_references(
    file_path: str,
    tree: ast.Module,
    model_names: set[str],
    serializer_names: set[str],
    form_names: set[str],
) -> list[ConsistencyDiagnostic]:
    diagnostics: list[ConsistencyDiagnostic] = []
    known = model_names | serializer_names | form_names | {"UserCreationForm"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and (
            node.id.endswith("Serializer") or node.id.endswith("Form") or node.id in model_names
        ):
            if node.id not in known:
                diagnostics.append(
                    ConsistencyDiagnostic(file_path, node.id, f"References missing symbol {node.id}.")
                )
    return diagnostics


def _check_url_references(
    file_path: str,
    tree: ast.Module,
    view_names: set[str],
) -> list[ConsistencyDiagnostic]:
    diagnostics: list[ConsistencyDiagnostic] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "views":
            if node.attr not in view_names:
                diagnostics.append(
                    ConsistencyDiagnostic(file_path, node.attr, f"URL references missing view {node.attr}.")
                )
    return diagnostics


def _check_admin_references(
    file_path: str,
    tree: ast.Module,
    model_names: set[str],
) -> list[ConsistencyDiagnostic]:
    diagnostics: list[ConsistencyDiagnostic] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr != "register":
                continue
            for arg in node.args:
                name = _name(arg)
                if name and name not in model_names:
                    diagnostics.append(
                        ConsistencyDiagnostic(file_path, name, f"Admin registers missing model {name}.")
                    )
    return diagnostics


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _string_list(node: ast.AST) -> list[str]:
    if isinstance(node, (ast.List, ast.Tuple)):
        return [value.value for value in node.elts if isinstance(value, ast.Constant) and isinstance(value.value, str)]
    return []
