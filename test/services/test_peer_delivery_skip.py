"""deliver_pending must never pane-inject a peer (bi-directional bridge).

A peer has no tmux pane, so its messages stay PENDING for the driver to pull. This
verifies the guard added at the top of ``InboxService.deliver_pending`` short-circuits
before the delivery machinery is touched. (python-testing-patterns: assert at the seam.)
"""

from cli_agent_orchestrator.services import inbox_service as mod


def test_deliver_pending_skips_peer(monkeypatch):
    svc = mod.InboxService()
    monkeypatch.setattr(mod, "is_peer", lambda tid: True)

    touched = {"pending": False}
    monkeypatch.setattr(
        mod,
        "get_pending_messages",
        lambda *a, **k: touched.__setitem__("pending", True) or [],
    )

    svc.deliver_pending("deadbeef")

    # Returned before touching the queue or any pane/provider machinery.
    assert touched["pending"] is False


def test_deliver_pending_non_peer_proceeds_to_queue(monkeypatch):
    svc = mod.InboxService()
    monkeypatch.setattr(mod, "is_peer", lambda tid: False)

    touched = {"pending": False}

    def _spy(*a, **k):
        touched["pending"] = True
        return []  # empty -> deliver_pending returns right after, no pane needed

    monkeypatch.setattr(mod, "get_pending_messages", _spy)

    svc.deliver_pending("aabbccdd")

    assert touched["pending"] is True  # a non-peer DOES consult the queue
