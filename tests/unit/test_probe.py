"""Everything the write gate learned from one live build.

Four turns of a task-tracker build, each failure caught by running the code
rather than by reading it. In order:

1. `def greet(name): return 'Hello, {}!'` — reported COMPLETE. Parses, so the
   syntax check would not have saved it; it is why this module's docstring
   says silence is not a certificate.
2. `from storage import Storage` where the file is `Storage.py`.
   `ModuleNotFoundError`, one capital letter away.
3. `from tasks import Storage, TaskList` — module real, symbol not in it.
   `ImportError`.
4. Refused twice on the import, the agent stopped importing `Storage` at all
   and kept calling it. `NameError`, and the task reported COMPLETE.

A fifth arrived from the OpenBazaar build, and is the reason `_redefinitions`
exists: asked to *add* a model to a file, the agent appended the same class
four times over. See `TestItMustNotDefineTheSameThingTwice`.

Each check refuses *before* the write, so a broken file never reaches disk and
the retry is clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.verification.probe import probe_syntax


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "Storage.py").write_text(
        "class Storage:\n    def load(self): return []\n", encoding="utf-8"
    )
    (tmp_path / "tasks.py").write_text(
        "class TaskList:\n    def all(self): return []\n", encoding="utf-8"
    )
    return tmp_path


class TestItMustParse:
    def test_truncated_output_is_refused(self) -> None:
        """The failure mode of a small model: it stops mid-structure."""
        problem = probe_syntax("x.py", "def f():\n    return {1: 2\n")
        assert problem is not None
        assert "not valid Python" in problem

    def test_the_offending_line_comes_back(self) -> None:
        """Given only "invalid syntax" a 7B rewrites the file; given the line, it fixes it."""
        problem = probe_syntax("x.py", "x = 1\ny = (2\nz = 3\n")
        assert problem is not None and "line" in problem

    def test_a_stray_markdown_fence_is_refused(self) -> None:
        assert probe_syntax("x.py", "```python\nx = 1\n```\n") is not None

    def test_valid_python_passes(self) -> None:
        assert probe_syntax("x.py", "def f(n):\n    return n + 1\n") is None

    def test_broken_json_is_refused(self) -> None:
        assert probe_syntax("p.json", '{"a": 1,}\n') is not None

    def test_an_unknown_extension_gets_no_opinion(self) -> None:
        """`None` here means "not judged", which is why it is never evidence."""
        assert probe_syntax("notes.md", "{{{ not anything\n") is None


class TestEveryNameMustExist:
    def test_a_name_never_defined_is_refused(self) -> None:
        problem = probe_syntax("cli.py", "def main():\n    s = Storage('t.json')\n    return s\n")
        assert problem is not None
        assert "'Storage'" in problem and "line 2" in problem

    def test_importing_it_settles_the_objection(self) -> None:
        assert (
            probe_syntax(
                "cli.py", "from Storage import Storage\ndef main():\n    return Storage('t')\n"
            )
            is None
        )

    @pytest.mark.parametrize(
        "source",
        [
            "class A:\n    def f(self, x):\n        return self.y + x\n",
            "xs = [1]\nys = [i * 2 for i in xs]\nprint(ys)\n",
            "print(len(str(TypeError)))\n",
            "import io\nwith io.StringIO() as f:\n    if (n := f.write('x')):\n        print(n)\n",
            "import functools\n@functools.cache\ndef f():\n    return 1\n",
            "try:\n    x = 1\nexcept ValueError as e:\n    print(e)\n",
            "def f(*args, **kwargs):\n    return args, kwargs\n",
            "import os.path\nprint(os.path.sep)\n",
            "print(__name__)\n",
            "def f():\n    global g\n    g = 1\n",
        ],
    )
    def test_ordinary_python_is_never_refused(self, source: str) -> None:
        """The check errs weak on purpose — a false refusal blocks correct work."""
        assert probe_syntax("x.py", source) is None, source


class TestLocalImportsMustResolve:
    def test_a_case_mismatch_is_caught(self, workspace: Path) -> None:
        """Windows hides this from the filesystem; `import` stays case-sensitive."""
        problem = probe_syntax("cli.py", "from storage import Storage\n", workspace=workspace)
        assert problem is not None
        assert "Storage.py" in problem

    def test_a_symbol_the_module_lacks_is_caught(self, workspace: Path) -> None:
        problem = probe_syntax("cli.py", "from tasks import Storage\n", workspace=workspace)
        assert problem is not None
        assert "tasks.py does not define Storage" in problem

    def test_the_message_names_the_file_that_has_it(self, workspace: Path) -> None:
        """Without this the model retried the identical bad import twice."""
        problem = probe_syntax("cli.py", "from tasks import Storage\n", workspace=workspace)
        assert problem is not None
        assert "from Storage import Storage" in problem

    def test_it_never_points_at_the_file_being_written(self, workspace: Path) -> None:
        """A half-written `cli.py` importing TaskList once made it its own source."""
        (workspace / "cli.py").write_text("from tasks import TaskList\n", encoding="utf-8")
        problem = probe_syntax(
            "cli.py", "from tasks import Storage, TaskList\n", workspace=workspace
        )
        assert problem is not None
        assert "in cli.py" not in problem

    def test_correct_imports_pass(self, workspace: Path) -> None:
        source = "from Storage import Storage\nfrom tasks import TaskList\n"
        assert probe_syntax("cli.py", source, workspace=workspace) is None

    def test_third_party_and_stdlib_are_not_judged(self, workspace: Path) -> None:
        """Whether `requests` is installed is not knowable from the workspace."""
        source = "import argparse\nimport requests\nfrom flask import Flask\n"
        assert probe_syntax("cli.py", source, workspace=workspace) is None

    def test_a_star_import_is_not_second_guessed(self, workspace: Path) -> None:
        assert probe_syntax("cli.py", "from tasks import *\n", workspace=workspace) is None

    def test_a_re_export_counts_as_provided(self, workspace: Path) -> None:
        """`from api import X` is legitimate when `api` imports X itself."""
        (workspace / "api.py").write_text("from Storage import Storage\n", encoding="utf-8")
        assert probe_syntax("cli.py", "from api import Storage\n", workspace=workspace) is None

    def test_without_a_workspace_imports_are_not_checked(self) -> None:
        """The check needs to know what is on disk; absent that it says nothing."""
        assert probe_syntax("cli.py", "from nowhere import Thing\n") is None


class TestItMustNotDefineTheSameThingTwice:
    """The append that would not stop, from the OpenBazaar build.

    Asked to add an `Item` model to a file already holding `User` and
    `Category`, the agent appended a full `class Item` — four times, each a
    slightly different draft. Every write was valid Python and every one earned
    `file_changed`; the module died at import with
    `NameError: name 'Item' is not defined`, and the run reported COMPLETE.
    """

    def test_a_class_appended_twice_is_refused(self) -> None:
        source = (
            "from django.db import models\n\n\n"
            "class Item(models.Model):\n    title = models.CharField(max_length=150)\n\n\n"
            "class Item(models.Model):\n    title = models.CharField(max_length=150)\n"
        )
        problem = probe_syntax("marketplace/models.py", source)
        assert problem is not None
        assert "'Item' twice" in problem

    def test_it_names_both_lines(self) -> None:
        """A 7B told only "duplicate class" rewrites the file; given the lines, it edits."""
        source = "class A:\n    pass\n\n\nclass A:\n    pass\n"
        problem = probe_syntax("m.py", source)
        assert problem is not None
        assert "line 1" in problem and "line 5" in problem

    def test_it_says_not_to_append_again(self) -> None:
        """The refusal has to redirect the behaviour that caused it."""
        source = "class A:\n    pass\n\n\nclass A:\n    pass\n"
        problem = probe_syntax("m.py", source)
        assert problem is not None
        assert "do not append another copy" in problem

    def test_functions_count_too(self) -> None:
        source = "def run():\n    return 1\n\n\ndef run():\n    return 2\n"
        assert probe_syntax("m.py", source) is not None

    def test_the_honest_file_passes(self) -> None:
        """The shape the build was actually trying to reach."""
        source = (
            "from django.db import models\n\n\n"
            "class User(models.Model):\n    pass\n\n\n"
            "class Category(models.Model):\n    pass\n\n\n"
            "class Item(models.Model):\n    pass\n"
        )
        assert probe_syntax("marketplace/models.py", source) is None


class TestRedefinitionHasToStayQuietWhereRepeatingIsNormal:
    """Every one of these is ordinary Python. A write gate that refused them
    would be worse than the bug it was added for."""

    def test_a_method_repeated_in_a_class_body_is_not_top_level(self) -> None:
        source = "class A:\n    def f(self): ...\n    def f(self): ...\n"
        assert probe_syntax("m.py", source) is None

    def test_a_property_and_its_setter_are_untouched(self) -> None:
        source = (
            "class A:\n"
            "    @property\n    def x(self): return self._x\n"
            "    @x.setter\n    def x(self, v): self._x = v\n"
        )
        assert probe_syntax("m.py", source) is None

    def test_one_definition_per_branch_is_fine(self) -> None:
        source = (
            "import sys\n\n"
            "if sys.version_info >= (3, 11):\n    class A:\n        pass\n"
            "else:\n    class A:\n        pass\n"
        )
        assert probe_syntax("m.py", source) is None

    def test_an_import_fallback_is_fine(self) -> None:
        source = "try:\n    from fast import A\nexcept ImportError:\n    class A:\n        pass\n"
        assert probe_syntax("m.py", source) is None

    def test_overloads_are_the_intended_spelling(self) -> None:
        source = (
            "from typing import overload\n\n"
            "@overload\ndef f(x: int) -> int: ...\n"
            "@overload\ndef f(x: str) -> str: ...\n"
            "def f(x): return x\n"
        )
        assert probe_syntax("m.py", source) is None

    def test_qualified_overloads_too(self) -> None:
        source = (
            "import typing\n\n"
            "@typing.overload\ndef f(x: int) -> int: ...\n"
            "@typing.overload\ndef f(x: str) -> str: ...\n"
            "def f(x): return x\n"
        )
        assert probe_syntax("m.py", source) is None

    def test_rebinding_a_module_level_name_is_ordinary(self) -> None:
        """Only definitions are checked; assignment is not."""
        assert probe_syntax("m.py", "X = 1\nX = 2\n") is None


class TestAByteOrderMarkIsNotASyntaxError:
    """A BOM wedged a live build until nothing could be written to the file.

    `compile()` rejects U+FEFF in a decoded string, but the interpreter reads
    source as `utf-8-sig` and imports a BOM'd module perfectly well. On Windows
    the mark arrives by accident constantly — Notepad, Visual Studio, and
    PowerShell 5.1's `Set-Content -Encoding utf8` all write one.

    Once present, every append to that file was refused with "invalid
    non-printable character U+FEFF at line 1": a complaint about bytes the
    model never wrote, at a line an append cannot reach. Ten tool calls, then
    the task blocked.
    """

    BOM = "\ufeff"

    def test_a_leading_mark_is_not_an_objection(self) -> None:
        assert probe_syntax("m.py", self.BOM + "VALUE = 42\n") is None

    def test_the_file_is_still_checked_after_it(self) -> None:
        """Stripping the mark must not become a way to skip the parse."""
        problem = probe_syntax("m.py", self.BOM + "def f():\n    return {1: 2\n")
        assert problem is not None
        assert "not valid Python" in problem

    def test_a_mark_in_the_middle_is_still_refused(self) -> None:
        """Only the leading mark is an encoding marker; the rest is content."""
        assert probe_syntax("m.py", "x = 1\n" + self.BOM + "y = 2\n") is not None

    def test_json_tolerates_it_too(self) -> None:
        assert probe_syntax("p.json", self.BOM + '{"a": 1}\n') is None

    def test_the_redefinition_check_still_sees_through_it(self) -> None:
        source = self.BOM + "class A:\n    pass\n\n\nclass A:\n    pass\n"
        assert probe_syntax("m.py", source) is not None


class TestAClassCannotUseItsOwnNameYet:
    """Twice in one build the 7B nested a model inside another and wired them.

        class Item(models.Model):
            class Pricing(models.Model):
                item = models.OneToOneField(Item, on_delete=models.CASCADE)

    Valid syntax; `_undefined_names` is scope-blind so `Item` counted as bound.
    Django died at import with "name 'Item' is not defined", and the step had
    already reported COMPLETE.
    """

    def test_a_nested_class_referring_outward_is_refused(self) -> None:
        source = (
            "from django.db import models\n\n\n"
            "class Item(models.Model):\n"
            "    title = models.CharField(max_length=150)\n\n"
            "    class Pricing(models.Model):\n"
            "        item = models.OneToOneField(Item, on_delete=models.CASCADE)\n"
        )
        problem = probe_syntax("marketplace/models.py", source)
        assert problem is not None
        assert "'Item' uses its own name" in problem
        assert "line 8" in problem, "the offending line is the field, not the nested class"

    def test_the_message_names_the_error_it_prevents(self) -> None:
        source = "class A:\n    x = A\n"
        problem = probe_syntax("m.py", source)
        assert problem is not None
        assert "NameError" in problem

    def test_a_direct_self_reference_in_the_body_is_refused(self) -> None:
        assert probe_syntax("m.py", "class A:\n    alias = A\n") is not None

    def test_a_default_argument_is_evaluated_immediately(self) -> None:
        """Defaults run at def time, which is still inside the class body."""
        assert probe_syntax("m.py", "class A:\n    def f(self, x=A): ...\n") is not None


class TestSelfReferenceMustAllowTheNormalIdioms:
    def test_a_method_may_name_its_own_class(self) -> None:
        """The ordinary factory/classmethod shape — runs long after binding."""
        source = "class A:\n    @classmethod\n    def make(cls):\n        return A()\n"
        assert probe_syntax("m.py", source) is None

    def test_a_deep_method_body_is_still_deferred(self) -> None:
        source = "class A:\n    def f(self):\n        if True:\n            return [A]\n"
        assert probe_syntax("m.py", source) is None

    def test_an_annotation_may_name_the_class(self) -> None:
        source = "from __future__ import annotations\n\n\nclass A:\n    parent: A\n"
        assert probe_syntax("m.py", source) is None

    def test_a_string_forward_reference_is_fine(self) -> None:
        """What Django actually wants: models.ForeignKey('self') or a name."""
        source = (
            "from django.db import models\n\n\n"
            "class Category(models.Model):\n"
            "    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True)\n"
        )
        assert probe_syntax("m.py", source) is None

    def test_a_lambda_body_is_deferred(self) -> None:
        assert probe_syntax("m.py", "class A:\n    f = lambda self: A\n") is None

    def test_a_sibling_class_may_be_named(self) -> None:
        """Only the *enclosing* class is unbound; one defined above is fine."""
        source = "class A:\n    pass\n\n\nclass B:\n    other = A\n"
        assert probe_syntax("m.py", source) is None


class TestAnImportMustNotShadowThisFilesOwnClass:
    """From the same file: `class User(AbstractUser)` at line 5, and then a
    later append added `from django.contrib.auth.models import User` — quietly
    replacing the project's own user model with Django's."""

    def test_an_import_rebinding_a_defined_class_is_refused(self) -> None:
        source = (
            "from django.contrib.auth.models import AbstractUser\n\n\n"
            "class User(AbstractUser):\n    pass\n\n\n"
            "from django.contrib.auth.models import User\n"
        )
        problem = probe_syntax("marketplace/models.py", source)
        assert problem is not None
        assert "rebinds 'User'" in problem
        assert "line 4" in problem and "line 8" in problem

    def test_it_says_to_remove_the_import_not_to_stop_appending(self) -> None:
        """Shadowing needs different advice from a duplicate definition."""
        source = "class User:\n    pass\n\n\nfrom django.contrib.auth.models import User\n"
        problem = probe_syntax("m.py", source)
        assert problem is not None
        assert "Remove that import" in problem

    def test_importing_then_subclassing_under_the_same_name_is_fine(self) -> None:
        """The common idiom, and the opposite order — it must stay allowed."""
        source = "from base import Widget\n\n\nclass Widget(Widget):\n    pass\n"
        assert probe_syntax("m.py", source) is None

    def test_an_aliased_import_does_not_collide(self) -> None:
        source = (
            "class User:\n    pass\n\n\nfrom django.contrib.auth.models import User as DjUser\n"
        )
        assert probe_syntax("m.py", source) is None


class TestASiblingModuleNeedsARelativeImport:
    """Python 3 has no implicit relative import, and a 7B keeps assuming it does.

    From the OpenBazaar build, appended to `marketplace/services.py`:

        from models import Order

    `marketplace/models.py` is right beside it, so the intent is obvious and
    the result is `ModuleNotFoundError: No module named 'models'`. The whole
    module stopped importing, and the step reported COMPLETE.

    `_local_imports` had globbed only the workspace *root*, so in any project
    organised as a package — which is most of them — it could not see the
    sibling at all and assumed `models` was a third-party package.
    """

    @pytest.fixture
    def package(self, tmp_path: Path) -> Path:
        app = tmp_path / "marketplace"
        app.mkdir()
        (app / "__init__.py").write_text("", encoding="utf-8")
        (app / "models.py").write_text("class Order:\n    pass\n", encoding="utf-8")
        return tmp_path

    def test_a_bare_sibling_import_is_refused(self, package: Path) -> None:
        problem = probe_syntax(
            "marketplace/services.py", "from models import Order\n", workspace=package
        )
        assert problem is not None
        assert "ModuleNotFoundError" in problem

    def test_it_gives_the_relative_form(self, package: Path) -> None:
        """Naming the fix is what turns a refusal into an instruction."""
        problem = probe_syntax(
            "marketplace/services.py", "from models import Order\n", workspace=package
        )
        assert problem is not None
        assert "from .models import" in problem

    def test_a_plain_import_of_a_sibling_is_caught_too(self, package: Path) -> None:
        problem = probe_syntax("marketplace/services.py", "import models\n", workspace=package)
        assert problem is not None

    def test_the_relative_import_passes(self, package: Path) -> None:
        source = "from .models import Order\n\n\ndef f():\n    return Order\n"
        assert probe_syntax("marketplace/services.py", source, workspace=package) is None

    def test_the_fully_qualified_import_passes(self, package: Path) -> None:
        source = "from marketplace.models import Order\n\n\ndef f():\n    return Order\n"
        assert probe_syntax("marketplace/services.py", source, workspace=package) is None

    def test_a_third_party_import_is_untouched(self, package: Path) -> None:
        source = "from django.db import models\n\n\ndef f():\n    return models\n"
        assert probe_syntax("marketplace/services.py", source, workspace=package) is None

    def test_a_file_does_not_count_as_its_own_sibling(self, package: Path) -> None:
        """Writing `marketplace/models.py` must not be told to import itself."""
        source = "import models\n"
        assert probe_syntax("marketplace/models.py", source, workspace=package) is None

    def test_a_root_level_file_is_judged_as_before(self, package: Path) -> None:
        """At the root, top-level and sibling are the same thing."""
        (package / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        source = "from helper import VALUE\n\n\ndef f():\n    return VALUE\n"
        assert probe_syntax("main.py", source, workspace=package) is None
