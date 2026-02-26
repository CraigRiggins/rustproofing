"""
report.py — Markdown report generator for Rust Proofing structural diagnostics.

Produces a report matching the format of the hand-written analysis in
carddemo-rustproofing-analysis.md.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence

from diagnostics import ProgramResult, Diagnostic, SEVERITY_CLEAN, SEVERITY_LOW


# ---------------------------------------------------------------------------
# Severity emoji / label helpers
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {SEVERITY_CLEAN: 0, SEVERITY_LOW: 1, "MEDIUM": 2, "HIGH": 3}

MODERNIZATION_RISK: dict[str, str] = {
    "HIGH": "Cannot be auto-structured (ALTER / GO TO loops present)",
    "MEDIUM": "Requires GO TO elimination before structuring",
    "LOW": "Minor cleanup needed",
    SEVERITY_CLEAN: "Ready for direct modernization",
}


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

class ReportGenerator:

    def generate(self, results: Sequence[ProgramResult]) -> str:
        now = datetime.now()
        lines: list[str] = []

        lines += self._header(now)
        lines += self._summary_table(results)
        lines += self._detailed_findings(results)
        lines += self._informational_section(results)
        lines += self._modernization_risk(results)
        lines += self._recommended_actions(results)
        lines += self._footer(now)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _header(self, now: datetime) -> list[str]:
        return [
            "# Rust Proofing — Structural Diagnostics Report",
            "",
            f"**Generated:** {now.strftime('%Y-%m-%d %H:%M:%S')}  ",
            "**Methodology:** IBM Transition Services — Legacy Transformation "
            "Rust Proofing (J. DelMonaco, 1999)  ",
            "**Section applied:** Structural Diagnostics",
            "",
            "> The term *structural diagnostic* refers to statements, or combinations "
            "of statements, allowed in COBOL which result in source code components "
            "that are difficult to maintain or understand regardless of the business "
            "function implemented.",
            "",
            "---",
            "",
        ]

    # ------------------------------------------------------------------
    # Summary table
    # ------------------------------------------------------------------

    def _summary_table(self, results: Sequence[ProgramResult]) -> list[str]:
        lines = [
            "## Summary",
            "",
            "| Program | Lines | Severity | Diagnostics | Issues |",
            "|---|---|---|---|---|",
        ]
        for r in results:
            name = Path(r.filepath).name
            sev = self._severity_label(r.severity)
            lines.append(
                f"| {name} | {r.total_lines:,} | {sev} | {len(r.diagnostics)} "
                f"| {r.issue_summary} |"
            )
        lines.append("")

        # Stats line
        total = len(results)
        by_sev: dict[str, int] = {}
        for r in results:
            by_sev[r.severity] = by_sev.get(r.severity, 0) + 1

        parts = []
        for sev in (SEVERITY_CLEAN, SEVERITY_LOW, "MEDIUM", "HIGH"):
            count = by_sev.get(sev, 0)
            if count:
                pct = count / total * 100
                parts.append(f"**{count} {sev}** ({pct:.0f}%)")
        lines += [" · ".join(parts), "", "---", ""]
        return lines

    # ------------------------------------------------------------------
    # Detailed findings
    # ------------------------------------------------------------------

    def _detailed_findings(self, results: Sequence[ProgramResult]) -> list[str]:
        flagged = [r for r in results if r.diagnostics]
        if not flagged:
            return ["## Detailed Findings", "", "_No structural diagnostics found._", "", "---", ""]

        lines = ["## Detailed Findings", ""]

        # Sort by severity descending
        flagged_sorted = sorted(flagged, key=lambda r: -_SEVERITY_ORDER.get(r.severity, 0))

        for r in flagged_sorted:
            name = Path(r.filepath).name
            sev_label = self._severity_label(r.severity)
            lines += [
                f"### {name} — {sev_label} ({len(r.diagnostics)} diagnostic{'s' if len(r.diagnostics) != 1 else ''})",
                "",
            ]

            # Group diagnostics by type
            by_type: dict[str, list[Diagnostic]] = {}
            for d in r.diagnostics:
                by_type.setdefault(d.diag_type, []).append(d)

            lines += [
                "| # | Diagnostic Type | Paragraph | Line | Description |",
                "|---|---|---|---|---|",
            ]
            idx = 1
            for dtype, diags in by_type.items():
                for d in diags:
                    lines.append(
                        f"| {idx} | **{dtype}** | `{d.paragraph}` "
                        f"| {d.line_num} | {d.detail} |"
                    )
                    idx += 1
            lines.append("")

        lines += ["---", ""]
        return lines

    # ------------------------------------------------------------------
    # Informational (non-returning CALLs)
    # ------------------------------------------------------------------

    def _informational_section(self, results: Sequence[ProgramResult]) -> list[str]:
        programs_with_info = [r for r in results if r.informational]
        if not programs_with_info:
            return []

        lines = [
            "## Informational — Non-returning CALLs",
            "",
            "The following programs call known abend routines. "
            "These are expected and not counted in severity scores, but any code "
            "placed after these calls would be unreachable.",
            "",
            "| Program | Paragraph | Line | Routine |",
            "|---|---|---|---|",
        ]
        for r in programs_with_info:
            name = Path(r.filepath).name
            for d in r.informational:
                routine = d.detail.split("'")[1] if "'" in d.detail else "?"
                lines.append(f"| {name} | `{d.paragraph}` | {d.line_num} | `{routine}` |")

        lines += ["", "---", ""]
        return lines

    # ------------------------------------------------------------------
    # Modernization risk
    # ------------------------------------------------------------------

    def _modernization_risk(self, results: Sequence[ProgramResult]) -> list[str]:
        by_risk: dict[str, list[str]] = {k: [] for k in MODERNIZATION_RISK}
        for r in results:
            name = Path(r.filepath).name
            by_risk[r.severity].append(name)

        lines = ["## Modernization Risk Assessment", "", "| Risk | Programs |", "|---|---|"]
        for sev in ("HIGH", "MEDIUM", SEVERITY_LOW, SEVERITY_CLEAN):
            programs = by_risk.get(sev, [])
            if programs:
                risk_label = MODERNIZATION_RISK[sev]
                prog_list = ", ".join(programs)
                lines.append(f"| {risk_label} | {prog_list} |")
        lines += ["", "---", ""]
        return lines

    # ------------------------------------------------------------------
    # Recommended actions
    # ------------------------------------------------------------------

    def _recommended_actions(self, results: Sequence[ProgramResult]) -> list[str]:
        high = [r for r in results if r.severity == "HIGH"]
        medium = [r for r in results if r.severity == "MEDIUM"]
        low = [r for r in results if r.severity == SEVERITY_LOW]

        if not (high or medium or low):
            return [
                "## Recommended Action Order",
                "",
                "_All programs are structurally clean. No remediation required._",
                "",
            ]

        lines = ["## Recommended Action Order", ""]
        step = 1

        for r in sorted(high, key=lambda x: len(x.diagnostics), reverse=True):
            name = Path(r.filepath).name
            alters = [d for d in r.diagnostics if d.diag_type == "ALTER statement"]
            gotos = [d for d in r.diagnostics if "GO TO" in d.diag_type]
            action_parts = []
            if alters:
                action_parts.append(
                    f"eliminate all {len(alters)} ALTER statement(s) first — "
                    "manually reconstruct dynamic dispatch using EVALUATE"
                )
            if gotos:
                action_parts.append(
                    f"replace {len(gotos)} non-standard GO TO(s) with structured PERFORM calls"
                )
            action = "; then ".join(action_parts) if action_parts else "remediate all diagnostics"
            lines.append(f"{step}. **{name}** (HIGH) — {action}.")
            step += 1

        for r in sorted(medium, key=lambda x: len(x.diagnostics), reverse=True):
            name = Path(r.filepath).name
            gotos = [d for d in r.diagnostics if "GO TO" in d.diag_type]
            prvs = [d for d in r.diagnostics if "PERFORM range violation" in d.diag_type]
            action_parts = []
            if gotos:
                action_parts.append(f"replace {len(gotos)} non-standard GO TO(s) with structured PERFORM calls")
            if prvs:
                action_parts.append(f"fix {len(prvs)} PERFORM THRU range exit(s)")
            action = "; ".join(action_parts) if action_parts else "remediate all diagnostics"
            lines.append(f"{step}. **{name}** (MEDIUM) — {action}.")
            step += 1

        for r in sorted(low, key=lambda x: len(x.diagnostics), reverse=True):
            name = Path(r.filepath).name
            actions = [d.diag_type for d in r.diagnostics]
            action = "; ".join(set(actions))
            lines.append(f"{step}. **{name}** (LOW) — address: {action}.")
            step += 1

        lines.append("")
        return lines

    # ------------------------------------------------------------------
    # Footer
    # ------------------------------------------------------------------

    def _footer(self, now: datetime) -> list[str]:
        clean_count = sum(1 for _ in [])
        return [
            "---",
            "",
            "_Report generated by the CardDemo Rust Proofing Analyzer. "
            "Based on IBM Transition Services — Legacy Transformation methodology "
            "(J. DelMonaco, 25-Jan-1999)._",
            "",
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _severity_label(self, severity: str) -> str:
        labels = {
            SEVERITY_CLEAN: "**CLEAN**",
            SEVERITY_LOW: "**LOW**",
            "MEDIUM": "**MEDIUM**",
            "HIGH": "**HIGH**",
        }
        return labels.get(severity, severity)
