---
name: large-file-surgery
description: Read and change a file too large to hold in one reply - outline first, then one symbol at a time.
---
# Large File Surgery

**Never hold the whole file when you need part of it. Never emit the whole file
when you are changing part of it.**

## Read

1. `read_file` on a large file returns its **outline** - every class and
   function with its line range. That is the map, not the text.
2. `read_symbol(filepath, symbol)` returns one symbol's source exactly.
3. For what the outline does not name, `read_file` with `start_line` and
   `end_line`. Ranges are expected, not a repeated read.

Do not re-read a file you have read. If it changed, you are told.

## Change

- A whole function or class: `replace_symbol(filepath, symbol, content)`.
- Anything smaller: `patch_file`, copying `old_string` character for character
  out of what you were shown. Do not retype it from memory.
- If the match fails, the error shows what the file actually says there. Copy
  from that. Do not guess, and do not rewrite the whole file.

## Build over 60 lines

1. `write_file` the skeleton: imports and signatures.
2. `append_file` each section, about 60 lines at a time.
3. Open blocks between sections are normal. Keep appending until it closes.

Every call must end on a **complete line**.
