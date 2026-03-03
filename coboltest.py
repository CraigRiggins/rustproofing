#!/usr/bin/env python3
"""
coboltest.py — COBOL unit test harness generator.

Reads a single COBOL source file and generates a companion COBOL test program
containing one test stub per section or paragraph (selectable via CLI flags).
For subroutines (PROCEDURE DIVISION USING) the harness reproduces the LINKAGE
SECTION as WORKING-STORAGE and passes it via CALL … USING.
For CICS programs an additional stubs file is emitted with skeleton paragraphs
for every EXEC CICS operation found in the source.
For batch programs the stubs drive the whole program entry-point; paragraph
names are used as scenario labels with a banner comment explaining the limit.

Usage
-----
  python coboltest.py source.cbl [--sections | --paragraphs | --all]
                      [-o OUTPUT_DIR] [--no-cics-stubs]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

from cobol_parser import CobolParser

# ---------------------------------------------------------------------------
# Module-level regex patterns
# ---------------------------------------------------------------------------
EXEC_CICS_RE = re.compile(r"EXEC\s+CICS\s+(\w+)(.*?)END-EXEC", re.DOTALL | re.IGNORECASE)
FILE_RE      = re.compile(r"\bFILE\s*\(\s*([^)]+)\s*\)",    re.IGNORECASE)
DATASET_RE   = re.compile(r"\bDATASET\s*\(\s*([^)]+)\s*\)", re.IGNORECASE)
MAP_RE       = re.compile(r"\bMAP\s*\(\s*([^)]+)\s*\)",     re.IGNORECASE)
MAPSET_RE    = re.compile(r"\bMAPSET\s*\(\s*([^)]+)\s*\)",  re.IGNORECASE)
COPY_RE      = re.compile(r"COPY\s+['\"]?([A-Z0-9][A-Z0-9\-]*)['\"]?", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CicsCommand:
    operation:   str
    file_name:   str = ""
    map_name:    str = ""
    mapset_name: str = ""
    line_num:    int = 0


@dataclass
class TestTarget:
    program_id:        str
    filepath:          Path
    total_lines:       int
    program_type:      str              # BATCH | CICS | UTILITY
    is_cics:           bool
    is_subroutine:     bool             # has PROCEDURE DIVISION USING
    using_params:      list[str]        = field(default_factory=list)
    linkage_raw_lines: list[str]        = field(default_factory=list)
    sections:          list[tuple[str, int]] = field(default_factory=list)
    paragraphs:        list[tuple[str, int]] = field(default_factory=list)
    cics_commands:     list[CicsCommand]     = field(default_factory=list)
    copybooks:         list[str]             = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fixed-format COBOL line writer
# ---------------------------------------------------------------------------

class _Writer:
    """
    Accumulates 80-column fixed-format COBOL lines.

    Layout:
      Cols  1– 6 : sequence number (auto-incrementing by 10)
      Col   7    : indicator  (space or '*' for comments)
      Cols  8–72 : content    (65 usable characters)
      Cols 73–80 : ignored    (8 trailing spaces)
    """

    _CONTENT_WIDTH = 65   # cols 8-72

    def __init__(self) -> None:
        self._seq   = 0
        self._lines: list[str] = []

    # ------------------------------------------------------------------ output

    def getvalue(self) -> str:
        return "\n".join(self._lines) + "\n"

    # ------------------------------------------------------------------ emit helpers

    def _next_seq(self) -> int:
        self._seq += 10
        return self._seq

    def _emit(self, indicator: str, content: str) -> None:
        seq = self._next_seq()
        c   = content[: self._CONTENT_WIDTH].ljust(self._CONTENT_WIDTH)
        self._lines.append(f"{seq:06d}{indicator}{c}        ")

    # ------------------------------------------------------------------ public API

    def blank(self) -> None:
        self._emit(" ", "")

    def comment(self, text: str = "") -> None:
        """Emit col-7 = '*' comment line."""
        self._emit("*", text)

    def divider(self, char: str = "=") -> None:
        self._emit("*", char * self._CONTENT_WIDTH)

    def a(self, text: str) -> None:
        """Emit content starting in Area A (col 8)."""
        self._emit(" ", text)

    def b(self, text: str) -> None:
        """Emit content starting in Area B (col 12 = 4 spaces in content field)."""
        self._emit(" ", "    " + text)

    def raw(self, content_8_72: str) -> None:
        """
        Emit a line whose content (cols 8-72) is given verbatim.
        Used to reproduce LINKAGE SECTION items in WORKING-STORAGE.
        """
        self._emit(" ", content_8_72)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class TestTargetParser:
    """
    Parses a single COBOL source file into a TestTarget descriptor.
    Delegates structural paragraph/section extraction to CobolParser,
    then performs its own division-level scan for LINKAGE content,
    PROCEDURE DIVISION USING params, COPY statements, and EXEC CICS ops.
    """

    def parse(self, filepath: Path) -> TestTarget:
        inner   = CobolParser()
        raw_lines = inner._read_lines(filepath)           # type: ignore[attr-defined]
        logical   = inner._join_continuations(raw_lines)  # type: ignore[attr-defined]
        source    = inner.parse(filepath)

        # Prefer our two-line-aware extraction over CobolParser's single-line one
        program_id = self._extract_program_id(logical) or source.program_id

        linkage_raw, using_params, copybooks, has_cics_inline = \
            self._scan_divisions(raw_lines)

        content_text = self._build_content_text(raw_lines)
        cics_commands, has_cics_regex = self._extract_cics_commands(content_text)

        is_cics      = has_cics_inline or has_cics_regex
        is_subroutine = bool(using_params)

        if is_cics:
            program_type = "CICS"
        elif is_subroutine:
            program_type = "UTILITY"
        else:
            program_type = "BATCH"

        sections:   list[tuple[str, int]] = []
        paragraphs: list[tuple[str, int]] = []
        for para in source.paragraphs:
            if para.name == "$MAINLINE" or para.is_exit_para:
                continue
            if para.is_section:
                sections.append((para.name, para.line_num))
            else:
                paragraphs.append((para.name, para.line_num))

        return TestTarget(
            program_id        = program_id,
            filepath          = filepath,
            total_lines       = source.total_lines,
            program_type      = program_type,
            is_cics           = is_cics,
            is_subroutine     = is_subroutine,
            using_params      = using_params,
            linkage_raw_lines = linkage_raw,
            sections          = sections,
            paragraphs        = paragraphs,
            cics_commands     = cics_commands,
            copybooks         = copybooks,
        )

    # ------------------------------------------------------------------

    def _extract_program_id(self, logical: list[tuple[int, str]]) -> str:
        """Handle both same-line and next-line PROGRAM-ID layouts."""
        pid_next = False
        for _, content in logical:
            upper = content.upper().strip()
            if pid_next:
                name = re.split(r"[\s.]+", upper)[0]
                if name and re.match(r"^[A-Z0-9][A-Z0-9\-]+$", name):
                    return name
                pid_next = False
            if upper.startswith("PROGRAM-ID"):
                parts = [p for p in re.split(r"[\s.]+", upper) if p]
                if len(parts) >= 2 and parts[1]:
                    return parts[1]
                pid_next = True
        return ""

    def _scan_divisions(
        self, raw_lines: list[tuple[int, str]]
    ) -> tuple[list[str], list[str], list[str], bool]:
        """
        Single-pass scan of raw COBOL lines for:
          - LINKAGE SECTION verbatim content (cols 8-72)
          - PROCEDURE DIVISION USING parameter names
          - COPY statement targets
          - EXEC CICS presence flag

        Returns (linkage_raw_lines, using_params, copybooks, has_exec_cics).
        """
        division:    str  = ""
        in_linkage:  bool = False
        linkage_raw: list[str] = []
        using_params: list[str] = []
        copybooks:    list[str] = []
        has_cics:     bool = False

        for _, raw in raw_lines:
            if len(raw) < 7:
                continue
            indicator = raw[6]
            if indicator in ("*", "/"):
                continue
            content = raw[7:72].rstrip() if len(raw) > 7 else ""
            upper   = content.upper().strip()

            if not upper:
                if in_linkage:
                    linkage_raw.append("")
                continue

            # Quick CICS presence check
            if "EXEC" in upper and "CICS" in upper:
                has_cics = True

            # Division header detection
            if "DIVISION" in upper:
                tokens  = [t.rstrip(".,;") for t in upper.split()]
                div_idx = next((i for i, t in enumerate(tokens) if t == "DIVISION"), -1)
                if div_idx > 0:
                    division    = tokens[div_idx - 1]
                    in_linkage  = False
                    # PROCEDURE DIVISION USING params (may be on same line)
                    if division == "PROCEDURE" and "USING" in tokens:
                        u_idx = tokens.index("USING")
                        for t in tokens[u_idx + 1:]:
                            if re.match(r"^[A-Z][A-Z0-9\-]*$", t):
                                using_params.append(t)
                continue

            # Section header detection
            if "SECTION" in upper:
                tokens  = [t.rstrip(".,;") for t in upper.split()]
                sec_idx = next((i for i, t in enumerate(tokens) if t == "SECTION"), -1)
                if sec_idx > 0:
                    section_name = tokens[sec_idx - 1]
                    in_linkage   = (division == "DATA" and section_name == "LINKAGE")
                continue

            # COPY statement
            if "COPY" in upper:
                m = COPY_RE.search(upper)
                if m:
                    name = m.group(1).strip("'\"").upper()
                    if name and name not in copybooks:
                        copybooks.append(name)

            # Capture LINKAGE SECTION lines verbatim
            if in_linkage:
                linkage_raw.append(content)

        return linkage_raw, using_params, copybooks, has_cics

    def _build_content_text(self, raw_lines: list[tuple[int, str]]) -> str:
        """Single-string of all non-comment cols 8-72 content for regex scanning."""
        parts = []
        for _, raw in raw_lines:
            if len(raw) < 7 or raw[6] in ("*", "/"):
                continue
            parts.append(raw[7:72] if len(raw) > 7 else "")
        return " ".join(parts)

    def _extract_cics_commands(
        self, content_text: str
    ) -> tuple[list[CicsCommand], bool]:
        """Extract all EXEC CICS commands from the full-content text."""
        commands: list[CicsCommand] = []
        has_cics  = False

        for m in EXEC_CICS_RE.finditer(content_text):
            has_cics  = True
            operation = m.group(1).upper()
            params    = m.group(2)

            def _get(pattern: re.Pattern, text: str) -> str:
                match = pattern.search(text)
                return match.group(1).strip().strip("'\"").upper() if match else ""

            file_name   = _get(FILE_RE, params) or _get(DATASET_RE, params)
            map_name    = _get(MAP_RE, params)
            mapset_name = _get(MAPSET_RE, params)

            commands.append(CicsCommand(
                operation   = operation,
                file_name   = file_name,
                map_name    = map_name,
                mapset_name = mapset_name,
            ))

        return commands, has_cics


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class TestGenerator:
    """Generates fixed-format COBOL test harness file(s) from a TestTarget."""

    def generate(
        self,
        target:         TestTarget,
        mode:           str,
        output_dir:     Path,
        gen_cics_stubs: bool = True,
    ) -> list[Path]:
        """
        Write test harness (and optional CICS stubs) to *output_dir*.

        mode: "sections" | "paragraphs" | "all"
        Returns list of Path objects for files written.
        """
        items    = self._resolve_items(target, mode)
        harness  = self._gen_harness(target, items)
        h_path   = output_dir / f"{target.program_id}-TEST.cbl"
        h_path.write_text(harness)
        written  = [h_path]

        if target.is_cics and gen_cics_stubs and target.cics_commands:
            stubs  = self._gen_cics_stubs(target)
            s_path = output_dir / f"{target.program_id}-STUBS.cbl"
            s_path.write_text(stubs)
            written.append(s_path)

        return written

    # ------------------------------------------------------------------
    # Item resolution
    # ------------------------------------------------------------------

    def _resolve_items(
        self, target: TestTarget, mode: str
    ) -> list[tuple[str, int, str]]:
        """
        Return sorted list of (name, line_num, kind) for test stub generation.
        kind is "SECTION" or "PARA".
        """
        if mode == "paragraphs":
            return [(n, l, "PARA") for n, l in target.paragraphs]

        if mode == "sections":
            if target.sections:
                return [(n, l, "SECTION") for n, l in target.sections]
            # Graceful fallback when no sections exist
            return [(n, l, "PARA") for n, l in target.paragraphs]

        # mode == "all": merge sections + paragraphs, sort by source line number
        combined = (
            [(n, l, "SECTION") for n, l in target.sections] +
            [(n, l, "PARA")    for n, l in target.paragraphs]
        )
        combined.sort(key=lambda x: x[1])
        return combined

    # ------------------------------------------------------------------
    # Test harness
    # ------------------------------------------------------------------

    def _gen_harness(
        self,
        target: TestTarget,
        items:  list[tuple[str, int, str]],
    ) -> str:
        w        = _Writer()
        today    = _date.today().isoformat()
        pid      = target.program_id
        test_pid = f"{pid}-TEST"[:30]

        # Build stub-name table: each item gets a unique 30-char paragraph name
        stub_table: list[tuple[str, str, int, str]] = []
        for idx, (name, line_num, kind) in enumerate(items, start=1):
            stub_name = f"{idx:04d}-TC-{name}"[:30]
            stub_table.append((stub_name, name, line_num, kind))

        using_clause = self._using_clause(target)

        # ---- IDENTIFICATION DIVISION ----
        w.a("IDENTIFICATION DIVISION.")
        w.a(f"PROGRAM-ID. {test_pid}.")
        w.comment()
        w.divider()
        w.comment(f" Unit test harness for {pid}")
        w.comment(f" Source:    {target.filepath.name}")
        w.comment(f" Generated: {today}")
        w.comment(f" Type:      {target.program_type}")
        if target.is_cics:
            w.comment(" CICS:      populate DFHCOMMAREA before each CALL")
        if not target.is_subroutine and not target.is_cics:
            w.comment(" NOTE: BATCH program — stubs drive full CALL entry point")
        w.divider()
        w.blank()

        # ---- DATA DIVISION ----
        w.a("DATA DIVISION.")
        w.a("WORKING-STORAGE SECTION.")

        # Test infrastructure
        w.comment()
        w.comment(" --- Test Infrastructure ---")
        w.a("01  WS-TEST-SUITE.")
        w.b("05  TS-TEST-COUNT       PIC 9(04) VALUE 0.")
        w.b("05  TS-PASS-COUNT       PIC 9(04) VALUE 0.")
        w.b("05  TS-FAIL-COUNT       PIC 9(04) VALUE 0.")
        w.b("05  TS-CURRENT-TEST     PIC X(60) VALUE SPACES.")
        w.b("05  TS-ASSERT-MSG       PIC X(80) VALUE SPACES.")
        w.a("01  TS-RESULT               PIC X(04) VALUE 'PASS'.")
        w.b("88  TC-PASSED           VALUE 'PASS'.")
        w.b("88  TC-FAILED           VALUE 'FAIL'.")

        # LINKAGE reproduction (non-CICS subroutines only)
        if target.is_subroutine and not target.is_cics and target.linkage_raw_lines:
            w.blank()
            w.comment()
            w.comment(f" === LINKAGE from {pid} reproduced as WORKING-STORAGE ===")
            for raw_line in target.linkage_raw_lines:
                if raw_line:
                    w.raw(raw_line)
                else:
                    w.blank()

        # CICS interface areas
        if target.is_cics:
            w.blank()
            w.comment()
            w.comment(" === CICS Interface Areas ===")
            w.a("01  DFHEIBLK.")
            w.b("05  EIBCALEN        PIC S9(4) COMP VALUE 0.")
            w.b("05  EIBTRNID        PIC X(4)  VALUE SPACES.")
            w.b("05  EIBDATE         PIC S9(7) COMP-3 VALUE 0.")
            w.b("05  EIBTIME         PIC S9(7) COMP-3 VALUE 0.")
            w.b("05  EIBRESP         PIC S9(8) COMP VALUE 0.")
            w.b("05  EIBRESP2        PIC S9(8) COMP VALUE 0.")
            w.b("05  EIBFN           PIC X(2)  VALUE SPACES.")
            w.b("05  EIBAID          PIC X(1)  VALUE SPACES.")
            w.b("05  EIBATT          PIC X(1)  VALUE SPACES.")
            w.b("05  EIBRSNCD        PIC X(4)  VALUE SPACES.")
            w.a("01  DFHCOMMAREA         PIC X(32768) VALUE SPACES.")

        w.blank()

        # ---- PROCEDURE DIVISION ----
        w.a("PROCEDURE DIVISION.")
        w.a("0000-MAIN.")
        w.b("PERFORM 0100-INIT")
        if not stub_table:
            w.comment(" TODO: No sections/paragraphs to generate stubs for")
        for stub_name, _, _, _ in stub_table:
            w.b(f"PERFORM {stub_name}")
        w.b("PERFORM 9000-REPORT")
        w.b("STOP RUN")
        w.b(".")
        w.blank()

        w.a("0100-INIT.")
        w.b("INITIALIZE WS-TEST-SUITE")
        w.b(".")
        w.blank()

        # ---- Test stubs ----
        for stub_name, orig_name, line_num, kind in stub_table:
            self._write_stub(w, target, stub_name, orig_name, line_num,
                             kind, using_clause)

        # ---- Infrastructure paragraphs ----
        w.comment()
        w.divider()
        w.comment(" Test framework infrastructure")
        w.divider()
        w.blank()

        bar = "=" * 42
        w.a("9000-REPORT.")
        w.b(f"DISPLAY '{bar}'")
        w.b(f"DISPLAY 'TEST RESULTS: {pid}'")
        w.b("DISPLAY 'Total:  ' TS-TEST-COUNT")
        w.b("DISPLAY 'Passed: ' TS-PASS-COUNT")
        w.b("DISPLAY 'Failed: ' TS-FAIL-COUNT")
        w.b(f"DISPLAY '{bar}'")
        w.b(".")
        w.blank()

        w.a("9100-TC-PASS.")
        w.b("MOVE 'PASS' TO TS-RESULT")
        w.b("ADD 1 TO TS-PASS-COUNT")
        w.b("DISPLAY 'PASS: ' TS-CURRENT-TEST")
        w.b(".")
        w.blank()

        w.a("9200-TC-FAIL.")
        w.b("MOVE 'FAIL' TO TS-RESULT")
        w.b("ADD 1 TO TS-FAIL-COUNT")
        w.b("DISPLAY 'FAIL: ' TS-CURRENT-TEST")
        w.b("DISPLAY '      ' TS-ASSERT-MSG")
        w.b(".")
        w.blank()

        return w.getvalue()

    def _write_stub(
        self,
        w:           _Writer,
        target:      TestTarget,
        stub_name:   str,
        orig_name:   str,
        line_num:    int,
        kind:        str,
        using_clause:str,
    ) -> None:
        """Emit one test stub paragraph."""
        pid = target.program_id

        w.comment()
        w.divider()
        w.comment(f" TEST {kind}: {orig_name}  (source line {line_num})")
        if not target.is_subroutine and not target.is_cics:
            w.comment(" *** BATCH: paragraphs cannot be CALL'd externally.        ***")
            w.comment(" *** Each stub drives the full program. Use paragraph name  ***")
            w.comment(" *** as scenario label; customize the SETUP section.        ***")
        w.divider()
        w.a(f"{stub_name}.")

        # Bookkeeping
        w.b(f"MOVE '{stub_name}' TO TS-CURRENT-TEST")
        w.b("ADD 1 TO TS-TEST-COUNT")

        # Setup
        w.comment(" --- SETUP ---")
        w.comment(" TODO: Initialize test inputs")
        if target.is_subroutine and not target.is_cics and target.using_params:
            for param in target.using_params:
                w.b(f"INITIALIZE {param}")
        elif target.is_cics:
            w.b("INITIALIZE DFHCOMMAREA")
            w.b("INITIALIZE DFHEIBLK")
        else:
            w.comment(" (no LINKAGE section — set up file/WS conditions here)")

        # Execute
        w.comment(" --- EXECUTE ---")
        call_stmt = f"CALL '{pid}'{using_clause}"
        w.b(call_stmt)

        # Assert
        w.comment(" --- ASSERT ---")
        w.comment(" TODO: Replace with actual assertions")
        if target.is_subroutine and target.using_params:
            sample = target.using_params[0]
            w.comment(f"*   IF {sample}-some-field = expected-value")
        elif target.is_cics:
            w.comment("*   IF EIBRESP = 0")
        else:
            w.comment("*   IF some-output-field = expected-value")
        w.comment("*       PERFORM 9100-TC-PASS")
        w.comment("*   ELSE")
        w.comment("*       MOVE 'Expected ...' TO TS-ASSERT-MSG")
        w.comment("*       PERFORM 9200-TC-FAIL")
        w.comment("*   END-IF")
        w.b(".")
        w.blank()

    def _using_clause(self, target: TestTarget) -> str:
        """Build the USING clause string for the generated CALL statement."""
        if target.is_cics:
            return " USING DFHEIBLK DFHCOMMAREA"
        if target.is_subroutine and target.using_params:
            return " USING " + " ".join(target.using_params)
        return ""

    # ------------------------------------------------------------------
    # CICS stubs companion file
    # ------------------------------------------------------------------

    def _gen_cics_stubs(self, target: TestTarget) -> str:
        w         = _Writer()
        today     = _date.today().isoformat()
        pid       = target.program_id
        stubs_pid = f"{pid}-STUBS"[:30]

        # Unique operations preserving source encounter order
        unique_ops: list[str] = list(dict.fromkeys(
            c.operation for c in target.cics_commands
        ))

        w.a("IDENTIFICATION DIVISION.")
        w.a(f"PROGRAM-ID. {stubs_pid}.")
        w.comment()
        w.comment(f" CICS stub implementations for {pid}-TEST")
        w.comment(f" Generated: {today}")
        w.comment(f" Simulates: {', '.join(unique_ops)}")
        w.comment(f" Link this object with {pid}-TEST when compiling.")
        w.blank()

        w.a("DATA DIVISION.")
        w.a("WORKING-STORAGE SECTION.")
        w.a("01  STUB-EIBRESP         PIC S9(8) COMP VALUE 0.")
        w.a("01  STUB-EIBRESP2        PIC S9(8) COMP VALUE 0.")

        # File table for READ/WRITE/BROWSE operations
        file_ops = {c.operation for c in target.cics_commands
                    if c.operation in (
                        "READ", "WRITE", "REWRITE", "DELETE",
                        "STARTBR", "READNEXT", "ENDBR", "ENDBRWS",
                    )}
        if file_ops:
            w.a("01  STUB-FILE-TABLE.")
            w.b("05  STUB-FILE-COUNT  PIC 9(2)  VALUE 0.")
            w.b("05  STUB-FILE-ENTRY  OCCURS 20 TIMES.")
            w.b("    10  STUB-FILE-NAME  PIC X(08) VALUE SPACES.")
            w.b("    10  STUB-FILE-DATA  PIC X(4096) VALUE SPACES.")
            w.b("    10  STUB-FILE-STAT  PIC XX    VALUE '00'.")

        # Map area for SEND/RECEIVE MAP operations
        map_ops = {c.operation for c in target.cics_commands
                   if c.operation in ("SEND", "RECEIVE")}
        if map_ops:
            w.a("01  STUB-MAP-AREA        PIC X(4096) VALUE SPACES.")
            w.a("01  STUB-MAP-NAME        PIC X(08)   VALUE SPACES.")

        w.blank()

        w.a("PROCEDURE DIVISION.")
        w.a("0000-MAIN.")
        w.b("STOP RUN")
        w.b(".")
        w.blank()

        for op in unique_ops:
            self._write_cics_op_stub(w, op, target)

        return w.getvalue()

    def _write_cics_op_stub(
        self, w: _Writer, operation: str, target: TestTarget
    ) -> None:
        """Write one stub paragraph for a CICS operation."""
        stub_para = f"STUB-{operation}"[:30]
        cmd = next((c for c in target.cics_commands if c.operation == operation), None)

        w.comment()
        w.comment(f" Stub for EXEC CICS {operation}")
        if cmd and cmd.file_name:
            w.comment(f" File:   {cmd.file_name}")
        if cmd and cmd.map_name:
            w.comment(f" Map:    {cmd.map_name}")
        if cmd and cmd.mapset_name:
            w.comment(f" Mapset: {cmd.mapset_name}")

        w.a(f"{stub_para}.")
        w.b("MOVE 0 TO STUB-EIBRESP")
        w.b("MOVE 0 TO STUB-EIBRESP2")

        op_u = operation.upper()
        if op_u == "READ":
            w.comment(" TODO: Pre-populate STUB-FILE-TABLE with test records")
            w.comment(" TODO: MOVE record data into caller's INTO/SET field")
        elif op_u in ("WRITE", "REWRITE"):
            w.comment(" TODO: Capture written record in STUB-FILE-TABLE for assertion")
        elif op_u == "DELETE":
            w.comment(" TODO: Record the deletion in STUB-FILE-TABLE for assertion")
        elif op_u == "SEND":
            if cmd and cmd.map_name:
                w.comment(f" TODO: Capture map {cmd.map_name} output in STUB-MAP-AREA")
                map_nm = f"'{cmd.map_name}'"
                w.b(f"MOVE {map_nm:<10} TO STUB-MAP-NAME")
            else:
                w.comment(" TODO: Capture SEND output in STUB-MAP-AREA for assertion")
        elif op_u == "RECEIVE":
            w.comment(" TODO: Populate caller's map area with simulated input")
        elif op_u in ("LINK", "XCTL"):
            w.comment(" TODO: Stub the called program or verify program name")
        elif op_u == "RETURN":
            w.comment(" TODO: Simulate CICS RETURN (program ends in production)")
        elif op_u in ("STARTBR", "READNEXT", "ENDBR", "ENDBRWS"):
            w.comment(" TODO: Simulate browse ops against STUB-FILE-TABLE")
        elif op_u == "GETMAIN":
            w.comment(" TODO: Simulate memory allocation (set pointer if required)")
        elif op_u == "FREEMAIN":
            w.comment(" TODO: Simulate memory release")
        elif op_u in ("SYNCPOINT", "SYNCPT"):
            w.comment(" TODO: Simulate CICS syncpoint (no-op in unit test)")
        else:
            w.comment(f" TODO: Implement stub for EXEC CICS {operation}")

        w.b(".")
        w.blank()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coboltest.py",
        description=(
            "Generate a COBOL unit test harness for a single source file.\n"
            "\n"
            "For subroutines (PROCEDURE DIVISION USING) the generated program\n"
            "reproduces the LINKAGE SECTION as WORKING-STORAGE and calls the\n"
            "target with the appropriate USING clause.\n"
            "\n"
            "For CICS programs a companion stubs file is also generated.\n"
            "\n"
            "For batch programs the stubs drive the full program entry-point;\n"
            "paragraph names serve as scenario labels."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "source",
        metavar="source.cbl",
        type=Path,
        help="COBOL source file to analyze",
    )
    p.add_argument(
        "-o", "--output-dir",
        metavar="OUTPUT_DIR",
        type=Path,
        default=None,
        help="Output directory for generated file(s) (default: same dir as source)",
    )
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--sections",
        action="store_const", dest="mode", const="sections",
        help="One test stub per SECTION (default; falls back to --paragraphs if none)",
    )
    mode_group.add_argument(
        "--paragraphs",
        action="store_const", dest="mode", const="paragraphs",
        help="One test stub per PARAGRAPH",
    )
    mode_group.add_argument(
        "--all",
        action="store_const", dest="mode", const="all",
        help="Test stubs for all paragraphs and sections (sorted by source line)",
    )
    p.add_argument(
        "--no-cics-stubs",
        action="store_true",
        help="Suppress CICS stub companion file for CICS programs",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args   = parser.parse_args(argv)

    source_path: Path = args.source.resolve()
    if not source_path.exists():
        print(f"Error: file not found: {source_path}", file=sys.stderr)
        return 1
    if not source_path.is_file():
        print(f"Error: not a file: {source_path}", file=sys.stderr)
        return 1

    output_dir: Path = (
        args.output_dir.resolve() if args.output_dir else source_path.parent
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    mode: str = args.mode or "sections"

    # Parse
    print(f"Parsing {source_path.name} …", file=sys.stderr)
    target = TestTargetParser().parse(source_path)

    print(f"  Program-ID : {target.program_id}", file=sys.stderr)
    print(f"  Type       : {target.program_type}", file=sys.stderr)
    print(f"  Lines      : {target.total_lines}", file=sys.stderr)
    print(f"  Sections   : {len(target.sections)}", file=sys.stderr)
    print(f"  Paragraphs : {len(target.paragraphs)}", file=sys.stderr)
    if target.using_params:
        print(f"  USING      : {', '.join(target.using_params)}", file=sys.stderr)
    if target.is_cics:
        ops = list(dict.fromkeys(c.operation for c in target.cics_commands))
        print(f"  CICS ops   : {', '.join(ops)}", file=sys.stderr)
    print(f"  Copybooks  : {len(target.copybooks)}", file=sys.stderr)

    # Effective mode (fall back sections → paragraphs when no sections exist)
    effective_mode = mode
    if mode == "sections" and not target.sections:
        print(
            "  (no sections found — falling back to --paragraphs)",
            file=sys.stderr,
        )
        effective_mode = "paragraphs"

    # Generate
    gen_stubs = not args.no_cics_stubs
    written   = TestGenerator().generate(target, effective_mode, output_dir, gen_stubs)

    for path in written:
        print(str(path))

    print(f"\nGenerated {len(written)} file(s):", file=sys.stderr)
    for path in written:
        print(f"  {path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
