#!/usr/bin/env python3
"""
appmap.py — COBOL Application Map Generator.

Reads a set of COBOL source files and produces a full application-level map:
  - Program inventory (batch, CICS online, utility subroutines)
  - Inter-program call graph (CALL statements + CICS XCTL transfers)
  - Data file access map (which programs read/write which DD names)
  - CICS transaction registry
  - Copybook dependency table

Usage:
    python appmap.py path/to/cobol/dir/ -v
    python appmap.py file1.cbl file2.cbl -o /output/dir/
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).parent.resolve()
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from cobol_parser import CobolParser


# ---------------------------------------------------------------------------
# Regex patterns for EXEC CICS block parsing
# ---------------------------------------------------------------------------

EXEC_CICS_RE    = re.compile(r'EXEC\s+CICS\s+(\w+)(.*?)END-EXEC', re.DOTALL | re.IGNORECASE)
PROGRAM_RE      = re.compile(r'\bPROGRAM\s*\(\s*([^)]+)\s*\)', re.IGNORECASE)
TRANSID_RE      = re.compile(r'\bTRANSID\s*\(\s*([^)]+)\s*\)', re.IGNORECASE)
MAP_RE          = re.compile(r'\bMAP\s*\(\s*([^)]+)\s*\)', re.IGNORECASE)
MAPSET_RE       = re.compile(r'\bMAPSET\s*\(\s*([^)]+)\s*\)', re.IGNORECASE)
DATASET_RE      = re.compile(r'\bDATASET\s*\(\s*([^)]+)\s*\)', re.IGNORECASE)
FILE_RE         = re.compile(r'\bFILE\s*\(\s*([^)]+)\s*\)', re.IGNORECASE)

# Transaction ID from working-storage VALUE clause
TRANID_VALUE_RE = re.compile(
    r"(?:TRAN|TRANSID|TRANSACTION)[A-Z0-9\-]*\s+PIC\s+[^.]+VALUE\s+'([A-Z0-9]{2,8})'",
    re.IGNORECASE,
)
# Also catch bare VALUE patterns on items with TRAN in the name
TRANID_WS_RE    = re.compile(
    r"TRAN[A-Z0-9\-]*\s+.*VALUE\s+'([A-Z0-9]{2,8})'",
    re.IGNORECASE,
)

# CALL literal pattern  — CALL 'NAME' or CALL "NAME"
CALL_LITERAL_RE = re.compile(r"""CALL\s+['"]([A-Z0-9\$@#\-]+)['"]""", re.IGNORECASE)

# Known non-returning / system calls (not application programs)
SYSTEM_CALLS = {
    "CEE3ABD", "ILBOABN0", "CEEABND", "CEEDAYS", "CEEFMDT", "MVSWAIT",
    "COBDATFT", "CEEDATE", "CEECRHFA", "IGZEDT4", "CSECT",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FileAccess:
    internal_name: str   # COBOL internal SELECT name
    dd_name: str         # JCL DD / CICS dataset name
    organization: str    # INDEXED, SEQUENTIAL, RELATIVE, VSAM
    access_mode: str     # SEQUENTIAL, RANDOM, DYNAMIC, mixed
    record_key: str      # Primary record key (INDEXED only)
    line_num: int


@dataclass
class CallEdge:
    target: str          # Called program name (uppercase)
    call_type: str       # CALL, XCTL, XCTL-DYNAMIC
    is_dynamic: bool     # True if target is a variable, not a literal
    line_num: int
    raw_text: str        # For context


@dataclass
class ProgramInfo:
    program_id: str
    filepath: Path
    total_lines: int
    program_type: str             # BATCH, CICS, UTILITY
    is_subroutine: bool           # PROCEDURE DIVISION USING present
    transaction_id: str           # CICS transaction code (e.g. CC00)
    calls: list[CallEdge] = field(default_factory=list)
    called_by: list[str] = field(default_factory=list)   # reverse index, filled post-parse
    files: list[FileAccess] = field(default_factory=list)
    copybooks: list[str] = field(default_factory=list)
    bms_maps: list[str] = field(default_factory=list)    # BMS mapset names
    cics_datasets: list[str] = field(default_factory=list)  # CICS FILE/DATASET names


# ---------------------------------------------------------------------------
# AppMapParser — scans all four COBOL divisions
# ---------------------------------------------------------------------------

class AppMapParser:
    """
    Scans a COBOL source file across all four divisions to extract
    application-level metadata: files, calls, copybooks, CICS operations.
    """

    def __init__(self):
        self._inner = CobolParser()

    def parse(self, filepath: Path) -> ProgramInfo:
        raw_lines = self._inner._read_lines(filepath)
        logical   = self._inner._join_continuations(raw_lines)
        program_id = self._extract_program_id(logical)

        # Build stripped content for EXEC CICS regex (one string from all non-comment lines)
        content_text = self._build_content_text(raw_lines)

        # State machine pass
        division  = ""
        section   = ""
        sel_buf: list[str] = []    # SELECT block accumulator
        in_select = False
        proc_using = False
        transaction_id = ""
        files: list[FileAccess] = []
        copybooks: list[str] = []
        calls_static: list[CallEdge] = []

        for line_num, content in logical:
            upper = content.upper().strip()

            if not upper:
                continue

            # ---- Division / Section detection --------------------------------
            if "DIVISION" in upper and not upper.startswith("*"):
                division = self._get_keyword(upper, "DIVISION")
                section = ""
                if division == "PROCEDURE" and "USING" in upper:
                    proc_using = True

            if "SECTION" in upper and not upper.startswith("*"):
                section = self._get_section_name(upper)

            # ---- PROGRAM-ID (already handled by inner parser) ----------------

            # ---- FILE-CONTROL: SELECT...ASSIGN (multi-line) ------------------
            if section == "FILE-CONTROL" or (division == "ENVIRONMENT" and "FILE-CONTROL" in upper):
                if "FILE-CONTROL" in upper:
                    section = "FILE-CONTROL"

            if section == "FILE-CONTROL":
                if upper.startswith("SELECT"):
                    in_select = True
                    sel_buf = []
                if in_select:
                    sel_buf.extend(content.split())
                    if content.rstrip().endswith("."):
                        fa = self._parse_select_block(sel_buf, line_num)
                        if fa:
                            files.append(fa)
                        in_select = False
                        sel_buf = []

            # ---- DATA DIVISION: COPY statements ------------------------------
            if division == "DATA" or division == "IDENTIFICATION":
                if upper.startswith("COPY"):
                    cb = self._extract_copy_name(upper)
                    if cb and cb not in copybooks:
                        copybooks.append(cb)

            # ---- Working-storage: transaction ID from VALUE clause -----------
            if section == "WORKING-STORAGE" and not transaction_id:
                m = TRANID_VALUE_RE.search(content)
                if not m:
                    m = TRANID_WS_RE.search(content)
                if m:
                    candidate = m.group(1).strip()
                    if 2 <= len(candidate) <= 8:
                        transaction_id = candidate

            # ---- PROCEDURE DIVISION: COPY statements (rare but exist) --------
            if division == "PROCEDURE":
                if upper.startswith("COPY"):
                    cb = self._extract_copy_name(upper)
                    if cb and cb not in copybooks:
                        copybooks.append(cb)

                # Static CALL statements
                for m in CALL_LITERAL_RE.finditer(content):
                    target = m.group(1).upper()
                    if target not in SYSTEM_CALLS:
                        calls_static.append(CallEdge(
                            target=target,
                            call_type="CALL",
                            is_dynamic=False,
                            line_num=line_num,
                            raw_text=content.strip(),
                        ))

        # ---- EXEC CICS block analysis (regex over full content text) ---------
        cics_detected = bool(EXEC_CICS_RE.search(content_text))
        cics_calls, bms_maps, cics_datasets, cics_transid = self._parse_cics_blocks(
            content_text, program_id
        )
        if cics_transid and not transaction_id:
            transaction_id = cics_transid

        # ---- Classify program type -------------------------------------------
        if cics_detected:
            program_type = "CICS"
        elif proc_using:
            program_type = "UTILITY"
        else:
            program_type = "BATCH"

        # Deduplicate calls: merge static CALLs + CICS calls; keep unique by (target, call_type)
        all_calls = self._dedup_calls(calls_static + cics_calls)

        return ProgramInfo(
            program_id=program_id.upper(),
            filepath=filepath,
            total_lines=len(raw_lines),
            program_type=program_type,
            is_subroutine=proc_using,
            transaction_id=transaction_id,
            calls=all_calls,
            files=files,
            copybooks=copybooks,
            bms_maps=sorted(set(bms_maps)),
            cics_datasets=sorted(set(cics_datasets)),
        )

    # ------------------------------------------------------------------
    # EXEC CICS block parser
    # ------------------------------------------------------------------

    def _parse_cics_blocks(
        self, content_text: str, program_id: str
    ) -> tuple[list[CallEdge], list[str], list[str], str]:
        calls: list[CallEdge] = []
        maps: list[str] = []
        datasets: list[str] = []
        transid = ""
        seen_calls: set[tuple] = set()

        for match in EXEC_CICS_RE.finditer(content_text):
            operation = match.group(1).upper()
            params    = match.group(2)

            # XCTL / LINK → inter-program transfer
            if operation in ("XCTL", "LINK"):
                pm = PROGRAM_RE.search(params)
                if pm:
                    raw_prog = pm.group(1).strip()
                    is_dynamic = raw_prog[0] not in ("'", '"')
                    target = raw_prog.strip("'\"").upper()
                    key = (target, operation)
                    if key not in seen_calls:
                        seen_calls.add(key)
                        calls.append(CallEdge(
                            target=target,
                            call_type="XCTL" if operation == "XCTL" else "LINK",
                            is_dynamic=is_dynamic,
                            line_num=0,
                            raw_text=f"EXEC CICS {operation} PROGRAM({raw_prog})",
                        ))

            # RETURN TRANSID → self-transaction code (only quoted literals, not variables)
            if operation == "RETURN":
                tm = TRANSID_RE.search(params)
                if tm and not transid:
                    raw_tid = tm.group(1).strip()
                    if raw_tid and raw_tid[0] in ("'", '"'):
                        transid = raw_tid.strip("'\"").upper()

            # MAP / MAPSET references
            mm = MAPSET_RE.search(params)
            if mm:
                ms = mm.group(1).strip().strip("'\"").upper()
                if ms:
                    maps.append(ms)

            # DATASET and FILE references
            for pat in (DATASET_RE, FILE_RE):
                dm = pat.search(params)
                if dm:
                    ds = dm.group(1).strip().strip("'\"").upper()
                    if ds and not ds.startswith("("):
                        datasets.append(ds)

        return calls, maps, datasets, transid

    # ------------------------------------------------------------------
    # SELECT...ASSIGN block parser
    # ------------------------------------------------------------------

    def _parse_select_block(
        self, tokens: list[str], line_num: int
    ) -> Optional[FileAccess]:
        upper = [t.upper().rstrip(".,") for t in tokens]
        try:
            sel_idx = upper.index("SELECT")
        except ValueError:
            return None

        internal_name = upper[sel_idx + 1] if sel_idx + 1 < len(upper) else ""
        if not internal_name:
            return None

        dd_name = ""
        organization = "SEQUENTIAL"
        access_mode = "SEQUENTIAL"
        record_key = ""

        i = 0
        while i < len(upper):
            t = upper[i]
            if t == "ASSIGN" and i + 1 < len(upper):
                # ASSIGN TO ddname  (skip "TO" if present)
                nxt = upper[i + 1]
                if nxt == "TO" and i + 2 < len(upper):
                    dd_name = upper[i + 2].lstrip("-")
                elif nxt not in ("TO", "IS"):
                    dd_name = nxt.lstrip("-")
            elif t == "ORGANIZATION" and i + 2 < len(upper):
                organization = upper[i + 2]   # skip "IS"
            elif t == "ACCESS" and i + 1 < len(upper):
                # Handles: ACCESS MODE IS DYNAMIC / ACCESS MODE DYNAMIC / ACCESS DYNAMIC
                nxt = upper[i + 1]
                if nxt == "MODE" and i + 2 < len(upper):
                    val = upper[i + 2]
                    if val == "IS" and i + 3 < len(upper):
                        access_mode = upper[i + 3]
                    else:
                        access_mode = val
                elif nxt == "IS" and i + 2 < len(upper):
                    access_mode = upper[i + 2]
                elif nxt not in ("IS", "MODE"):
                    access_mode = nxt
            elif t in ("RECORD", "KEY") and "KEY" in upper[i:i+2]:
                for j in range(i, min(i + 4, len(upper))):
                    if upper[j] in ("IS", "KEY", "RECORD"):
                        continue
                    record_key = upper[j]
                    break
            i += 1

        # Strip hyphen prefix from ASSIGN names like "-ACCTFILE"
        dd_name = dd_name.strip().lstrip("S-")
        # Some assign names have S- prefix (ASSIGN TO S-DDNAME on some compilers)
        # Keep as-is if no hyphen

        return FileAccess(
            internal_name=internal_name,
            dd_name=dd_name or internal_name,
            organization=organization,
            access_mode=access_mode,
            record_key=record_key,
            line_num=line_num,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_program_id(self, logical: list[tuple[int, str]]) -> str:
        """
        Extract PROGRAM-ID handling both same-line and next-line layouts:
          PROGRAM-ID. CBACT01C.          (single line)
          PROGRAM-ID.                    (name on the next logical line)
              COACTUPC.
        """
        pid_next = False
        for _, content in logical:
            upper = content.upper().strip()
            if pid_next:
                # Previous line was bare PROGRAM-ID. — name is here
                name = re.split(r"[\s.]+", upper)[0]
                if name and re.match(r"^[A-Z0-9][A-Z0-9\-]+$", name):
                    return name
                pid_next = False
            if upper.startswith("PROGRAM-ID"):
                parts = [p for p in re.split(r"[\s.]+", upper) if p]
                if len(parts) >= 2:
                    return parts[1]
                pid_next = True  # name expected on next logical line
        return "UNKNOWN"

    def _build_content_text(self, raw_lines: list[tuple[int, str]]) -> str:
        """Build a single string from all non-comment content for regex scanning."""
        parts = []
        for _, raw in raw_lines:
            if len(raw) < 7:
                continue
            if raw[6] in ("*", "/"):
                continue
            parts.append(raw[7:72] if len(raw) > 7 else "")
        return " ".join(parts)

    def _get_keyword(self, upper: str, word: str) -> str:
        """Extract the word immediately before 'word' in a division header."""
        parts = [p.rstrip(".,;") for p in upper.split()]
        idx = next((i for i, p in enumerate(parts) if p == word), -1)
        if idx > 0:
            return parts[idx - 1]
        return ""

    def _get_section_name(self, upper: str) -> str:
        """Extract the section name from 'NAME SECTION.' pattern."""
        parts = [p.rstrip(".,;") for p in upper.split()]
        idx = next((i for i, p in enumerate(parts) if p == "SECTION"), -1)
        if idx > 0:
            return parts[idx - 1]
        return ""

    def _extract_copy_name(self, upper: str) -> str:
        """Extract copybook name from COPY name. statement (handles quoted names too)."""
        parts = upper.split()
        if len(parts) >= 2:
            name = parts[1].rstrip(".,;").strip("'\"")
            # Skip REPLACING and other modifiers
            if name not in ("REPLACING", "IN", "OF") and re.match(r"^[A-Z0-9][A-Z0-9\-]*$", name):
                return name
        return ""

    def _dedup_calls(self, calls: list[CallEdge]) -> list[CallEdge]:
        """Remove duplicate call edges (same target + call_type), keeping first occurrence."""
        seen: set[tuple] = set()
        result = []
        for c in calls:
            key = (c.target, c.call_type)
            if key not in seen:
                seen.add(key)
                result.append(c)
        return result


# ---------------------------------------------------------------------------
# AppMapAnalyzer — cross-program analysis
# ---------------------------------------------------------------------------

class AppMapAnalyzer:
    """Builds cross-program relationships from a list of ProgramInfo objects."""

    def analyze(self, programs: list[ProgramInfo]) -> "AppMap":
        prog_map = {p.program_id: p for p in programs}

        # Fill called_by reverse index
        for prog in programs:
            for call in prog.calls:
                target_id = call.target.upper()
                if target_id in prog_map:
                    callee = prog_map[target_id]
                    if prog.program_id not in callee.called_by:
                        callee.called_by.append(prog.program_id)

        # Build file access map: dd_name → list of program_ids
        file_map: dict[str, list[str]] = {}
        for prog in programs:
            for fa in prog.files:
                dd = fa.dd_name.upper()
                if dd not in file_map:
                    file_map[dd] = []
                if prog.program_id not in file_map[dd]:
                    file_map[dd].append(prog.program_id)
            for ds in prog.cics_datasets:
                dd = ds.upper()
                if dd not in file_map:
                    file_map[dd] = []
                if prog.program_id not in file_map[dd]:
                    file_map[dd].append(prog.program_id)

        # Build copybook usage map: copybook → list of program_ids
        copy_map: dict[str, list[str]] = {}
        for prog in programs:
            for cb in prog.copybooks:
                if cb not in copy_map:
                    copy_map[cb] = []
                copy_map[cb].append(prog.program_id)

        return AppMap(
            programs=programs,
            prog_map=prog_map,
            file_map=file_map,
            copy_map=copy_map,
        )


@dataclass
class AppMap:
    programs: list[ProgramInfo]
    prog_map: dict[str, ProgramInfo]
    file_map: dict[str, list[str]]   # dd_name → [program_ids]
    copy_map: dict[str, list[str]]   # copybook → [program_ids]


# ---------------------------------------------------------------------------
# AppMapReport — markdown generator
# ---------------------------------------------------------------------------

class AppMapReport:

    def generate(self, app: AppMap, source_paths: list[str]) -> str:
        now = datetime.now()
        total = len(app.programs)
        cics   = [p for p in app.programs if p.program_type == "CICS"]
        batch  = [p for p in app.programs if p.program_type == "BATCH"]
        utils  = [p for p in app.programs if p.program_type == "UTILITY"]

        lines: list[str] = []
        lines += self._header(now, total, source_paths)
        lines += self._inventory(batch, cics, utils)
        lines += self._call_graph(app, batch, cics, utils)
        lines += self._cics_registry(cics)
        lines += self._file_access_map(app)
        lines += self._copybook_table(app)
        lines += self._statistics(app, batch, cics, utils)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _header(self, now: datetime, total: int, paths: list[str]) -> list[str]:
        path_str = paths[0] if len(paths) == 1 else f"{len(paths)} paths"
        return [
            "# COBOL Application Map",
            "",
            f"**Generated:** {now.strftime('%Y-%m-%d %H:%M:%S')}  ",
            f"**Source:** `{path_str}`  ",
            f"**Programs analyzed:** {total}",
            "",
            "---",
            "",
        ]

    # ------------------------------------------------------------------
    # Program Inventory
    # ------------------------------------------------------------------

    def _inventory(
        self,
        batch: list[ProgramInfo],
        cics: list[ProgramInfo],
        utils: list[ProgramInfo],
    ) -> list[str]:
        lines = ["## Program Inventory", ""]

        def prog_table(progs: list[ProgramInfo]) -> list[str]:
            rows = ["| Program | Lines | Calls | Files | Copybooks | Called By |",
                    "|---|---|---|---|---|---|"]
            for p in sorted(progs, key=lambda x: x.program_id):
                app_calls = [c for c in p.calls if c.call_type in ("CALL", "LINK")]
                xctls     = [c for c in p.calls if c.call_type in ("XCTL", "XCTL-DYNAMIC")]
                call_str  = ", ".join(c.target for c in app_calls) if app_calls else "—"
                xctl_str  = ", ".join(c.target for c in xctls) if xctls else ""
                all_calls = ", ".join(filter(None, [call_str if call_str != "—" else "", xctl_str])) or "—"
                files_str = ", ".join(fa.dd_name for fa in p.files) or (
                    ", ".join(p.cics_datasets) if p.cics_datasets else "—"
                )
                cbs_str   = str(len(p.copybooks)) if p.copybooks else "—"
                callers   = ", ".join(p.called_by) if p.called_by else "—"
                rows.append(
                    f"| `{p.program_id}` | {p.total_lines:,} | {all_calls} "
                    f"| {files_str} | {cbs_str} | {callers} |"
                )
            return rows

        if batch:
            lines += [f"### Batch Programs ({len(batch)})", ""]
            lines += prog_table(batch)
            lines.append("")

        if cics:
            lines += [f"### CICS Online Programs ({len(cics)})", ""]
            lines += prog_table(cics)
            lines.append("")

        if utils:
            lines += [f"### Utility / Subroutines ({len(utils)})", ""]
            lines += prog_table(utils)
            lines.append("")

        lines += ["---", ""]
        return lines

    # ------------------------------------------------------------------
    # Call graph
    # ------------------------------------------------------------------

    def _call_graph(
        self,
        app: AppMap,
        batch: list[ProgramInfo],
        cics: list[ProgramInfo],
        utils: list[ProgramInfo],
    ) -> list[str]:
        lines = ["## Call Graph", ""]

        # Batch call graph — programs that call other programs
        batch_callers = [p for p in batch if p.calls]
        if batch_callers:
            lines += ["### Batch", "```"]
            for p in sorted(batch_callers, key=lambda x: x.program_id):
                for call in p.calls:
                    dyn = " (dynamic)" if call.is_dynamic else ""
                    lines.append(f"{p.program_id}  →  {call.target}  [{call.call_type}{dyn}]")
            lines += ["```", ""]

        # CICS chain — entry points and XCTL chains
        if cics:
            lines += ["### CICS Transaction Flow", ""]
            # Entry points: CICS programs not called by any other CICS program
            cics_ids = {p.program_id for p in cics}
            entry_points = [
                p for p in cics
                if not any(c in cics_ids for c in p.called_by)
            ]
            rendered: set[str] = set()

            def render_cics(prog: ProgramInfo, depth: int) -> list[str]:
                indent = "    " * depth
                tid = f" ({prog.transaction_id})" if prog.transaction_id else ""
                row = [f"{indent}{'└── ' if depth else ''}`{prog.program_id}`{tid}"]
                rendered.add(prog.program_id)
                xctls = [c for c in prog.calls if c.call_type in ("XCTL", "LINK")]
                for call in sorted(xctls, key=lambda x: x.target):
                    child = app.prog_map.get(call.target)
                    if child and child.program_id not in rendered:
                        row += render_cics(child, depth + 1)
                    elif call.is_dynamic:
                        row.append(f"{'    ' * (depth+1)}└── `{call.target}` (dynamic — varies at runtime)")
                    else:
                        row.append(f"{'    ' * (depth+1)}└── `{call.target}` (external)")
                return row

            lines.append("```")
            for ep in sorted(entry_points, key=lambda x: x.transaction_id or x.program_id):
                lines += render_cics(ep, 0)
            # Any CICS programs not yet rendered
            for p in sorted(cics, key=lambda x: x.program_id):
                if p.program_id not in rendered:
                    lines += render_cics(p, 0)
            lines += ["```", ""]

        lines += ["---", ""]
        return lines

    # ------------------------------------------------------------------
    # CICS Transaction Registry
    # ------------------------------------------------------------------

    def _cics_registry(self, cics: list[ProgramInfo]) -> list[str]:
        if not cics:
            return []
        lines = [
            "## CICS Transaction Registry",
            "",
            "| Transaction | Program | BMS Mapsets | Subroutine Calls |",
            "|---|---|---|---|",
        ]
        for p in sorted(cics, key=lambda x: x.transaction_id or x.program_id):
            tid  = p.transaction_id or "—"
            maps = ", ".join(p.bms_maps) if p.bms_maps else "—"
            sub_calls = [c for c in p.calls if c.call_type == "CALL"]
            subs = ", ".join(c.target for c in sub_calls) if sub_calls else "—"
            lines.append(f"| `{tid}` | `{p.program_id}` | {maps} | {subs} |")
        lines += ["", "---", ""]
        return lines

    # ------------------------------------------------------------------
    # File Access Map
    # ------------------------------------------------------------------

    def _file_access_map(self, app: AppMap) -> list[str]:
        lines = [
            "## Data File Access Map",
            "",
            "| DD Name | Organization | Access | Programs |",
            "|---|---|---|---|",
        ]

        # Gather file metadata
        file_meta: dict[str, FileAccess] = {}
        for prog in app.programs:
            for fa in prog.files:
                dd = fa.dd_name.upper()
                if dd not in file_meta:
                    file_meta[dd] = fa

        for dd in sorted(app.file_map.keys()):
            progs = ", ".join(sorted(app.file_map[dd]))
            if dd in file_meta:
                fa   = file_meta[dd]
                org  = fa.organization
                acc  = fa.access_mode
            else:
                org  = "CICS"
                acc  = "RANDOM"
            lines.append(f"| `{dd}` | {org} | {acc} | {progs} |")

        lines += ["", "---", ""]
        return lines

    # ------------------------------------------------------------------
    # Copybook Usage Table
    # ------------------------------------------------------------------

    def _copybook_table(self, app: AppMap) -> list[str]:
        lines = [
            "## Copybook Dependencies",
            "",
            "| Copybook | Used By (programs) |",
            "|---|---|",
        ]
        for cb in sorted(app.copy_map.keys()):
            progs = ", ".join(sorted(app.copy_map[cb]))
            lines.append(f"| `{cb}` | {progs} |")
        lines += ["", "---", ""]
        return lines

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def _statistics(
        self,
        app: AppMap,
        batch: list[ProgramInfo],
        cics: list[ProgramInfo],
        utils: list[ProgramInfo],
    ) -> list[str]:
        total_calls  = sum(len(p.calls) for p in app.programs)
        total_files  = len(app.file_map)
        total_copies = len(app.copy_map)
        with_txn     = sum(1 for p in cics if p.transaction_id)

        return [
            "## Summary Statistics",
            "",
            f"| Metric | Count |",
            f"|---|---|",
            f"| Total programs | {len(app.programs)} |",
            f"| Batch programs | {len(batch)} |",
            f"| CICS online programs | {len(cics)} |",
            f"| Utility / subroutines | {len(utils)} |",
            f"| Inter-program call edges | {total_calls} |",
            f"| Distinct data files (DD names) | {total_files} |",
            f"| Distinct copybooks | {total_copies} |",
            f"| CICS programs with transaction IDs | {with_txn} |",
            "",
        ]


# ---------------------------------------------------------------------------
# File discovery (same logic as analyze.py)
# ---------------------------------------------------------------------------

def collect_cbl_files(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for p in paths:
        target = Path(p)
        if target.is_file():
            if target.suffix.lower() in (".cbl", ".cob", ".cobol"):
                found.append(target.resolve())
        elif target.is_dir():
            for ext in ("*.cbl", "*.CBL", "*.cob", "*.COB"):
                found.extend(sorted(target.glob(ext)))
        else:
            print(f"WARNING: {target} not found — skipping", file=sys.stderr)
    seen: set[Path] = set()
    return [f for f in found if not (f in seen or seen.add(f))]  # type: ignore[func-returns-value]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="appmap.py",
        description=(
            "COBOL Application Map Generator.\n"
            "Scans COBOL source files and produces a full program map:\n"
            "call graph, file access map, CICS registry, copybook dependencies."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Map all programs in a directory
  python appmap.py path/to/cobol/dir/ -v

  # Map specific files, write report to /tmp/
  python appmap.py COSGN00C.cbl COMEN01C.cbl CBACT01C.cbl -o /tmp/

  # Map the full CardDemo project
  python rustproofing/appmap.py aws-mainframe-modernization-carddemo/app/cbl/ -v
""",
    )
    p.add_argument("paths", nargs="+", metavar="PATH",
                   help="One or more .cbl files or directories")
    p.add_argument("-o", "--output-dir", default=None, metavar="DIR",
                   help="Directory for report output (default: same dir as appmap.py)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print per-file progress")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    output_dir = Path(args.output_dir).resolve() if args.output_dir else _HERE
    output_dir.mkdir(parents=True, exist_ok=True)

    files = collect_cbl_files(args.paths)
    if not files:
        print("ERROR: No COBOL source files found.", file=sys.stderr)
        return 1

    print(f"Mapping {len(files)} COBOL source file(s)...", file=sys.stderr)

    parser   = AppMapParser()
    programs = []
    for filepath in files:
        if args.verbose:
            print(f"  Parsing {filepath.name} ...", end="", flush=True)
        try:
            info = parser.parse(filepath)
            programs.append(info)
            if args.verbose:
                calls_str = f"{len(info.calls)} calls" if info.calls else "no calls"
                print(f"  {info.program_type:<8} {calls_str}")
        except Exception as exc:
            print(f"\n  ERROR parsing {filepath.name}: {exc}", file=sys.stderr)

    if not programs:
        print("ERROR: No files successfully parsed.", file=sys.stderr)
        return 1

    app    = AppMapAnalyzer().analyze(programs)
    report = AppMapReport().generate(app, args.paths)

    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"appmap_report_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")

    print(f"\nReport written to: {report_path}", file=sys.stderr)
    print(str(report_path))

    # Brief stdout summary
    print("", file=sys.stderr)
    cics  = [p for p in programs if p.program_type == "CICS"]
    batch = [p for p in programs if p.program_type == "BATCH"]
    utils = [p for p in programs if p.program_type == "UTILITY"]
    print(f"  BATCH:   {len(batch):3} programs", file=sys.stderr)
    print(f"  CICS:    {len(cics):3} programs", file=sys.stderr)
    print(f"  UTILITY: {len(utils):3} programs", file=sys.stderr)
    print(f"  Files:   {len(app.file_map):3} DD names", file=sys.stderr)
    print(f"  Copybooks: {len(app.copy_map)}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
