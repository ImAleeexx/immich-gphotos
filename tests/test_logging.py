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
