"""Run every test suite: four pytest suites (one process each, because each folder has its own conftest) and the
Node tests for the board's JavaScript. Exit code is non-zero if any suite fails.

    python scripts/run_tests.py            # everything that can run here
    python scripts/run_tests.py --quick    # skip the Node suites
    python scripts/run_tests.py --verbose  # one line per test (CI)
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST_SUITES = ["tests", "board/tests", "skills/job-apply-fleet/tests", "prep_extract/tests"]
SUITE_TIMEOUT_S = 150
NODE_TESTS = ["board/tests_js/jobs.test.js", "board/tests_js/jobs_scoring.test.js"]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    failed = []
    for suite in PYTEST_SUITES:
        print(f"\n=== pytest {suite}", flush=True)
        # A test stuck for 60 s dumps every thread's stack; a suite past SUITE_TIMEOUT_S fails instead of hanging.
        cmd = [sys.executable, "-m", "pytest", suite, "-v" if "--verbose" in argv else "-q", "-p", "no:cacheprovider", "-o", "faulthandler_timeout=60"]
        try:
            if subprocess.run(cmd, cwd=ROOT, timeout=SUITE_TIMEOUT_S, stdin=subprocess.DEVNULL).returncode:
                failed.append(suite)
        except subprocess.TimeoutExpired:
            print(f"TIMEOUT: {suite} ran past {SUITE_TIMEOUT_S} s", flush=True)
            failed.append(suite)
    if "--quick" not in argv:
        node = shutil.which("node")
        print("\n=== node --test board/tests_js", flush=True)
        if node is None:
            print("node not on PATH: skipped (the scorer and page-logic tests need Node 18+)")
        elif subprocess.run([node, "--test", *NODE_TESTS], cwd=ROOT, timeout=SUITE_TIMEOUT_S, stdin=subprocess.DEVNULL).returncode:
            failed.append("board/tests_js")
    print("\nFAILED: " + ", ".join(failed) if failed else "\nall suites passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
