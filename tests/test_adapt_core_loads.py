#!/usr/bin/env python3
"""The ADAPTS apply core must actually LOAD in the daemon — not degrade silently.

THE OUTAGE (found 2026-07-29, round-6 review; live for 4+ weeks before that).
iris.py loads scripts/adaptation.py with spec_from_file_location + exec_module,
without registering it in sys.modules first. adaptation.py combines
`from __future__ import annotations` with @dataclass, and dataclasses resolves
its string annotations through:

    sys.modules.get(cls.__module__).__dict__

Unregistered, that get() returns None and the whole module raises
`AttributeError: 'NoneType' object has no attribute '__dict__'`. iris.py's
best-effort `except` then swallowed it, set `_adaptation = None`, logged a single
WARNING and carried on — so `/adapt list|show|approve|reject|outcome` answered
"⚠️ Adaptation apply core failed to load — /adapt is disabled" to every message
Steve sent, for four consecutive rotated daemon logs (2026-07-05/12/19/26).

Nothing caught it because everything about it looked healthy: the daemon booted,
every suite was green, and `scripts/adaptation.py --apply` worked fine from the
CLI (there `__module__` is "__main__", which IS registered). The only signal was
one WARNING line that read like harness noise in test output.

This suite fails if the registration is removed, so the feature cannot go dark
again without something going red.

Run: python3 tests/test_adapt_core_loads.py   (exit 0 = pass)
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ADAPT = REPO / "scripts" / "adaptation.py"


def _load(register: bool):
    """Load adaptation.py the way iris.py does. `register` toggles the fix."""
    name = "_adaptation_registered" if register else "_adaptation_unregistered"
    spec = importlib.util.spec_from_file_location(name, ADAPT)
    mod = importlib.util.module_from_spec(spec)
    if register:
        sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        return mod, None
    except Exception as exc:  # noqa: BLE001 - the thing under test
        return None, exc
    finally:
        sys.modules.pop(name, None)


class TestAdaptCoreLoads(unittest.TestCase):

    def test_registering_in_sys_modules_lets_it_load(self):
        """THE regression. With the registration, the module loads and exposes
        the apply core /adapt needs."""
        mod, exc = _load(register=True)
        self.assertIsNone(exc, f"adaptation.py must load when registered: {exc!r}")
        self.assertTrue(hasattr(mod, "apply_proposal"),
                        "the loaded module must expose apply_proposal — /adapt approve "
                        "calls it")

    def test_without_registration_it_fails_the_documented_way(self):
        """Pins the CAUSE, so the comment in iris.py stays true and a future
        reader can see exactly what breaks. If Python ever stops requiring this,
        this test fails and the comment gets revisited — deliberately."""
        mod, exc = _load(register=False)
        self.assertIsNotNone(exc, "expected the unregistered load to fail")
        self.assertIsInstance(exc, AttributeError)
        self.assertIn("__dict__", str(exc))

    def test_iris_actually_has_the_core_loaded(self):
        """End-to-end through iris.py itself: `_adaptation` must not be None.

        This is the assertion that would have caught the outage — the module-level
        loader runs at import, so a broken load leaves _adaptation = None here
        exactly as it did in the live daemon."""
        try:
            import test_pipeline_subprocess_timeout as harness  # noqa: F401
        except Exception as exc:  # pragma: no cover
            # FAIL, never skipTest. This is the ONLY assertion that catches a
            # regression in iris.py — the other two load adaptation.py themselves
            # and pass either way. With a skip, renaming an unrelated sibling test
            # file made the guard for a CONFIRMED four-week production outage exit
            # 0 with the whole 44-suite gate green. precheck.sh already draws this
            # line (an absent INTERNAL module is a failure, third-party is a skip);
            # a suite must not undercut it. Round-6 lane-1 review, 2026-07-29.
            self.fail(f"iris loader harness unavailable, so this guard could not "
                      f"run — that is a FAILURE, not a skip: {exc}")
        iris = harness.iris
        self.assertIsNotNone(
            getattr(iris, "_adaptation", None),
            "iris._adaptation is None -> /adapt is DISABLED in the daemon; every "
            "/adapt command answers with the failed-to-load message")
        self.assertTrue(hasattr(iris._adaptation, "apply_proposal"))

    def test_iris_also_has_the_docx_helper_loaded(self):
        """The OTHER guarded loader, asserted before it bites.

        `_script_to_docx` has the identical shape — unregistered spec load inside a
        best-effort except — and script_to_docx.py already carries `from __future__
        import annotations`. It survives today only because it defines no
        @dataclass, which is luck rather than design: adding one silently disabled
        auto-docx with all 44 suites green when the round-6 reviewer tried it.
        Fixing only _adaptation would have treated the symptom."""
        import test_pipeline_subprocess_timeout as harness
        iris = harness.iris
        self.assertIsNotNone(
            getattr(iris, "_script_to_docx", None),
            "iris._script_to_docx is None -> the auto-docx hook is dark and nothing "
            "else would tell you")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    unittest.main(verbosity=1)
