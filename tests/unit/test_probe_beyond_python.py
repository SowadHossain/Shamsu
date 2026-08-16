"""The write gate outside Python, which is where it did not exist.

Building the same PRD as a Node/React/PostgreSQL project instead of a Django
one made the hole obvious. `probe_syntax` understood `.py` and `.json` and
nothing else, so for every other file in the repository it returned `None` —
which the caller cannot distinguish from "fine":

    probe on a 2-line comment stub .sql  ->  None
    probe on pure garbage .sql           ->  None
    probe on an empty .js                ->  None
    probe on `function f() { return {`   ->  None

Asked to reproduce the PRD's schema into `db/schema.sql`, the model wrote two
comment lines saying it would, earned `file_changed` and `git_diff_reviewed`,
and the run reported COMPLETE with no schema in the repository at all.
"""

from __future__ import annotations

import pytest

from shamsu.verification.probe import probe_syntax


class TestAFileOfPureCommentaryIsNotAnImplementation:
    def test_the_sql_stub_that_caused_this_is_refused(self) -> None:
        source = (
            "-- This file will contain the PostgreSQL schema for the OpenBazaar Marketplace\n"
            "-- Generated from OpenBazaar_Marketplace_PRD.docx, Section 5.2\n"
        )
        problem = probe_syntax("db/schema.sql", source)
        assert problem is not None
        assert "every line in it is a comment" in problem

    @pytest.mark.parametrize(
        ("path", "source"),
        [
            ("src/server.js", "// TODO: implement the Express server\n"),
            ("src/db.ts", "/* the pool goes here */\n"),
            ("app/models.py", "# This module will hold the models\n"),
            ("styles/main.css", "/* styles to follow */\n"),
            ("docker-compose.yml", "# services will be defined here\n"),
            ("Makefile.toml", "# targets to be added\n"),
        ],
    )
    def test_stubs_are_refused_across_languages(self, path: str, source: str) -> None:
        assert probe_syntax(path, source) is not None, path

    def test_one_real_line_is_enough_to_pass(self) -> None:
        source = "-- the users table\nCREATE TABLE users (id UUID PRIMARY KEY);\n"
        assert probe_syntax("db/schema.sql", source) is None

    def test_a_comment_after_real_code_is_fine(self) -> None:
        source = "CREATE TABLE users (id UUID PRIMARY KEY);\n-- more to come\n"
        assert probe_syntax("db/schema.sql", source) is None

    def test_an_empty_file_is_left_alone(self) -> None:
        """`__init__.py`, `.gitkeep` and `py.typed` are deliberately empty."""
        assert probe_syntax("marketplace/__init__.py", "") is None
        assert probe_syntax("src/index.js", "\n\n  \n") is None

    def test_an_unknown_extension_is_still_not_judged(self) -> None:
        assert probe_syntax("NOTES.md", "<!-- nothing here yet -->\n") is None

    def test_a_block_comment_only_file_is_refused(self) -> None:
        source = "/*\n * The database pool.\n * To be written.\n */\n"
        assert probe_syntax("src/db.js", source) is not None

    def test_code_wrapped_around_a_block_comment_passes(self) -> None:
        source = "/* set up */\nconst pg = require('pg');\n"
        assert probe_syntax("src/db.js", source) is None


class TestJavaScriptMustNotStopHalfway:
    def test_an_unclosed_brace_is_refused(self) -> None:
        problem = probe_syntax("src/server.js", "function f() {\n  return {\n")
        assert problem is not None
        assert "unclosed" in problem

    def test_an_unterminated_string_is_refused(self) -> None:
        problem = probe_syntax("src/server.js", "const name = 'OpenBazaar;\n")
        assert problem is not None
        assert "unterminated string" in problem

    def test_an_unterminated_template_literal_is_refused(self) -> None:
        problem = probe_syntax("src/q.js", "const sql = `SELECT * FROM items\n")
        assert problem is not None
        assert "template literal" in problem

    def test_an_unclosed_block_comment_is_refused(self) -> None:
        problem = probe_syntax("src/a.js", "/* the pool\nconst x = 1;\n")
        assert problem is not None
        assert "block comment" in problem

    def test_the_line_is_reported(self) -> None:
        problem = probe_syntax("src/a.js", "const a = 1;\nconst b = 2;\nfunction f() {\n")
        assert problem is not None
        assert "line 3" in problem


class TestBalanceMustNotRefuseWorkingJavaScript:
    """Every one of these is ordinary. A gate that refused them would cost
    more than the truncation it catches."""

    @pytest.mark.parametrize(
        "source",
        [
            "const express = require('express');\nconst app = express();\n",
            "async function main() {\n  await pool.query('SELECT 1');\n}\n",
            "const sql = `SELECT *\n  FROM items\n  WHERE id = $1`;\n",
            "const re = /^[a-z]+\\/[0-9]{2}$/;\nconsole.log(re);\n",
            "const half = total / 2;\nconst q = half / 3;\n",
            "// a comment with an unbalanced ( paren\nconst x = 1;\n",
            "const s = 'it\\'s escaped';\n",
            'const s = "a } brace in a string";\n',
            "const o = { a: [1, 2, {b: 3}], c: (x) => x + 1 };\n",
            "const path = 'a/b/c';\nconst d = path.split('/');\n",
            "export default function App() {\n  return <div>hi</div>;\n}\n",
            "const t = `outer ${ `inner ${x}` } end`;\n",
            "/* block */ const x = 1; /* another */\n",
            "const url = 'http://example.com/a/b';\n",
        ],
    )
    def test_ordinary_javascript_passes(self, source: str) -> None:
        assert probe_syntax("src/a.js", source) is None, source

    @pytest.mark.parametrize("suffix", [".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"])
    def test_every_c_family_extension_is_checked(self, suffix: str) -> None:
        assert probe_syntax(f"src/a{suffix}", "function f() {\n") is not None

    def test_a_mismatched_closer_is_not_refused(self) -> None:
        """Real, but indistinguishable from a mis-lexed regex — so left alone."""
        assert probe_syntax("src/a.js", "const x = (1];\n") is None

    def test_python_is_still_judged_by_the_parser_not_by_balance(self) -> None:
        """Braces mean something else in Python; `_python` owns that file type."""
        assert probe_syntax("a.py", "d = {'a': 1}\nxs = [d]\n") is None


class TestARelativeImportMustResolve:
    """The JavaScript half of the check Python already had.

    Asked for `src/auction.js` importing the sibling `src/db.js`, the model
    wrote `import { query } from '../db.js'` — one directory too far up. Node
    raises `ERR_MODULE_NOT_FOUND` on load, and nothing before this said a word.
    """

    @pytest.fixture
    def project(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "db.js").write_text("export const query = () => {};\n", encoding="utf-8")
        (src / "server.js").write_text("export const app = {};\n", encoding="utf-8")
        return tmp_path

    def test_the_import_that_caused_this_is_refused(self, project) -> None:
        problem = probe_syntax(
            "src/auction.js", "import { query } from '../db.js';\n", workspace=project
        )
        assert problem is not None
        assert "ERR_MODULE_NOT_FOUND" in problem

    def test_it_suggests_the_path_that_works(self, project) -> None:
        problem = probe_syntax(
            "src/auction.js", "import { query } from '../db.js';\n", workspace=project
        )
        assert problem is not None
        assert "'./db.js'" in problem

    def test_the_correct_import_passes(self, project) -> None:
        source = "import { query } from './db.js';\nexport const f = () => query();\n"
        assert probe_syntax("src/auction.js", source, workspace=project) is None

    def test_an_extensionless_sibling_resolves(self, project) -> None:
        """Bundlers and CommonJS both allow it, so it must not be refused."""
        assert probe_syntax("src/a.js", "import x from './db';\n", workspace=project) is None

    def test_require_is_checked_too(self, project) -> None:
        problem = probe_syntax("src/a.js", "const db = require('../db.js');\n", workspace=project)
        assert problem is not None

    def test_a_dynamic_import_is_checked_too(self, project) -> None:
        problem = probe_syntax(
            "src/a.js", "const m = await import('../db.js');\n", workspace=project
        )
        assert problem is not None

    def test_a_bare_package_specifier_is_never_judged(self, project) -> None:
        """Whether express is installed is not knowable from the workspace."""
        source = "import express from 'express';\nimport pg from 'pg';\nexport const a = express;\n"
        assert probe_syntax("src/a.js", source, workspace=project) is None

    def test_a_scoped_package_is_not_judged(self, project) -> None:
        assert probe_syntax("src/a.js", "import x from '@scope/pkg';\n", workspace=project) is None

    def test_an_index_file_in_a_directory_resolves(self, project) -> None:
        routes = project / "src" / "routes"
        routes.mkdir()
        (routes / "index.js").write_text("export const r = 1;\n", encoding="utf-8")
        assert probe_syntax("src/a.js", "import r from './routes';\n", workspace=project) is None

    def test_the_line_number_is_reported(self, project) -> None:
        source = "import express from 'express';\nimport { query } from '../db.js';\n"
        problem = probe_syntax("src/a.js", source, workspace=project)
        assert problem is not None
        assert "line 2" in problem

    def test_without_a_workspace_nothing_is_judged(self) -> None:
        assert probe_syntax("src/a.js", "import x from '../nowhere.js';\n") is None


class TestABodyOfPureCommentaryIsAlsoAStub:
    """The same evasion one level down, once the file must look real.

        export async function placeBid(itemId, bidderId, amount) {
          // Step one: await query with the text SELECT ...
        }

    Balanced, the import resolves, the signature is right — and it returns
    `undefined`. It happened twice in one build and every check passed it.
    """

    def test_the_body_that_caused_this_is_refused(self) -> None:
        source = (
            "import { query } from './db.js';\n\n"
            "export async function placeBid(itemId, bidderId, amount) {\n"
            "  // Step one: await query with the text SELECT current_highest_bid ...\n"
            "}\n"
        )
        problem = probe_syntax("src/auction.js", source)
        assert problem is not None
        assert "nothing but a comment" in problem

    def test_the_line_is_reported(self) -> None:
        source = "function f() {\n  // later\n}\n"
        problem = probe_syntax("src/a.js", source)
        assert problem is not None
        assert "line 1" in problem

    def test_a_block_comment_body_is_refused_too(self) -> None:
        source = "function f() {\n  /* TODO: implement */\n}\n"
        assert probe_syntax("src/a.js", source) is not None

    def test_a_genuinely_empty_body_is_allowed(self) -> None:
        """`noop() {}` and `catch (e) {}` are deliberate and common."""
        assert probe_syntax("src/a.js", "function noop() {}\n") is None
        assert probe_syntax("src/a.js", "try { risky(); } catch (e) {}\n") is None

    def test_a_real_body_passes(self) -> None:
        source = (
            "import { query } from './db.js';\n\n"
            "export async function placeBid(id, bidder, amount) {\n"
            "  // read the item first\n"
            "  const r = await query('SELECT 1', [id]);\n"
            "  return r.rows[0];\n"
            "}\n"
        )
        assert probe_syntax("src/auction.js", source) is None

    def test_an_empty_object_literal_is_untouched(self) -> None:
        assert probe_syntax("src/a.js", "const config = {};\nexport default config;\n") is None

    def test_a_comment_mentioning_braces_in_a_string_is_safe(self) -> None:
        source = "const t = '{ // not a comment }';\nexport default t;\n"
        assert probe_syntax("src/a.js", source) is None
