"""Format a bounded recent view of the read-only Slurm history output."""

from __future__ import annotations

import re

_JOB_ID = re.compile(r"([0-9]+)(?:_[0-9]+)?(?:\.[A-Za-z0-9_-]+)?\Z")
_FORMAT_ERROR = "Cannot identify recent history safely; use history full to view all output."


def recent_history(output: str, limit: int = 10) -> str:
    """Keep headers and the highest numeric job IDs, preserving each row's text.

    Short informational or legacy mock output is returned unchanged. Longer
    unknown formats fail explicitly so callers can offer the complete output.
    """
    if limit < 1:
        raise ValueError("History limit must be positive.")
    lines = output.splitlines()
    header_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if header_index is None:
        return output
    if lines[header_index].split()[0] not in {"JobID", "JobIDRaw"}:
        if len(lines) <= limit:
            return output
        raise ValueError(_FORMAT_ERROR)

    rows: list[tuple[int, int, str]] = []
    for index in range(header_index + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if all(char in "- \t" for char in line):
            continue
        match = _JOB_ID.fullmatch(line.split()[0])
        if match is None:
            raise ValueError(_FORMAT_ERROR)
        rows.append((int(match[1]), index, line))
    if not rows:
        return output

    selected = iter(row[2] for row in sorted(rows, key=lambda row: row[0], reverse=True)[:limit])
    row_indices = {row[1] for row in rows}
    result = []
    for index, line in enumerate(lines):
        if index in row_indices:
            replacement = next(selected, None)
            if replacement is not None:
                result.append(replacement)
        else:
            result.append(line)
    return "\n".join(result) + ("\n" if output.endswith("\n") else "")
