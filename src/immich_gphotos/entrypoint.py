"""Container entrypoint: claim the data directory, then stop being root.

The image ships an unprivileged `app` user, but `chown`ing `/data` at build
time buys nothing: a bind mount replaces that directory wholesale with one
carrying the host's ownership, and Docker creates a missing bind-mount source
as root. The container is the only party in a position to fix that without
asking the user to run `chown` on the host first -- so it starts as root,
takes the directory, and drops to the app user before the app itself runs.

Set PUID/PGID to own the files as some other user (a NAS account, or your own
login so the database is readable outside the container).
"""

import contextlib
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

DEFAULT_UID = 1000
DEFAULT_GID = 1000


def _id(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(env[key])
    except (KeyError, TypeError, ValueError):
        return default


def resolve_ids(env: Mapping[str, str]) -> tuple[int, int]:
    return _id(env, "PUID", DEFAULT_UID), _id(env, "PGID", DEFAULT_GID)


def claim(
    path: Path,
    uid: int,
    gid: int,
    *,
    euid: int,
    chown: Callable[..., None] = os.chown,
    access: Callable[..., bool] = os.access,
) -> None:
    """Make `path` writable by `uid`, or explain why it isn't."""
    if euid != 0:
        # Someone pinned `user:` in their compose file, so the privilege to fix
        # this was given away before we got here. Say so in the only terms that
        # resolve it.
        if not (path.is_dir() and access(path, os.W_OK)):
            raise SystemExit(
                f"{path} is not writable by uid {euid}, and this container was started "
                f"without the privileges to fix that itself.\n"
                f"Either drop the `user:` line from your compose file and let the container "
                f"take ownership on boot, or run `sudo chown -R {euid} <the host directory "
                f"mounted at {path}>` yourself."
            )
        return

    path.mkdir(parents=True, exist_ok=True)
    targets = [path, *path.rglob("*")]
    for target in targets:
        # NFS with root_squash refuses this even as root, and the mount may
        # still be writable regardless. Let the app find out for itself rather
        # than refusing to boot on a guess.
        with contextlib.suppress(OSError):
            chown(target, uid, gid)


def drop_privileges(uid: int, gid: int) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def exec_app() -> None:
    os.execv(sys.executable, [sys.executable, "-m", "immich_gphotos"])  # noqa: S606 - fixed argv


def main(
    env: Mapping[str, str] | None = None,
    *,
    euid: int | None = None,
    chown: Callable[..., None] = os.chown,
    access: Callable[..., bool] = os.access,
    drop: Callable[[int, int], None] = drop_privileges,
    exec_app: Callable[[], None] = exec_app,
) -> None:
    env = os.environ if env is None else env
    euid = os.geteuid() if euid is None else euid
    uid, gid = resolve_ids(env)

    data_dir = Path(env.get("IGP_DATA_DIR", "/data"))
    claim(data_dir, uid, gid, euid=euid, chown=chown, access=access)

    scratch = env.get("IGP_SCRATCH_DIR")
    if scratch and Path(scratch) != data_dir:
        claim(Path(scratch), uid, gid, euid=euid, chown=chown, access=access)

    if euid == 0:
        # HOME survives the exec, and a custom PUID does not own the app
        # user's home directory as the image built it.
        home = env.get("HOME")
        if home:
            claim(Path(home), uid, gid, euid=euid, chown=chown, access=access)
        drop(uid, gid)

    exec_app()


if __name__ == "__main__":
    main()
