---
name: large-file-surgery
description: Read and change a file too large to hold in one reply - outline first, then one symbol at a time.
---
# Large File Surgery

Use this when a file is too big to read or rewrite whole: fixing a bug in a
long module, adding to a large class, or building a file of several hundred
lines.

The rule underneath all of it: **never hold the whole file when you only need
part of it, and never emit the whole file when you only need to change part of
it.**

## Reading: outline, then the one part you need

1. `read_file` on a large file returns its **outline** - every class and
   function with its exact line range - not the text. That is the map.
2. Pick the one symbol the task is about. `read_symbol(filepath, symbol)`
   returns its source exactly.
3. Need something the outline does not name - a constant block, imports, a
   region between functions? `read_file` with `start_line` and `end_line`.
   Reading a file in ranges is expected; it is not a repeated read.

Do not re-read a file you have already read. If it has not changed you will be
told so, and the copy you already have is still correct.

## Changing: patch the part, do not re-emit the file

1. Copy the exact text you are replacing out of a `read_symbol` or `read_file`
   result. Copy it character for character - do not retype it from memory.
   Line-number gutters are stripped for you, so pasting what you were shown is
   safe.
2. `patch_file` with that `old_string` and the replacement. Its cost does not
   grow with the file and it cannot lose the parts you did not touch.
3. If the match fails, the error shows what the file **actually** says at the
   nearest point. Copy from that, do not guess again, and do not fall back to
   rewriting the whole file.

## Building: skeleton, then sections

For a new file over 60 lines:

1. `write_file` the skeleton - imports, class and function signatures, enough
   structure to be worth filling.
2. `append_file` each following section, 60 lines at a time.
3. An unfinished file with open blocks is normal between sections. You will be
   told how many are still open; that is progress, not a fault. Keep appending
   until it closes.

Every call must end on a **complete line**. An unfinished section is fine; an
unfinished line is not, and will be refused.

## Finishing

- `run_tests` after the change - it finds the project's test command itself.
- If there are no tests, run the file or the build.
- Say what you changed and what you checked. Report a failure as a failure.
