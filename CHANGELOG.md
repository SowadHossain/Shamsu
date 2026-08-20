# Changelog

All notable SHAMSU release changes are documented here.

## Unreleased

### Added

- A per-call content cap for every tool that carries a payload
  (`write_file`, `append_file`, `patch_file`, `read_and_patch`,
  `create_and_run`): `clamp(2,000 | 0.85 x reply_cap | 8,000)` characters. This
  restores smallcode's 4x headroom ratio, where the model is never permitted to
  attempt a write large enough to exhaust its own output budget. The ceiling is
  llama.cpp's ~13KB tool-argument wall, which does NOT report
  `done_reason: "length"` and so was invisible to the existing truncation
  guard; the floor says the window is the wrong shape for the task rather than
  degrading to useless chunk sizes. `MAX_REPLY_TOKENS` is deliberately
  unchanged - the unit of work is bounded, not the budget.
- The number 60 lines is now stated in the system prompt AND in the schema
  description of every payload argument, not only enforced in the tool. A
  refusal names the strategy ("write the first 60 lines, then append_file each
  section") rather than only the limit, which turns an unrecoverable failure
  into a recoverable one: the content was fully generated and is merely
  rejected at the door.
- A pre-write gate that tests for TRUNCATION SIGNATURES rather than validity -
  an unterminated string or comment, a dangling operator, a bracket opened on
  the last line - for new files and for every language, closing the hole where
  both write-time gates bailed out when the target did not already exist and
  only understood Python. A first section with unclosed blocks passes, because
  a gate testing for validity would refuse every legitimate chunk.
- Continue-from-the-tail recovery in simple mode, language-agnostic: a file
  found stopping mid-construct is asked for ONLY the missing remainder, quoted
  against its own last twelve lines, instead of being resent whole.

### Added (read path, 2026-08-20)

- **Outline-first reading.** `read_file` on a file over 200 lines returns its
  outline - every class and function, its signature and its exact line range,
  bodies omitted - instead of the first 24,000 bytes. Head-clipping is what
  started the dead end in §2: the model patched from what it saw, `old_string`
  was in the half it never saw, the fuzzy retry missed too, and the whole-file
  rewrite was refused for being a partial read. Deterministic - Python through
  `ast`, braced languages through a declaration scan - rather than smallcode's
  LLM summary of the first 8KB, which derives a 2,000-line file's outline from
  its first ~200 lines and costs a full generation.
- **`read_symbol`**, the follow-up an outline earns: one function or class,
  exactly, from the same parse that produced the outline, so the range cannot
  drift from what the model was shown.
- **Line numbers on every read**, smallcode's gutter. `start_line` is arithmetic
  on a wall of text until the model has seen which line is which. The gutter is
  stripped back out of `patch_file` arguments, so copying what you were shown is
  safe.
- **`run_tests`**, which detects the project's test command (npm script, pytest
  layout, Cargo, go.mod, Makefile) instead of leaving the model to guess it, and
  runs it through `run_command` so approval and the risk classifier still apply.
- **`use_skill` and a skill index in the prompt.** The skill loader, its
  frontmatter parsing and its override rules all existed and nothing in simple
  mode had ever called them. Adds a `large-file-surgery` skill for the
  outline -> symbol -> patch -> verify workflow.
- **The system prompt is now `agents/prompts/simple_system.md`**, section by
  section, so the most-edited and least code-like thing in the agent can be read
  and changed without opening Python. Adds smallcode's acting rule: a model that
  writes one section and asks "what would you like next?" spends the turn on a
  question the user already answered.

### Added (symbol editing + Definition of Done, 2026-08-20)

- **`replace_symbol(filepath, symbol, content)`** - replace a whole function or
  class by NAME. `patch_file` could never do this cheaply: replacing a function
  means reproducing every line of the old one exactly, and a model that can
  write the new one will still fail to retype the old one. Three guards, live
  proven: content is re-indented to the original's column (a small model sends a
  method at column zero far more often than not); an edit that would stop a
  working file parsing is refused; and an edit that would silently DELETE
  members of a container is refused by name.
- **Definition-of-Done contracts** - `contract_create`, `contract_status`,
  `contract_assert_pass` / `_fail` / `_skip`, and a done-guard that sends a
  premature "the task is complete" back with the list of unchecked assertions.
  Stored on disk, because a `SimpleChatLoop` is rebuilt for every user message
  and a contract that does not outlive one turn is not a contract. `passed`
  requires evidence; `failed` counts as resolved, so the model can still REPORT
  a failure. `SHAMSU_CONTRACT=0` disables it.
  (Distinct from `verify/contract.py`, which DERIVES a contract from the
  prompt, and from `verify/dod.py`, which runs registry-declared checks. This
  one is authored by the model for the task in hand.)
- `read_symbol` on a container returns ITS outline rather than its body. Live
  2026-08-20 the model did exactly as told - read the outline, then read_symbol
  the class it needed - and got 313 lines back, because `export class Player`
  spanned lines 34-347. The outline had just saved the window and the next call
  spent it.
- A missing symbol now suggests the closest matches before listing the roster.
  The model asked for `initializePlayer`, `updatePlayerState` and `renderPlayer`
  in three consecutive calls; each time it was handed thirty names in one line
  and invented a fourth.

### Fixed

- **An append that breaks a working file is rolled back.** Live 2026-08-20 the
  model was shown a REPLACEMENT for `takeDamage` and appended it past the
  closing brace of the class, leaving the module unparseable - then appended the
  same eleven lines again. Structural counting cannot catch this (the block is
  perfectly brace-balanced), so the write happens, is judged by the real
  checker, and is undone. Silent when the file was already unfinished, or the
  guard would break chunked writing.
- **The prose nudge no longer leads with `append_file`.** It said "call
  append_file to add it to the end", and a model showing a replacement took that
  literally - which is what produced the corruption above. It now leads with
  `replace_symbol`, then `patch_file`, and offers append only for content that
  belongs at the end.
- `read only the functions you need` is no longer classified as read-only MODE.
  A run that fixed a real bug reported `contract violation: prompt forbade file
  changes but 2 changed`, because the spaced form matched. `read-only` and
  `readonly` still match unconditionally; the spaced form now has to prove it is
  not governing an object.

- **The truncation gate no longer judges a fragment as a file.** It ran on
  `patch_file.new_string` and refused a legitimate JSDoc block three times with
  "it ends inside a /* comment opened on line 23", then ended the turn blaming
  an output limit that had never fired. A patch replaces a region that may start
  inside one block and end inside another, and an append chunk is unfinished by
  design. The gate now runs only on `write_file` and `create_and_run`, where the
  payload really is a whole file; the size cap still applies to all five.
  smallcode caps payload size and checks nothing else.
- Reading an unchanged file again says so instead of resending it - eight
  `read_file js/game.js` calls in one turn were eliding the window to make room
  for copies of a file that had not changed. Unlike smallcode's version, the
  memory is dropped whenever an elision sweep runs, so the claim is true
  whenever it is made.
- Ranged reads are no longer counted as repeated reads. `_argument_summary`
  returned the filepath alone, so section 3 of a file read in pieces was
  answered with "you have already called this" - firing on exactly the strategy
  the outline now tells the model to use.
- The stop message after three refused writes names the cause it actually had,
  instead of blaming the output limit for a content refusal.
- The bundled `developer` skill said "Default to `write_file` with the COMPLETE
  file content" - the exact opposite of the 60-line rule the tool enforces. A
  skill that fights the harness is worse than no skill.

- A file still being built now reports its open blocks as PROGRESS ("3 block(s)
  still open - continue with append_file") instead of as a fault. Verifying
  after every chunk was correct and had to stay; reporting an unfinished
  section as broken would have sent the model repairing a file that was simply
  not finished yet. A file left open when the turn ENDS is still failed, so the
  run outcome cannot read a half-written file as a success.
- A write that GROWS a file no longer counts toward the repeated-edit ceiling.
  Live 2026-08-20, told to build a 1,500-line file, qwen2.5:3b chunked as asked
  but carried each section with `write_file`; five verified-clean sections in,
  the turn was stopped for "5 blind edits I cannot confirm". The exemption
  already existed for `append_file` and now follows the shape rather than the
  tool.
- Unknown models no longer silently drop to an 8,192-token window. The table
  was exact-match only, so `qwen2.5:3b-instruct-q4_K_M` - one quantisation
  suffix from a listed model - asked for 8k, which collapses the reply cap and
  shrinks every write cap derived from it. Family patterns are consulted before
  the fallback.
- `test_count_tokens_falls_back_when_asset_missing` left the tokenizer cache
  memoised on a missing asset, so every later `count_tokens` in the session
  silently used chars/4.

## 0.4.0b1 - 2026-07-20

### Added

- Full-request noninteractive harness with deterministic approval policy,
  dry-run, timeout, JSON result contract, and persisted-evidence validation.
- Canonical ActionLedger run folders for every prompt, including structured
  decisions, tool/model calls, context records, command output, diagnostics,
  mutations, verification, final output, and concise `/run show` inspection.
- Composite routing, mentioned-document normalization, shared workspace file
  policy, asynchronous memory finalization, TaskFlow PRD acceptance, and
  complete PRD-to-Django generation/browser checks.
- Web provider fallback and browser capability status, one-time first-run
  readiness report, expanded `/doctor`, and idempotent workspace schema
  upgrades that preserve historical evidence.
- Three-OS CI lifecycle validation and a deterministic Python/Django/Node/
  React/mixed release dogfood benchmark.

### Fixed

- False-success outcomes, swallowed patch/fallback errors, ungrounded compound
  routing, malformed approval semantics, and non-actionable Git probe errors.
- Missing optional run artifacts on read-only requests and incomplete run
  summaries.
- Unix lifecycle scripts failing under Bash because of CRLF line endings.
- SQLite web-cache connections remaining open until garbage collection on
  Windows; cache creation is now lazy for non-web prompts.

### Measured

- 1.266s import startup, 1.071s cold first answer, 0.307s slowest warm answer,
  90.9 MB peak RSS, and 52,128 bytes of run-log growth across five deterministic
  dogfood sessions on the release workstation.
- Default model tier: 11/12 cases at three samples; light tier: 9/12. Stochastic
  cases and tier limitations are documented rather than hidden.

## MVP Release

This MVP focuses on a local-first path from workspace understanding to
approval-backed project work on low-resource machines.

### Added

- CLI REPL with workspace-scoped `index`, `status`, `log`, `search`,
  `symbols`, `parse-prd`, `models`, and `django setup` commands.
- SQLite-backed workspace index with Python symbol extraction, snippets, and
  stale-file cleanup.
- Markdown PRD parsing, rule-based entity extraction, and `ProjectSpec`
  assembly.
- Deterministic Django fixed templates for generated projects using Django,
  DRF, Simple JWT, crispy forms, DaisyUI, HTMX, and SQLite.
- Natural-language routing for QA, code edit, bug fix, audit, test generation,
  documentation, and project-generation intents.
- Approval-backed command runner with workspace checks, command risk
  classification, blocked-command rejection, timeouts, captured output, and
  secret redaction.
- Django generated-project setup runner for `pip install -r requirements.txt`,
  `makemigrations`, and `migrate`.
- Approval-backed patch validation, Rich preview, apply, rollback, backups,
  failure restore, and post-patch re-indexing.
- Read-only git dirty-worktree helpers for safer edits.
- Local JSONL audit trail under `.shamsu/` for safety-sensitive events.
- Local Ollama runtime status and repair helpers.

### Changed

- README now includes the PRD format guide, generated Django project run guide,
  demo script, and explicit known limitations.
- CLI help now presents workflow examples and generated-project setup commands.
- Local logs and command outputs are redacted before display.

### Known Limitations

- SHAMSU is a workspace sandbox, not an OS sandbox or Docker isolation layer.
- Full PRD-to-Django project generation is branch-dependent until the complete
  pipeline lands on the default branch.
- Generated Django projects target local development with SQLite.
- PostgreSQL, Docker deployment, React SPA generation, file uploads, email
  delivery, and background workers are outside the MVP scope.
- Local model quality and speed depend on the installed Ollama models and host
  hardware.
