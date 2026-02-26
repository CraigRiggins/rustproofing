"""
cobol_parser.py — Fixed-format COBOL source parser.

Parses COBOL programs written in the traditional fixed-format layout:
  Cols 1–6   : Sequence number (ignored)
  Col  7     : Indicator  (* or / = comment, - = continuation, space = normal)
  Cols 8–11  : Area A     (division/section/paragraph names, level numbers)
  Cols 12–72 : Area B     (statements, data definitions)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# COBOL verbs — used to distinguish paragraph names from statements in Area A
# ---------------------------------------------------------------------------
COBOL_VERBS = {
    "ACCEPT", "ADD", "ALTER", "CALL", "CANCEL", "CLOSE", "COMPUTE",
    "CONTINUE", "DELETE", "DISPLAY", "DIVIDE", "EVALUATE", "EXEC",
    "EXIT", "GO", "GOBACK", "IF", "INITIALIZE", "INSPECT", "MERGE",
    "MOVE", "MULTIPLY", "OPEN", "PERFORM", "READ", "RELEASE", "RETURN",
    "REWRITE", "SEARCH", "SET", "SORT", "START", "STOP", "STRING",
    "SUBTRACT", "UNSTRING", "WRITE", "NEXT", "END-IF", "END-PERFORM",
    "END-READ", "END-EVALUATE", "END-STRING", "END-COMPUTE", "END-CALL",
    "NOT", "WHEN", "THEN", "ELSE", "UNTIL", "VARYING", "THROUGH",
    "THRU", "USING", "GIVING", "INTO", "FROM", "BY", "TO", "OF",
    "IN", "AT", "ON", "WITH", "DATA", "FILE", "WORKING-STORAGE",
    "LINKAGE", "PROCEDURE", "DIVISION", "SECTION", "COPY", "REPLACE",
}

# Known abend / non-returning call targets
NON_RETURNING_CALLS = {"CEE3ABD", "ILBOABN0", "CEEABND", "ABEND", "MVSWAIT"}

# Tokens that follow PERFORM for inline (no paragraph reference) PERFORMs
INLINE_PERFORM_KEYWORDS = {"UNTIL", "VARYING", "WITH", "TEST", "TIMES"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Statement:
    """A single logical COBOL statement (after continuation joining)."""
    verb: str                # First keyword uppercased (e.g. PERFORM, GO, ALTER)
    tokens: list[str]        # All tokens on the logical line
    line_num: int            # Source line number where statement begins
    text: str                # Joined text of the logical statement

    @property
    def upper_tokens(self) -> list[str]:
        return [t.upper() for t in self.tokens]

    @property
    def full_upper(self) -> str:
        return self.text.upper()


@dataclass
class Paragraph:
    """A COBOL paragraph or section header with its statements."""
    name: str
    line_num: int
    statements: list[Statement] = field(default_factory=list)
    source_order: int = 0        # 0-based index in the paragraph list
    is_section: bool = False      # True if this is a SECTION header
    is_exit_para: bool = False    # True if this is an EXIT-only paragraph


@dataclass
class CobolSource:
    """Parsed representation of a single COBOL source file."""
    filepath: Path
    program_id: str
    total_lines: int
    paragraphs: list[Paragraph] = field(default_factory=list)
    para_map: dict[str, Paragraph] = field(default_factory=dict)
    all_statements: list[tuple[Paragraph, Statement]] = field(default_factory=list)

    def paragraph_at_order(self, order: int) -> Optional[Paragraph]:
        if 0 <= order < len(self.paragraphs):
            return self.paragraphs[order]
        return None

    def paragraphs_in_range(self, start_name: str, end_name: str) -> list[Paragraph]:
        """Return paragraphs in source order from start_name to end_name inclusive."""
        if start_name not in self.para_map or end_name not in self.para_map:
            return []
        start_order = self.para_map[start_name].source_order
        end_order = self.para_map[end_name].source_order
        if start_order > end_order:
            return []
        return self.paragraphs[start_order: end_order + 1]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class CobolParser:
    """Parses a fixed-format COBOL source file into a CobolSource object."""

    def parse(self, filepath: Path) -> CobolSource:
        raw_lines = self._read_lines(filepath)
        logical_lines = self._join_continuations(raw_lines)
        program_id = self._extract_program_id(logical_lines)
        paragraphs = self._extract_paragraphs(logical_lines)
        self._mark_exit_paragraphs(paragraphs)

        para_map = {p.name.upper(): p for p in paragraphs}
        all_statements = [
            (para, stmt)
            for para in paragraphs
            for stmt in para.statements
        ]

        return CobolSource(
            filepath=filepath,
            program_id=program_id,
            total_lines=len(raw_lines),
            paragraphs=paragraphs,
            para_map=para_map,
            all_statements=all_statements,
        )

    # ------------------------------------------------------------------
    # Step 1: Read raw lines, enforcing 72-col limit
    # ------------------------------------------------------------------

    def _read_lines(self, filepath: Path) -> list[tuple[int, str]]:
        """Return list of (line_num, raw_line) with lines truncated at col 72."""
        lines = []
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                line = line.rstrip("\n\r")
                # Pad to at least 7 characters so indicator col exists
                if len(line) < 7:
                    line = line.ljust(7)
                # Truncate to col 72
                line = line[:72]
                lines.append((i, line))
        return lines

    # ------------------------------------------------------------------
    # Step 2: Join continuation lines, skip comments
    # ------------------------------------------------------------------

    def _join_continuations(
        self, raw_lines: list[tuple[int, str]]
    ) -> list[tuple[int, str]]:
        """
        Return logical lines as (start_line_num, content_cols_8_to_72).
        - Skip comment lines (col 7 = '*' or '/')
        - Join continuation lines (col 7 = '-') to the previous line,
          stripping the leading quote char on the continuation if present.
        """
        result: list[tuple[int, str]] = []

        for line_num, raw in raw_lines:
            if len(raw) < 7:
                continue
            indicator = raw[6]  # col 7 is index 6 (0-based)

            if indicator in ("*", "/"):
                # Full-line comment — skip
                continue

            content = raw[7:72].rstrip() if len(raw) > 7 else ""

            if indicator == "-":
                # Continuation: strip leading whitespace; if starts with quote, drop it
                cont = content.lstrip()
                if cont and cont[0] in ('"', "'"):
                    cont = cont[1:]
                if result:
                    prev_num, prev_content = result[-1]
                    result[-1] = (prev_num, prev_content + cont)
                else:
                    result.append((line_num, cont))
            else:
                result.append((line_num, content))

        return result

    # ------------------------------------------------------------------
    # Step 3: Extract PROGRAM-ID
    # ------------------------------------------------------------------

    def _extract_program_id(self, logical_lines: list[tuple[int, str]]) -> str:
        for _, content in logical_lines:
            upper = content.upper().strip()
            if upper.startswith("PROGRAM-ID"):
                # PROGRAM-ID. CBACT01C.
                parts = re.split(r"[\s.]+", upper)
                for i, part in enumerate(parts):
                    if part == "PROGRAM-ID" and i + 1 < len(parts):
                        return parts[i + 1].strip(" .")
        return "UNKNOWN"

    # ------------------------------------------------------------------
    # Step 4: Extract paragraphs from PROCEDURE DIVISION
    # ------------------------------------------------------------------

    def _extract_paragraphs(
        self, logical_lines: list[tuple[int, str]]
    ) -> list[Paragraph]:
        paragraphs: list[Paragraph] = []
        in_procedure = False
        current_para: Optional[Paragraph] = None
        current_statements: list[Statement] = []
        order = 0
        proc_div_line = 0

        for line_num, content in logical_lines:
            upper = content.upper().strip()

            # Detect PROCEDURE DIVISION boundary
            if not in_procedure:
                if "PROCEDURE" in upper and "DIVISION" in upper:
                    in_procedure = True
                    proc_div_line = line_num
                continue

            if not content.strip():
                continue

            # Determine if this line starts in Area A (col 8 = index 0 of content,
            # but in the original line col 8 is index 7; here content is already
            # cols 8–72, so Area A = first 4 chars of content, Area B = chars 4+)
            # A paragraph name starts at col 8, i.e., content[0] is not a space.
            starts_in_area_a = len(content) > 0 and content[0] != " "

            if starts_in_area_a:
                para_name, is_section = self._try_parse_para_name(content)
                if para_name:
                    # Save previous paragraph
                    if current_para is not None:
                        current_para.statements = current_statements
                        paragraphs.append(current_para)
                    elif current_statements:
                        # Statements found before any named paragraph → implicit mainline
                        mainline = Paragraph(
                            name="$MAINLINE",
                            line_num=proc_div_line + 1,
                            source_order=order,
                        )
                        mainline.statements = current_statements
                        paragraphs.append(mainline)
                        order += 1
                    current_statements = []
                    current_para = Paragraph(
                        name=para_name.upper(),
                        line_num=line_num,
                        source_order=order,
                        is_section=is_section,
                    )
                    order += 1
                    continue

            # It's a statement line — parse it
            stmt = self._parse_statement(content.strip(), line_num)
            if stmt:
                current_statements.append(stmt)

        # Don't forget the last paragraph
        if current_para is not None:
            current_para.statements = current_statements
            paragraphs.append(current_para)
        elif current_statements:
            # Only had implicit mainline, no named paragraphs
            mainline = Paragraph(
                name="$MAINLINE",
                line_num=proc_div_line + 1,
                source_order=order,
            )
            mainline.statements = current_statements
            paragraphs.append(mainline)

        # Assign source_order (already set above, but normalise)
        for i, p in enumerate(paragraphs):
            p.source_order = i

        return paragraphs

    def _try_parse_para_name(self, content: str) -> tuple[Optional[str], bool]:
        """
        Try to interpret a line starting in Area A as a paragraph/section name.
        Returns (name, is_section) or (None, False) if not a para name.
        """
        stripped = content.strip()
        upper = stripped.upper()

        # Must end with a period (possibly with trailing space already stripped)
        if not upper.endswith("."):
            # Some compilers allow the para name without a period if next line continues
            # For safety, skip non-period endings
            return None, False

        # Strip the trailing period
        body = stripped[:-1].strip()
        parts = body.split()

        if not parts:
            return None, False

        name = parts[0].upper()

        # SECTION declaration: "NAME SECTION"
        if len(parts) == 2 and parts[1].upper() == "SECTION":
            # Validate name looks like a COBOL identifier
            if self._is_cobol_identifier(name):
                return name, True

        # Paragraph: single token
        if len(parts) == 1:
            # Must not be a COBOL verb (those also appear in Area A sometimes)
            if name in COBOL_VERBS:
                return None, False
            if self._is_cobol_identifier(name):
                return name, False

        return None, False

    def _is_cobol_identifier(self, name: str) -> bool:
        """Return True if name looks like a valid COBOL procedure name."""
        return bool(re.match(r"^[A-Z0-9][A-Z0-9\-]*$", name.upper()))

    def _parse_statement(self, text: str, line_num: int) -> Optional[Statement]:
        """Parse a statement line into a Statement object."""
        if not text:
            return None
        tokens = text.split()
        if not tokens:
            return None
        # Strip trailing period/comma so "GOBACK." is recognized as "GOBACK"
        verb = tokens[0].upper().rstrip(".,;")
        # Handle two-word verbs (also strip punctuation from the second token)
        t1 = tokens[1].upper().rstrip(".,;") if len(tokens) > 1 else ""
        if verb == "GO" and t1 == "TO":
            verb = "GO TO"
            tokens = ["GO TO"] + tokens[2:]
        elif verb == "STOP" and t1 == "RUN":
            verb = "STOP RUN"
            tokens = ["STOP RUN"] + tokens[2:]
        elif verb == "EXIT" and t1 == "PROGRAM":
            verb = "EXIT PROGRAM"
            tokens = ["EXIT PROGRAM"] + tokens[2:]
        elif verb == "NEXT" and t1 == "SENTENCE":
            verb = "NEXT SENTENCE"
            tokens = ["NEXT SENTENCE"] + tokens[2:]
        return Statement(verb=verb, tokens=tokens, line_num=line_num, text=text)

    # ------------------------------------------------------------------
    # Step 5: Mark EXIT-only paragraphs
    # ------------------------------------------------------------------

    def _mark_exit_paragraphs(self, paragraphs: list[Paragraph]) -> None:
        """
        Mark paragraphs that contain only EXIT. as is_exit_para = True.
        These are the exit stubs used with PERFORM...THRU.
        """
        for para in paragraphs:
            non_empty = [
                s for s in para.statements
                if s.verb not in ("EXIT",) or s.text.strip().upper() not in ("EXIT.", "EXIT")
            ]
            only_exit = all(
                s.text.strip().upper().rstrip(".") == "EXIT"
                for s in para.statements
            ) if para.statements else False
            para.is_exit_para = only_exit or para.name.endswith("-EXIT")
