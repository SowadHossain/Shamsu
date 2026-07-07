"""Strict Debug Mode prompt + final-response enforcement.

Debug mode deliberately starves the model of everything except the one
selected root error, the files it was actually shown, its own failed history,
and the verification command. The rules forbid success claims and blind edits
so the loop - not the model - decides whether a fix worked.
"""
from __future__ import annotations

import re

from shamsu.repair.types import DebugContext

STRICT_DEBUG_SYSTEM = """You are SHAMSU in STRICT DEBUG MODE.
You are fixing ONE root-cause error at a time in a real project.

Hard rules:
- Fix ONLY the single error described below. Ignore downstream/cascade errors.
- You may edit ONLY files whose content is shown to you here. If you need a
  file you cannot see, say so and stop - do NOT guess its contents.
- Propose the MINIMAL change that resolves this one error.
- Do NOT claim the error is fixed, resolved, verified, or passing. You cannot
  verify anything; a separate verifier decides that after you.
- Output a structured repair plan as JSON only, no prose outside the JSON.

Output JSON schema:
{"root_cause": string,
 "target_file": string,
 "inspected_files": [string],
 "search": string,   // exact snippet to replace (preferred)
 "replace": string,  // its replacement
 "full_content": string}  // OR the complete new file if a search block is impractical
Provide EITHER search+replace OR full_content, not both.
"""


def build_debug_prompt(context: DebugContext) -> str:
    error = context.primary_error
    parts: list[str] = []
    parts.append("## Selected root error (fix ONLY this)")
    location = f"{error.file}:{error.line}" if error.file else "(no file location)"
    parts.append(
        f"- kind: {error.kind.value}\n"
        f"- code: {error.code or 'n/a'}\n"
        f"- where: {location}\n"
        f"- symbol: {error.symbol or 'n/a'}\n"
        f"- message: {error.message}\n"
        f"- raw: {error.raw_block}"
    )

    if context.import_suggestion:
        parts.append("## Deterministic resolver suggestion (verified on disk)")
        parts.append(context.import_suggestion)

    if context.inspected:
        parts.append("## Inspected files (you may edit ONLY these)")
        for snippet in context.inspected:
            parts.append(
                f"### {snippet.file} (lines {snippet.line_start}-{snippet.line_end})\n"
                f"{snippet.content}"
            )
    else:
        parts.append("## Inspected files\n(none inspected yet - request one instead of editing blind)")

    if context.previous_attempts:
        parts.append("## Previous failed attempts (do NOT repeat these)")
        for i, attempt in enumerate(context.previous_attempts, 1):
            files = ", ".join(attempt.files_changed) or "(none)"
            parts.append(
                f"{i}. changed [{files}] -> {attempt.outcome.value}"
                + (f" ({attempt.note})" if attempt.note else "")
            )

    parts.append("## Verification command")
    parts.append(context.verify_command or "(none provided)")
    parts.append(
        "## Task\nProduce the minimal repair plan JSON for the selected error only. "
        "Do not claim success."
    )
    return "\n\n".join(parts)


# --- Final response enforcement ------------------------------------------------

_FORBIDDEN_RE = re.compile(
    r"\b(fixed|resolved|verified|pass(?:es|ed|ing)?)\b", re.IGNORECASE
)
# Replacements are chosen so the neutralized word does NOT contain the original
# as a substring (e.g. not "unverified" for "verified").
_NEUTRALIZE = {
    "fixed": "attempted",
    "resolved": "attempted",
    "verified": "unconfirmed",
}


def contains_unverified_success_claim(message: str, verifier_exit_code: int) -> bool:
    """True if `message` asserts success while the verifier did NOT exit 0."""
    if verifier_exit_code == 0:
        return False
    return bool(_FORBIDDEN_RE.search(message or ""))


def enforce_final_response(message: str, verifier_exit_code: int) -> str:
    """The final human-facing message may only claim success when the verifier
    exited 0. Otherwise, forbidden success words are neutralized so SHAMSU can
    never tell the user something is fixed when it isn't.

    This is a hard backstop on top of the model's own instructions - it runs on
    the assembled final string regardless of where the text came from.
    """
    if verifier_exit_code == 0:
        return message
    if not message:
        return message

    def _replace(match: re.Match[str]) -> str:
        word = match.group(0).lower()
        if word.startswith("pass"):
            return "failing"
        return _NEUTRALIZE.get(word, "attempted")

    neutralized = _FORBIDDEN_RE.sub(_replace, message)
    return neutralized


def build_final_message(
    verifier_exit_code: int,
    attempts: int,
    remaining_summary: str,
) -> str:
    """Construct the final message from verifier ground truth only - never
    from the model's self-report."""
    if verifier_exit_code == 0:
        return (
            f"Verifier passed (exit code 0) after {attempts} repair attempt(s). "
            "The build/tests are green."
        )
    base = (
        f"Verifier still failing (exit code {verifier_exit_code}) after {attempts} "
        f"repair attempt(s). I did not resolve it."
    )
    if remaining_summary:
        base += f"\nRemaining root error: {remaining_summary}"
    return base
