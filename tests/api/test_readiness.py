"""`evaluate_readiness` is pure and does no I/O, so it is tested directly
rather than through HTTP -- the route is a one-line adapter over it."""

from immich_gphotos.api.readiness import evaluate_readiness
from immich_gphotos.models import Priority
from immich_gphotos.storage_keys import (
    IMMICH_KEY_KEY,
    IMMICH_URL_KEY,
    WORKFLOW_ID_KEY,
)
from immich_gphotos.sync.backfill import BACKFILL_CURSOR


def _check(readiness, check_id):
    return next(c for c in readiness.checks if c.id == check_id)


def test_a_fresh_install_needs_setup(rig_services):
    readiness = evaluate_readiness(rig_services)
    assert readiness.overall == "needs_setup"
    assert _check(readiness, "immich").state == "pending"
    assert _check(readiness, "google").state == "pending"
    assert _check(readiness, "immich").action_href == "/wizard"


def test_immich_is_not_connected_while_the_client_is_the_fake(rig_services):
    """Credentials stored but the runtime still holding FakeImmichClient means
    the live swap did not happen -- reporting "connected" there would be a
    lie the operator cannot see through."""
    rig_services.settings_repo.set(IMMICH_URL_KEY, "http://immich:2283")
    rig_services.settings_repo.set(IMMICH_KEY_KEY, "k")
    assert _check(evaluate_readiness(rig_services), "immich").state == "pending"


def test_delivery_is_pending_until_a_workflow_is_registered(configured_services):
    check = _check(evaluate_readiness(configured_services), "delivery")
    assert check.state == "pending"
    assert "reconciler" in check.detail.lower()


def test_delivery_is_ok_once_a_workflow_is_registered(configured_services):
    configured_services.settings_repo.set(WORKFLOW_ID_KEY, "wf-1")
    assert _check(evaluate_readiness(configured_services), "delivery").state == "ok"


def test_a_fully_configured_quiet_system_is_ready(configured_services):
    configured_services.settings_repo.set(WORKFLOW_ID_KEY, "wf-1")
    readiness = evaluate_readiness(configured_services)
    assert readiness.overall == "ready"
    assert all(c.state == "ok" for c in readiness.checks)


def test_backfill_in_progress_reports_its_page(configured_services):
    configured_services.cursors.set(BACKFILL_CURSOR, "7")
    check = _check(evaluate_readiness(configured_services), "backfill")
    assert check.state == "pending"
    assert "7" in check.detail


def test_queued_assets_are_pending_not_an_alarm(configured_services, asset_factory):
    """Queued work is normal on the event-driven path. Flagging it as
    attention would keep the panel permanently red on a healthy system."""
    configured_services.assets.upsert_pending(asset_factory("a"), Priority.WEBHOOK)
    assert _check(evaluate_readiness(configured_services), "backfill").state == "pending"


def test_a_paused_transfer_is_attention_and_names_the_reason(configured_services):
    configured_services.runtime.paused_reason = "google credential rejected"
    readiness = evaluate_readiness(configured_services)
    assert readiness.overall == "attention"
    check = _check(readiness, "transfer")
    assert check.state == "attention"
    assert "google credential rejected" in check.detail
    assert check.action_href == "/diagnostics"


def test_failed_assets_are_attention_and_link_to_the_failures_page(
    configured_services, asset_factory
):
    from immich_gphotos.models import ErrorClass

    configured_services.assets.upsert_pending(asset_factory("a"), Priority.WEBHOOK)
    configured_services.assets.claim_next(limit=1)
    configured_services.assets.mark_failed("a", ErrorClass.UNSUPPORTED_MEDIA, "rejected")
    check = _check(evaluate_readiness(configured_services), "failures")
    assert check.state == "attention"
    assert check.action_href == "/failures"


def test_setup_outranks_attention_in_the_rollup(rig_services):
    """An unconfigured system with a paused runtime must say "needs setup",
    not "attention" -- the setup is the actionable thing."""
    rig_services.runtime.paused_reason = "nothing configured"
    assert evaluate_readiness(rig_services).overall == "needs_setup"


def test_a_probe_that_raises_is_reported_rather_than_propagating(configured_services):
    """Readiness is polled by the dashboard; it must never 500. Precedent:
    pages.diagnostics_page and wizard.wizard_status both swallow here."""

    class Exploding:
        @property
        def paused_reason(self):
            raise RuntimeError("runtime is mid-rebuild")

    configured_services.runtime = Exploding()
    check = _check(evaluate_readiness(configured_services), "transfer")
    assert check.state == "attention"
    assert "mid-rebuild" in check.detail


def test_every_check_reports_rather_than_propagates_when_its_store_raises(rig_services):
    """Every check -- not just transfer -- must survive a raising dependency.
    A locked or corrupted sqlite file can surface through settings_repo,
    cursors or assets alike; none of them may turn a poll of /api/readiness
    into a 500."""

    class Explodes:
        def get(self, *args, **kwargs):
            raise RuntimeError("store is locked")

        def counts_by_state(self):
            raise RuntimeError("store is locked")

    class ExplodingRuntime:
        @property
        def paused_reason(self):
            raise RuntimeError("store is locked")

    rig_services.settings_repo = Explodes()
    rig_services.cursors = Explodes()
    rig_services.assets = Explodes()
    rig_services.runtime = ExplodingRuntime()

    readiness = evaluate_readiness(rig_services)

    assert readiness.overall == "needs_setup"
    assert len(readiness.checks) == 6
    assert all(c.state == "attention" for c in readiness.checks)
    assert all("store is locked" in c.detail for c in readiness.checks)
