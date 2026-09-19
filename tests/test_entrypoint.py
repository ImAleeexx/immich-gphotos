"""The container's own privilege handling.

The image runs the app as an unprivileged user, but a bind-mounted `./data`
arrives owned by whoever created it on the host -- root, when Docker created
it because the directory did not exist yet. Nothing inside the image can be
chowned to fix that: the mount hides the image's own `/data` entirely. So the
container starts as root, claims the directory, and only then drops to the
app user. These tests pin that order and the fallbacks around it.
"""

from pathlib import Path

import pytest

from immich_gphotos.entrypoint import (
    DEFAULT_GID,
    DEFAULT_UID,
    claim,
    main,
    resolve_ids,
)


def test_ids_default_to_the_app_user_baked_into_the_image():
    assert resolve_ids({}) == (DEFAULT_UID, DEFAULT_GID)


def test_puid_and_pgid_override_the_default():
    assert resolve_ids({"PUID": "1026", "PGID": "100"}) == (1026, 100)


def test_each_id_falls_back_independently():
    assert resolve_ids({"PUID": "1026"}) == (1026, DEFAULT_GID)
    assert resolve_ids({"PGID": "100"}) == (DEFAULT_UID, 100)


def test_unparseable_ids_fall_back_rather_than_crashing_the_container():
    """A typo'd PUID should not be the difference between a container that
    boots and one that crash-loops with a ValueError."""
    assert resolve_ids({"PUID": "", "PGID": "nope"}) == (DEFAULT_UID, DEFAULT_GID)


def test_running_as_root_claims_the_directory_and_everything_in_it(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "immich-gphotos.db").touch()
    (data / "scratch").mkdir()
    (data / "scratch" / "staged.jpg").touch()
    chowned = []

    claim(data, 1000, 1000, euid=0, chown=lambda p, u, g: chowned.append((Path(p), u, g)))

    assert (data, 1000, 1000) in chowned
    assert (data / "immich-gphotos.db", 1000, 1000) in chowned
    assert (data / "scratch", 1000, 1000) in chowned
    assert (data / "scratch" / "staged.jpg", 1000, 1000) in chowned


def test_running_as_root_creates_the_directory_when_it_is_missing(tmp_path):
    data = tmp_path / "data"

    claim(data, 1000, 1000, euid=0, chown=lambda *a: None)

    assert data.is_dir()


def test_a_directory_that_cannot_be_chowned_is_not_fatal(tmp_path):
    """NFS with root_squash refuses the chown even as root. The mount may
    still be perfectly writable, so let the app try rather than refusing to
    boot on its behalf."""
    data = tmp_path / "data"
    data.mkdir()

    def refuse(*_args):
        raise PermissionError(1, "Operation not permitted")

    claim(data, 1000, 1000, euid=0, chown=refuse)


def test_running_as_non_root_does_not_attempt_a_chown(tmp_path):
    """`user:` in the compose file means we never had the privilege to fix
    ownership, and a failed chown here would be noise, not a diagnosis."""
    data = tmp_path / "data"
    data.mkdir()
    chowned = []

    claim(data, 1000, 1000, euid=1000, chown=lambda *a: chowned.append(a), access=lambda *a: True)

    assert chowned == []


def test_non_root_on_an_unwritable_directory_explains_the_fix(tmp_path):
    data = tmp_path / "data"
    data.mkdir()

    with pytest.raises(SystemExit) as exc:
        claim(data, 1000, 1000, euid=1000, chown=lambda *a: None, access=lambda *a: False)

    message = str(exc.value)
    assert str(data) in message
    assert "1000" in message
    # The two ways out, named -- this is the last thing the user sees.
    assert "chown" in message
    assert "user:" in message


def test_boot_claims_then_drops_then_execs_in_that_order(tmp_path):
    calls = []

    main(
        {"IGP_DATA_DIR": str(tmp_path / "data"), "PUID": "1026", "PGID": "100"},
        euid=0,
        chown=lambda *a: None,
        drop=lambda uid, gid: calls.append(("drop", uid, gid)),
        exec_app=lambda: calls.append(("exec",)),
    )

    assert calls == [("drop", 1026, 100), ("exec",)]


def test_boot_as_non_root_execs_without_trying_to_drop(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    calls = []

    main(
        {"IGP_DATA_DIR": str(data)},
        euid=1000,
        chown=lambda *a: None,
        access=lambda *a: True,
        drop=lambda *a: calls.append("drop"),
        exec_app=lambda: calls.append("exec"),
    )

    assert calls == ["exec"]


def test_the_scratch_override_is_claimed_too(tmp_path):
    """IGP_SCRATCH_DIR can point outside the data dir, at its own mount."""
    data = tmp_path / "data"
    scratch = tmp_path / "elsewhere"
    scratch.mkdir()
    chowned = []

    main(
        {"IGP_DATA_DIR": str(data), "IGP_SCRATCH_DIR": str(scratch)},
        euid=0,
        chown=lambda p, u, g: chowned.append(Path(p)),
        drop=lambda *a: None,
        exec_app=lambda: None,
    )

    assert scratch in chowned


def test_the_app_users_home_is_claimed_too(tmp_path):
    """Dropping from root leaves HOME pointing at the app user's home, which
    a custom PUID does not own. Anything the upload library caches under ~
    would fail on a directory we could trivially have claimed."""
    home = tmp_path / "home" / "app"
    home.mkdir(parents=True)
    chowned = []

    main(
        {"IGP_DATA_DIR": str(tmp_path / "data"), "HOME": str(home), "PUID": "1026"},
        euid=0,
        chown=lambda p, u, g: chowned.append((Path(p), u)),
        drop=lambda *a: None,
        exec_app=lambda: None,
    )

    assert (home, 1026) in chowned
