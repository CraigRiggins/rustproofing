#!/usr/bin/env python3
"""
analyze.py — IBM Rust Proofing Structural Diagnostics Analyzer for COBOL.

Usage:
    python analyze.py file1.cbl file2.cbl ...
    python analyze.py path/to/cobol/directory/
    python analyze.py path/to/dir/ -o /output/directory/

Writes a markdown report to the output directory (default: same directory
as this script). Prints the report path to stdout on completion.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve imports whether run from within the package dir or from elsewhere
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent.resolve()
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from cobol_parser import CobolParser
from diagnostics import DiagnosticsAnalyzer, ProgramResult
from report import ReportGenerator


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def collect_cbl_files(paths: list[str]) -> list[Path]:
    """Expand a list of file/directory paths to a sorted list of .cbl files."""
    found: list[Path] = []
    for p in paths:
        target = Path(p)
        if target.is_file():
            if target.suffix.lower() in (".cbl", ".cob", ".cobol"):
                found.append(target.resolve())
            else:
                print(f"WARNING: {target} is not a recognised COBOL file extension — skipping")
        elif target.is_dir():
            for ext in ("*.cbl", "*.CBL", "*.cob", "*.COB", "*.cobol"):
                found.extend(sorted(target.glob(ext)))
        else:
            print(f"WARNING: {target} not found — skipping")
    # Deduplicate preserving order
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in found:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def analyze_files(files: list[Path], verbose: bool = False) -> list[ProgramResult]:
    parser = CobolParser()
    analyzer = DiagnosticsAnalyzer()
    results: list[ProgramResult] = []

    for filepath in files:
        if verbose:
            print(f"  Parsing   {filepath.name} ...", end="", flush=True)
        try:
            source = parser.parse(filepath)
        except Exception as exc:
            print(f"\n  ERROR parsing {filepath.name}: {exc}")
            continue

        if verbose:
            print(f" {len(source.paragraphs)} paragraphs found", end="")

        result = analyzer.analyze(source)
        results.append(result)

        if verbose:
            sev = result.severity
            count = len(result.diagnostics)
            print(f"  → {sev} ({count} diagnostic{'s' if count != 1 else ''})")

    return results


def write_report(results: list[ProgramResult], output_dir: Path) -> Path:
    generator = ReportGenerator()
    markdown = generator.generate(results)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"rustproofing_report_{timestamp}.md"
    report_path.write_text(markdown, encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="analyze.py",
        description=(
            "IBM Rust Proofing Structural Diagnostics Analyzer for COBOL.\n"
            "Analyzes fixed-format COBOL source files and produces a markdown report."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze a single file
  python analyze.py path/to/CBSTM03A.cbl

  # Analyze all .cbl files in a directory
  python analyze.py path/to/cobol/dir/

  # Analyze specific batch programs, write report to /tmp/
  python analyze.py CBACT01C.cbl CBSTM03A.cbl CBSTM03B.cbl -o /tmp/

  # Analyze the CardDemo batch programs (from the repo root)
  python rustproofing/analyze.py aws-mainframe-modernization-carddemo/app/cbl/
""",
    )
    p.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="One or more .cbl files or directories containing .cbl files",
    )
    p.add_argument(
        "-o", "--output-dir",
        default=None,
        metavar="DIR",
        help="Directory to write the report to (default: same directory as analyze.py)",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print per-file progress to stderr",
    )
    p.add_argument(
        "--summary-only",
        action="store_true",
        help="Print a brief per-file summary to stdout (no file written)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Resolve output directory
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    else:
        output_dir = _HERE
    output_dir.mkdir(parents=True, exist_ok=True)

    # Collect files
    files = collect_cbl_files(args.paths)
    if not files:
        print("ERROR: No COBOL source files found.", file=sys.stderr)
        return 1

    print(f"Analyzing {len(files)} COBOL source file(s)...", file=sys.stderr)

    results = analyze_files(files, verbose=args.verbose)

    if not results:
        print("ERROR: No files were successfully parsed.", file=sys.stderr)
        return 1

    # Summary-only mode
    if args.summary_only:
        _print_summary(results)
        return 0

    # Write report
    report_path = write_report(results, output_dir)
    print(f"\nReport written to: {report_path}", file=sys.stderr)
    print(str(report_path))  # Machine-readable path to stdout

    # Also print a brief table to stderr for immediate feedback
    print("", file=sys.stderr)
    _print_summary(results, file=sys.stderr)

    return 0


def _print_summary(
    results: list[ProgramResult],
    file=None,
) -> None:
    if file is None:
        file = sys.stdout
    width_name = max((len(Path(r.filepath).name) for r in results), default=20)
    header = f"{'Program':<{width_name}}  {'Lines':>6}  {'Severity':<8}  {'Diags':>5}  Issues"
    print(header, file=file)
    print("-" * len(header), file=file)
    for r in results:
        name = Path(r.filepath).name
        issues = r.issue_summary if r.diagnostics else "—"
        if len(issues) > 50:
            issues = issues[:47] + "..."
        print(
            f"{name:<{width_name}}  {r.total_lines:>6,}  {r.severity:<8}  "
            f"{len(r.diagnostics):>5}  {issues}",
            file=file,
        )


if __name__ == "__main__":
    sys.exit(main())
