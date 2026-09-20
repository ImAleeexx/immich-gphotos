import json
import logging

from immich_gphotos.logging import Redactor, configure_logging


def test_registered_secrets_are_scrubbed():
    r = Redactor(["androidId=abc123&app=xyz", "immich-api-key"])
    out = r.scrub("failed with androidId=abc123&app=xyz using immich-api-key")
    assert "abc123" not in out
    assert "immich-api-key" not in out
    assert out.count("[redacted]") == 2


def test_auth_data_pattern_is_scrubbed_even_if_not_registered():
    r = Redactor([])
    assert "6789" not in r.scrub("androidId=123456789abc&app=com.google.android.apps.photos")


def test_empty_secrets_do_not_blank_the_message():
    r = Redactor(["", None])
    assert r.scrub("hello") == "hello"


def test_add_secret_is_scrubbed_from_then_on():
    """The wizard persists a new API key or auth_data well after Redactor is
    constructed at boot; add_secret is how that credential still gets
    scrubbed for the rest of the process's life."""
    r = Redactor(["boot-secret"])
    assert "boot-secret" not in r.scrub("token=boot-secret")
    assert "wizard-secret" in r.scrub("token=wizard-secret")

    r.add_secret("wizard-secret")

    assert "wizard-secret" not in r.scrub("token=wizard-secret")
    assert "boot-secret" not in r.scrub("token=boot-secret")  # unaffected


def test_add_secret_ignores_empty_values():
    r = Redactor([])
    r.add_secret(None)
    r.add_secret("")
    assert r.scrub("hello") == "hello"


def test_configure_logging_accepts_a_shared_redactor_instance(capsys):
    """accounts.build.build_account_services hands the same Redactor to
    configure_logging and to the stores, so a credential added later
    (Redactor.add_secret) reaches both without configure_logging building its
    own separate copy."""
    shared = Redactor(["hunter3"])
    configure_logging("INFO", redactor=shared)
    shared.add_secret("added-later")
    logging.getLogger("test").info("secrets: hunter3 added-later")
    line = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)
    assert "hunter3" not in record["message"]
    assert "added-later" not in record["message"]


def test_configure_logging_emits_json_and_redacts(capsys):
    configure_logging("INFO", secrets=["hunter2"])
    logging.getLogger("test").info("password is hunter2")
    line = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["level"] == "INFO"
    assert "hunter2" not in record["message"]


def test_configure_logging_redacts_the_logger_name(capsys):
    configure_logging("INFO", secrets=["hunter2"])
    logging.getLogger("hunter2").info("hello")
    line = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)
    assert "hunter2" not in record["logger"]


def test_two_threads_adding_secrets_at_once_lose_neither():
    """FINDING M3: `add_secret` is read-then-reassign, and one `Redactor` is
    shared by every account. Two wizard completions on different accounts
    are two request threads, and without a lock the second's reassignment is
    built from a list read before the first's -- so one account's credential
    stays in the shared log stream for the life of the process, with no
    error and nothing to notice.

    Two things make the interleaving reliable rather than lucky: the barrier
    lines both threads up on the read, and the redactor is pre-loaded with
    enough secrets that the `sorted()` between the read and the reassignment
    spans plenty of bytecode for the interpreter to switch threads inside
    (helped along by a short switch interval). Without the lock this loses a
    secret within a couple of rounds; with it, never.
    """
    import sys
    import threading

    switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for round_number in range(20):
            redactor = Redactor(f"filler-secret-{i:05d}" for i in range(4000))
            start = threading.Barrier(2)
            secrets = (f"immich-key-{round_number}", f"google-auth-{round_number}")

            def add(secret, redactor=redactor, start=start):
                start.wait(timeout=5)
                redactor.add_secret(secret)

            threads = [threading.Thread(target=add, args=(s,)) for s in secrets]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            scrubbed = redactor.scrub(" ".join(secrets))
            for secret in secrets:
                assert secret not in scrubbed, f"round {round_number} lost {secret}"
    finally:
        sys.setswitchinterval(switch_interval)
