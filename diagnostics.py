"""
diagnostics.py — IBM Rust Proofing Structural Diagnostics detector.

Implements the checks defined in RustProofing.html, Structural Diagnostics section.
Each check returns a list of Diagnostic instances found in a CobolSource.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from cobol_parser import (
    CobolSource, Paragraph, Statement,
    NON_RETURNING_CALLS, INLINE_PERFORM_KEYWORDS,
)


# ---------------------------------------------------------------------------
# Severity thresholds
# ---------------------------------------------------------------------------

SEVERITY_CLEAN = "CLEAN"
SEVERITY_LOW = "LOW"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_HIGH = "HIGH"


def severity_for(count: int) -> str:
    if count == 0:
        return SEVERITY_CLEAN
    if count <= 3:
        return SEVERITY_LOW
    if count <= 8:
        return SEVERITY_MEDIUM
    return SEVERITY_HIGH


# ---------------------------------------------------------------------------
# Diagnostic record
# ---------------------------------------------------------------------------

@dataclass
class Diagnostic:
    diag_type: str     # e.g. "ALTER statement"
    paragraph: str     # paragraph name where the issue was found
    line_num: int      # source line number
    detail: str        # human-readable description


# ---------------------------------------------------------------------------
# Analysis result for one program
# ---------------------------------------------------------------------------

@dataclass
class ProgramResult:
    program_name: str
    filepath: str
    total_lines: int
    diagnostics: list[Diagnostic]
    informational: list[Diagnostic]   # non-returning CALLs etc. — informational only

    @property
    def severity(self) -> str:
        return severity_for(len(self.diagnostics))

    @property
    def issue_summary(self) -> str:
        if not self.diagnostics:
            return "—"
        counts: dict[str, int] = {}
        for d in self.diagnostics:
            counts[d.diag_type] = counts.get(d.diag_type, 0) + 1
        parts = []
        for dtype, cnt in counts.items():
            parts.append(f"{cnt}× {dtype}" if cnt > 1 else dtype)
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokens_of(stmt: Statement) -> list[str]:
    return [t.upper().rstrip(".,;") for t in stmt.tokens]


def _goto_target(stmt: Statement) -> Optional[str]:
    """Return the GO TO target paragraph name, or None."""
    if stmt.verb != "GO TO":
        return None
    toks = _tokens_of(stmt)
    # toks[0] == "GO TO" (combined two-word verb token), target is toks[1]
    if len(toks) >= 2:
        return toks[1].rstrip(".")
    return None


def _perform_targets(stmt: Statement) -> tuple[Optional[str], Optional[str]]:
    """
    Return (start_para, thru_para) for a PERFORM statement.
    thru_para is None if no THRU clause.
    Returns (None, None) for inline PERFORMs.
    """
    if stmt.verb != "PERFORM":
        return None, None
    toks = _tokens_of(stmt)
    # toks[0] == "PERFORM"
    rest = toks[1:]
    if not rest:
        return None, None
    # If first token after PERFORM is a keyword → inline
    if rest[0] in INLINE_PERFORM_KEYWORDS or rest[0] in ("END-PERFORM",):
        return None, None
    start = rest[0].rstrip(".")
    # Check for THRU / THROUGH
    thru = None
    for i, t in enumerate(rest):
        if t in ("THRU", "THROUGH") and i + 1 < len(rest):
            thru = rest[i + 1].rstrip(".")
            break
    return start, thru


def _call_target(stmt: Statement) -> Optional[str]:
    """Return the literal name of a CALL target, or None for dynamic calls."""
    if stmt.verb != "CALL":
        return None
    toks = _tokens_of(stmt)
    if len(toks) < 2:
        return None
    target = toks[1].strip("'\"")
    # Dynamic if it's an identifier (no quotes originally)
    raw_toks = stmt.tokens
    if len(raw_toks) > 1 and raw_toks[1][0] in ('"', "'"):
        return target.upper()
    return None


# ---------------------------------------------------------------------------
# Main analyser
# ---------------------------------------------------------------------------

class DiagnosticsAnalyzer:
    """Run all structural diagnostic checks on a CobolSource."""

    def analyze(self, source: CobolSource) -> ProgramResult:
        diagnostics: list[Diagnostic] = []
        informational: list[Diagnostic] = []

        diagnostics += self.check_alter(source)
        diagnostics += self.check_multiple_termination(source)
        diagnostics += self.check_next_sentence(source)
        diagnostics += self.check_goto(source)
        diagnostics += self.check_perform_thru_violations(source)
        # check_inline_perform omitted: "by shop standard" optional per RustProofing.html
        diagnostics += self.check_recursive_perform(source)
        diagnostics += self.check_unentered_procedures(source)
        diagnostics += self.check_unresolved_perform(source)
        diagnostics += self.check_fall_thru(source)
        diagnostics += self.check_section_null_fallthru(source)

        informational += self.check_nonreturning_calls(source)

        return ProgramResult(
            program_name=source.program_id,
            filepath=str(source.filepath),
            total_lines=source.total_lines,
            diagnostics=diagnostics,
            informational=informational,
        )

    # ------------------------------------------------------------------
    # 1. ALTER statements
    # ------------------------------------------------------------------

    def check_alter(self, source: CobolSource) -> list[Diagnostic]:
        results = []
        for para, stmt in source.all_statements:
            if stmt.verb == "ALTER":
                toks = stmt.tokens
                # ALTER para TO PROCEED TO para2
                target_from = toks[1].rstrip(".") if len(toks) > 1 else "?"
                target_to = toks[-1].rstrip(".") if toks else "?"
                results.append(Diagnostic(
                    diag_type="ALTER statement",
                    paragraph=para.name,
                    line_num=stmt.line_num,
                    detail=(
                        f"ALTER {target_from} → {target_to}: "
                        "dynamically modifies GO TO target at runtime; "
                        "prevents static analysis and automated structuring"
                    ),
                ))
        return results

    # ------------------------------------------------------------------
    # 2. Multiple termination statements
    # ------------------------------------------------------------------

    def check_multiple_termination(self, source: CobolSource) -> list[Diagnostic]:
        term_stmts = []
        for para, stmt in source.all_statements:
            if stmt.verb in ("GOBACK", "STOP RUN", "EXIT PROGRAM"):
                term_stmts.append((para, stmt))
        if len(term_stmts) > 1:
            # Only flag if count > 1 — report all occurrences
            return [
                Diagnostic(
                    diag_type="Multiple termination statements",
                    paragraph=para.name,
                    line_num=stmt.line_num,
                    detail=(
                        f"{stmt.verb} — {len(term_stmts)} termination verbs found "
                        "in program; only one is standard"
                    ),
                )
                for para, stmt in term_stmts
            ]
        return []

    # ------------------------------------------------------------------
    # 3. Non-standard NEXT SENTENCE
    # ------------------------------------------------------------------

    def check_next_sentence(self, source: CobolSource) -> list[Diagnostic]:
        results = []
        for para, stmt in source.all_statements:
            if stmt.verb == "NEXT SENTENCE":
                results.append(Diagnostic(
                    diag_type="Non-standard NEXT SENTENCE",
                    paragraph=para.name,
                    line_num=stmt.line_num,
                    detail=(
                        "NEXT SENTENCE abandons the current conditional scope; "
                        "replace with CONTINUE or restructure the IF block"
                    ),
                ))
        return results

    # ------------------------------------------------------------------
    # 4. GO TO classification
    # ------------------------------------------------------------------

    def _is_goback_stub(self, para: Paragraph) -> bool:
        """Return True if a paragraph contains only GOBACK (a named exit stub)."""
        meaningful = [s for s in para.statements
                      if s.verb not in ("EXIT", "CONTINUE")]
        return len(meaningful) == 1 and meaningful[0].verb in ("GOBACK", "STOP RUN", "EXIT PROGRAM")

    def check_goto(self, source: CobolSource) -> list[Diagnostic]:
        """
        Classify each GO TO as standard or non-standard.
        Standard GO TOs:
          - Target is an EXIT-only paragraph (name ends -EXIT, or is_exit_para)
          - Target is a GOBACK-stub paragraph (contains only GOBACK/STOP RUN —
            a common early-exit convention like 9999-GOBACK)
          - Target is the immediately preceding paragraph (local backward loop)
        Non-standard GO TOs: everything else.
        """
        results = []
        exit_para_names = {
            p.name for p in source.paragraphs if p.is_exit_para
        }
        goback_stub_names = {
            p.name for p in source.paragraphs if self._is_goback_stub(p)
        }
        para_names = {p.name for p in source.paragraphs}

        for para, stmt in source.all_statements:
            if stmt.verb != "GO TO":
                continue
            target = _goto_target(stmt)
            if not target:
                continue
            target_upper = target.upper()

            # Standard: target is an EXIT paragraph or GOBACK stub
            if target_upper in exit_para_names or target_upper in goback_stub_names:
                continue

            # Standard: target is the immediately preceding paragraph (local loop)
            if para.source_order > 0:
                prev_para = source.paragraph_at_order(para.source_order - 1)
                if prev_para and prev_para.name == target_upper:
                    continue

            # Non-standard GO TO
            if target_upper not in para_names:
                label = "Unresolved GO TO"
            elif target_upper == para.name:
                label = "GO TO loop"
            else:
                label = "Non-standard GO TO"

            results.append(Diagnostic(
                diag_type=label,
                paragraph=para.name,
                line_num=stmt.line_num,
                detail=(
                    f"GO TO {target}: branches to paragraph outside a PERFORM range "
                    "exit, or creates non-local control flow"
                ),
            ))
        return results

    # ------------------------------------------------------------------
    # 5. PERFORM THRU range violations (PRV / LPRV)
    # ------------------------------------------------------------------

    def check_perform_thru_violations(self, source: CobolSource) -> list[Diagnostic]:
        """
        For each PERFORM A THRU B, determine the set of paragraphs in A..B.
        Any GO TO inside that range targeting a paragraph OUTSIDE the range = PRV.
        A PRV whose target is earlier in source order than A = LPRV (looping).
        """
        results = []
        # Collect all PERFORM THRU declarations
        perform_thrus: list[tuple[str, str, int]] = []  # (start, end, line_num)
        for para, stmt in source.all_statements:
            if stmt.verb == "PERFORM":
                start, thru = _perform_targets(stmt)
                if start and thru:
                    perform_thrus.append((start.upper(), thru.upper(), stmt.line_num))

        # For each GO TO, check if it violates any enclosing PERFORM THRU range
        for para, stmt in source.all_statements:
            if stmt.verb != "GO TO":
                continue
            target = _goto_target(stmt)
            if not target:
                continue
            target_upper = target.upper()

            for start, end, perform_line in perform_thrus:
                range_paras = source.paragraphs_in_range(start, end)
                range_names = {p.name for p in range_paras}
                if para.name not in range_names:
                    continue  # This GO TO is not inside this PERFORM THRU range
                if target_upper in range_names:
                    continue  # Target is inside the range — fine
                # Target is outside the range → PRV
                target_para = source.para_map.get(target_upper)
                if target_para:
                    target_order = target_para.source_order
                    start_para = source.para_map.get(start)
                    if start_para and target_order < start_para.source_order:
                        diag_type = "Looping PERFORM range violation (LPRV)"
                        detail = (
                            f"GO TO {target} exits PERFORM {start} THRU {end} range "
                            "backwards, creating potential recursion"
                        )
                    else:
                        diag_type = "PERFORM range violation (PRV)"
                        detail = (
                            f"GO TO {target} exits PERFORM {start} THRU {end} range "
                            "via GO TO rather than EXIT paragraph"
                        )
                else:
                    diag_type = "PERFORM range violation (PRV)"
                    detail = (
                        f"GO TO {target} exits PERFORM {start} THRU {end} range "
                        "(target paragraph not found)"
                    )
                results.append(Diagnostic(
                    diag_type=diag_type,
                    paragraph=para.name,
                    line_num=stmt.line_num,
                    detail=detail,
                ))
        return results

    # ------------------------------------------------------------------
    # 6. Inline PERFORMs
    # ------------------------------------------------------------------

    def check_inline_perform(self, source: CobolSource) -> list[Diagnostic]:
        """
        Flag PERFORM statements that are inline (no named paragraph reference).
        Inline = PERFORM followed immediately by UNTIL/VARYING/TIMES/TEST.
        """
        results = []
        for para, stmt in source.all_statements:
            if stmt.verb != "PERFORM":
                continue
            toks = _tokens_of(stmt)
            rest = toks[1:]
            if not rest:
                continue
            first = rest[0].rstrip(".")
            if first in INLINE_PERFORM_KEYWORDS:
                results.append(Diagnostic(
                    diag_type="In-line PERFORM",
                    paragraph=para.name,
                    line_num=stmt.line_num,
                    detail=(
                        f"PERFORM {first} ...: inline PERFORM without a named paragraph; "
                        "consider extracting to a named paragraph"
                    ),
                ))
        return results

    # ------------------------------------------------------------------
    # 7. Recursive PERFORM
    # ------------------------------------------------------------------

    def check_recursive_perform(self, source: CobolSource) -> list[Diagnostic]:
        """
        Build a call graph of PERFORM relationships and detect cycles.
        A cycle means paragraph A (directly or indirectly) PERFORMs itself.
        """
        # Build adjacency: para_name → set of directly PERFORMed para names
        graph: dict[str, set[str]] = {p.name: set() for p in source.paragraphs}
        perform_stmt_map: dict[tuple[str, str], Statement] = {}

        for para, stmt in source.all_statements:
            if stmt.verb != "PERFORM":
                continue
            start, thru = _perform_targets(stmt)
            if start:
                s = start.upper()
                graph[para.name].add(s)
                perform_stmt_map[(para.name, s)] = stmt

        results = []
        visited: set[str] = set()
        in_stack: set[str] = set()
        cycle_reported: set[frozenset] = set()

        def dfs(node: str, path: list[str]) -> None:
            if node in in_stack:
                # Found a cycle
                cycle_start = path.index(node)
                cycle = path[cycle_start:]
                key = frozenset(cycle)
                if key not in cycle_reported:
                    cycle_reported.add(key)
                    # Find the statement that created the back-edge
                    caller = path[-1]
                    stmt = perform_stmt_map.get((caller, node))
                    line = stmt.line_num if stmt else 0
                    results.append(Diagnostic(
                        diag_type="Recursive PERFORM",
                        paragraph=caller,
                        line_num=line,
                        detail=(
                            f"PERFORM {node}: recursive cycle detected — "
                            f"{' → '.join(cycle + [node])}"
                        ),
                    ))
                return
            if node in visited:
                return
            visited.add(node)
            in_stack.add(node)
            path.append(node)
            for neighbour in graph.get(node, set()):
                if neighbour in graph:
                    dfs(neighbour, path)
            path.pop()
            in_stack.discard(node)

        for para in source.paragraphs:
            if para.name not in visited:
                dfs(para.name, [])

        return results

    # ------------------------------------------------------------------
    # 8. Unentered procedures
    # ------------------------------------------------------------------

    def check_unentered_procedures(self, source: CobolSource) -> list[Diagnostic]:
        """
        Paragraphs that are never referenced (via PERFORM, GO TO, or THRU range)
        AND that cannot be reached by sequential fall-through from the previous
        paragraph (i.e., the previous paragraph has a definitive exit statement).

        In COBOL, PERFORM handles its own return boundary — paragraphs do not need
        an explicit exit to be "returned from". Sequential fall-through into the
        next paragraph is therefore always a valid entry path unless blocked by a
        definitive exit (GOBACK, STOP RUN, EXIT PROGRAM, or GO TO) at the end of
        the prior paragraph.
        """
        if not source.paragraphs:
            return []

        referenced: set[str] = set()
        definitive_exits = {"GOBACK", "STOP RUN", "EXIT PROGRAM", "GO TO"}

        for para, stmt in source.all_statements:
            if stmt.verb == "PERFORM":
                start, thru = _perform_targets(stmt)
                if start:
                    start_u = start.upper()
                    referenced.add(start_u)
                    if thru:
                        thru_u = thru.upper()
                        referenced.add(thru_u)
                        for p in source.paragraphs_in_range(start_u, thru_u):
                            referenced.add(p.name)
            elif stmt.verb == "GO TO":
                target = _goto_target(stmt)
                if target:
                    referenced.add(target.upper())

        # First paragraph is always entered
        referenced.add(source.paragraphs[0].name)

        def _has_definitive_exit(para: Paragraph) -> bool:
            """Return True if para ends with a statement that prevents fall-through."""
            for stmt in reversed(para.statements):
                v = stmt.verb.upper()
                if v in ("CONTINUE", "EXIT", "END-IF", "END-PERFORM",
                         "END-READ", "END-EVALUATE", "END-COMPUTE", "END-CALL",
                         "END-STRING"):
                    continue  # Skip structural keywords, look further back
                return v in definitive_exits
            return False  # No meaningful statements → can fall through

        results = []
        for i, para in enumerate(source.paragraphs):
            if para.name in referenced:
                continue
            # Check if accessible via fall-through from previous paragraph
            if i > 0:
                prev = source.paragraphs[i - 1]
                if not _has_definitive_exit(prev):
                    continue  # Reachable via fall-through — not flagged
            results.append(Diagnostic(
                diag_type="Unentered procedure",
                paragraph=para.name,
                line_num=para.line_num,
                detail=(
                    f"Paragraph {para.name} is never PERFORMed, targeted by GO TO, "
                    "or within a PERFORM THRU range, and the preceding paragraph "
                    "ends with a definitive exit; may be dead code"
                ),
            ))
        return results

    # ------------------------------------------------------------------
    # 9. Unresolved PERFORM ranges
    # ------------------------------------------------------------------

    def check_unresolved_perform(self, source: CobolSource) -> list[Diagnostic]:
        """PERFORM references a paragraph name that doesn't exist in this program."""
        results = []
        para_names = set(source.para_map.keys())

        for para, stmt in source.all_statements:
            if stmt.verb != "PERFORM":
                continue
            start, thru = _perform_targets(stmt)
            if start and start.upper() not in para_names:
                results.append(Diagnostic(
                    diag_type="Unresolved PERFORM range",
                    paragraph=para.name,
                    line_num=stmt.line_num,
                    detail=(
                        f"PERFORM {start}: target paragraph not found in this program"
                    ),
                ))
            if thru and thru.upper() not in para_names:
                results.append(Diagnostic(
                    diag_type="Unresolved PERFORM...THRU range",
                    paragraph=para.name,
                    line_num=stmt.line_num,
                    detail=(
                        f"PERFORM ... THRU {thru}: THRU target paragraph not found "
                        "in this program"
                    ),
                ))
        return results

    # ------------------------------------------------------------------
    # 10. Non-standard FALL THRUs
    # ------------------------------------------------------------------

    def check_fall_thru(self, source: CobolSource) -> list[Diagnostic]:
        """
        Detect paragraphs that are targeted by GO TO statements (rather than
        PERFORM) and then fall through into the next paragraph rather than
        returning or exiting. This is the true "non-standard fall-through" —
        when a GO TO lands in a paragraph that has no explicit exit, causing
        uncontrolled sequential flow.

        Note: Paragraphs called via PERFORM do NOT exhibit fall-through;
        the PERFORM mechanism automatically returns control at the next
        paragraph boundary. Only paragraphs reached via GO TO (or the first
        paragraph) can exhibit true fall-through.
        """
        transfer_verbs = {"GOBACK", "STOP RUN", "EXIT PROGRAM", "GO TO"}
        results = []

        # Collect all paragraphs that are targets of GO TO (not just PERFORM)
        goto_targets: set[str] = set()
        for para, stmt in source.all_statements:
            if stmt.verb == "GO TO":
                target = _goto_target(stmt)
                if target:
                    goto_targets.add(target.upper())

        # The first paragraph is also executed directly (not via PERFORM)
        if source.paragraphs:
            goto_targets.add(source.paragraphs[0].name)

        for i, para in enumerate(source.paragraphs[:-1]):
            if para.name not in goto_targets:
                continue  # Reached only via PERFORM — no fall-through issue
            if para.is_exit_para or para.is_section:
                continue

            next_para = source.paragraphs[i + 1]

            # Find the last meaningful statement
            last_stmt = None
            for s in reversed(para.statements):
                if s.verb.upper() in (
                    "CONTINUE", "EXIT", "END-IF", "END-PERFORM",
                    "END-READ", "END-EVALUATE", "END-COMPUTE",
                ):
                    continue
                last_stmt = s
                break

            if last_stmt is None:
                results.append(Diagnostic(
                    diag_type="Non-standard FALL THRU",
                    paragraph=para.name,
                    line_num=para.line_num,
                    detail=(
                        f"Paragraph {para.name} (reached via GO TO) has no statements "
                        f"and falls through into {next_para.name}"
                    ),
                ))
                continue

            last_verb = last_stmt.verb.upper()
            if last_verb not in transfer_verbs:
                if next_para.is_exit_para:
                    continue  # Falls into EXIT stub — acceptable convention
                results.append(Diagnostic(
                    diag_type="Non-standard FALL THRU",
                    paragraph=para.name,
                    line_num=last_stmt.line_num,
                    detail=(
                        f"Paragraph {para.name} (reached via GO TO) ends with "
                        f"{last_verb} and falls through into {next_para.name}; "
                        "add explicit GO TO EXIT or GOBACK"
                    ),
                ))
        return results

    # ------------------------------------------------------------------
    # 11. Null fall-thru from SECTION header
    # ------------------------------------------------------------------

    def check_section_null_fallthru(self, source: CobolSource) -> list[Diagnostic]:
        """
        A SECTION header followed immediately by another SECTION or paragraph
        with no intervening statements.
        """
        results = []
        for i, para in enumerate(source.paragraphs[:-1]):
            if not para.is_section:
                continue
            if not para.statements:
                next_para = source.paragraphs[i + 1]
                results.append(Diagnostic(
                    diag_type="Null fall-thru from SECTION header",
                    paragraph=para.name,
                    line_num=para.line_num,
                    detail=(
                        f"SECTION {para.name} has no statements and falls through "
                        f"into {next_para.name}"
                    ),
                ))
        return results

    # ------------------------------------------------------------------
    # Informational: Non-returning CALLs
    # ------------------------------------------------------------------

    def check_nonreturning_calls(self, source: CobolSource) -> list[Diagnostic]:
        """
        Flag CALL statements to known abend/non-returning routines.
        Informational — not counted in severity score.
        """
        results = []
        for para, stmt in source.all_statements:
            if stmt.verb != "CALL":
                continue
            target = _call_target(stmt)
            if target and target.upper() in NON_RETURNING_CALLS:
                results.append(Diagnostic(
                    diag_type="Non-returning CALL",
                    paragraph=para.name,
                    line_num=stmt.line_num,
                    detail=(
                        f"CALL '{target}': known non-returning abend routine; "
                        "any code after this CALL is unreachable"
                    ),
                ))
        return results
