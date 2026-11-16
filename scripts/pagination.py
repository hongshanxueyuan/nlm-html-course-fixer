"""Check unambiguous page-boundary errors in ordered Markdown."""

import re


_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+\S")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_RULE = re.compile(r"^ {0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
_TABLE_REFERENCE = re.compile(r"(?:这(?:张|个|份)?表|上表|上述表|该表|表格?中)")


class PaginationBoundaryError(ValueError):
    pass


def validate_boundaries(pages):
    for index, (current, following) in enumerate(zip(pages, pages[1:]), start=1):
        current_headings = _headings(current)
        next_headings = _headings(following)
        if not next_headings:
            continue
        first_next = next_headings[0]
        prefix = following[:first_next[0]]
        if current_headings:
            last = current_headings[-1]
            if (last[1] < first_next[1] and _substantive(current[:last[0]])
                    and not _substantive(prefix)):
                raise PaginationBoundaryError(f"第 {index} 页的上级标题应与第 {index + 1} 页的首个子标题一起检查")
        last_lines = [line.strip() for line in current.splitlines() if line.strip() and not _RULE.fullmatch(line)]
        first_paragraph = re.split(r"\n[ \t]*\n", prefix.strip(), maxsplit=1)[0]
        if last_lines and last_lines[-1].startswith("|") and last_lines[-1].endswith("|") and _TABLE_REFERENCE.search(first_paragraph):
            raise PaginationBoundaryError(f"第 {index + 1} 页开头的表格说明应与第 {index} 页的表格一起检查")


def _headings(text):
    headings = []
    offset = 0
    fence = None
    for line in text.splitlines(keepends=True):
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None:
            match = _HEADING.match(line)
            if match:
                headings.append((offset, len(match.group(1))))
        offset += len(line)
    return headings


def _substantive(text):
    return any(line.strip() and not _RULE.fullmatch(line) for line in text.splitlines())
