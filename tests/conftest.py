"""Suite-wide safety seams.

⚠️ ``bounded_call`` runs network calls in a CHILD process, and a child imports
the real modules fresh — so a test's monkeypatched Drive/Firestore fakes would
NOT reach it, and a test could quietly talk to the real services. Every test
therefore runs bounded calls in-process unless it explicitly turns the child
back on (``tests/test_bounded_call.py`` does, with functions that never touch
the network). ⚠️ This only makes the monkeypatches REACH the call; it does not
stub anything. A function no test replaced still runs for real in-process, so
each test module must still stub its own network seams (the purchase-audit
``tick`` fixture makes both report-only extras raise if reached).

``DISCORD_WEBHOOK`` is blanked for the same reason: nothing in the suite may
post to Discord, whatever the developer's shell has set.
"""

import pytest

from app.core import bounded_call


@pytest.fixture(autouse=True)
def _bounded_calls_in_process(monkeypatch):
    monkeypatch.setattr(bounded_call, "IN_CHILD", False)
    monkeypatch.setenv("DISCORD_WEBHOOK", "")
