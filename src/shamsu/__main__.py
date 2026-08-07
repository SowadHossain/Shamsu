"""`python -m shamsu`.

The module entry point rather than a console script: `pyproject.toml`
deliberately declares no `[project.scripts]`, because a pip-installed `shamsu`
binary would shadow the managed launcher under `~/.shamsu/bin`.
"""

from shamsu.cli import main

raise SystemExit(main())
