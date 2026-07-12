"""Run every test module in this package. Exit non-zero if any fail.

Run: python -m tests.run_all
"""

from __future__ import annotations

import sys

from tests import test_features, test_merge_advanced, test_merge_offline


def main() -> int:
    suites = [
        ("merge (offline)", test_merge_offline.main),
        ("merge (advanced)", test_merge_advanced.main),
        ("features", test_features.main),
    ]
    failed = 0
    for name, fn in suites:
        print(f"\n===== {name} =====")
        try:
            rc = fn()
            if rc:
                failed += 1
        except AssertionError as err:
            print(f"FAILED: {err}")
            failed += 1
    print(f"\n{'ALL SUITES PASSED ✔' if not failed else f'{failed} SUITE(S) FAILED ✖'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
