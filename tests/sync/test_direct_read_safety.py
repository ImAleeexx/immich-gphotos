"""Direct reads: library-root allowlist, scratch hygiene, source stability
and timestamp diagnostics (PR #5 follow-up).

The invariant under test throughout: Immich's library is only ever *read*,
only from configured roots, and a resolution never leaves an orphaned
scratch file behind or claims a capture date it did not verify.
"""

import base64
import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from immich_gphotos.accounts import build
from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Filters, RetryPolicy
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.immich.protocol import ImmichError
from immich_gphotos.models import Asset, AssetState, ErrorClass, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync import bytes as bytes_mod
from immich_gphotos.sync.bytes import TIMESTAMP_UNVERIFIED, ByteResolver
from immich_gphotos.sync.worker import Worker

TAKEN = "2022-07-05T12:00:00Z"
TAKEN_TS = datetime(2022, 7, 5, 12, 0, tzinfo=UTC).timestamp()

ASSET = Asset(
    immich_id="a1",
    checksum="sum-a",
    filename="a.jpg",
    type="IMAGE",
    size_bytes=3,
    immich_updated_at="2026-09-17T10:00:00Z",
    original_path=None,
    visibility="timeline",
    is_offline=False,
    is_trashed=False,
    taken_at=TAKEN,
)


def _b64sha1(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


def _library(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    original = lib / "a.jpg"
    original.write_bytes(b"ABC")
    os.utime(original, (TAKEN_TS, TAKEN_TS))
    return lib, original


def _resolver(tmp_path, lib, immich=None):
    return ByteResolver(
        immich or FakeImmichClient(contents={"a1": b"XYZ"}),
        scratch=tmp_path / "scratch",
        direct_roots=[lib],
    )


def _scratch_files(tmp_path):
    scratch = tmp_path / "scratch"
    return sorted(p.name for p in scratch.iterdir()) if scratch.exists() else []


# --- allowlist -------------------------------------------------------------


def test_a_path_inside_a_library_root_is_read_directly_with_zero_copies(tmp_path):
    lib, original = _library(tmp_path)
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolved = _resolver(tmp_path, lib, immich).resolve(replace(ASSET, original_path=str(original)))
    assert resolved.path == original.resolve()
    assert resolved.temporary is False
    assert resolved.notes == ()
    assert immich.downloads == []
    assert _scratch_files(tmp_path) == []


def test_a_path_outside_every_root_is_downloaded_instead(tmp_path):
    lib, _ = _library(tmp_path)
    secret = tmp_path / "data" / "immich-gphotos.db"
    secret.parent.mkdir()
    secret.write_bytes(b"auth_data=SECRET")
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolved = _resolver(tmp_path, lib, immich).resolve(replace(ASSET, original_path=str(secret)))
    assert resolved.temporary is True
    assert resolved.path.read_bytes() == b"XYZ"
    assert immich.downloads == ["a1"]


def test_dot_dot_traversal_out_of_the_root_is_refused(tmp_path):
    lib, _ = _library(tmp_path)
    (tmp_path / "config.json").write_bytes(b"{}")
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    sneaky = f"{lib}/../config.json"
    resolved = _resolver(tmp_path, lib, immich).resolve(replace(ASSET, original_path=sneaky))
    assert immich.downloads == ["a1"]
    assert resolved.path.read_bytes() == b"XYZ"


def test_a_symlink_escaping_the_root_is_refused(tmp_path):
    lib, _ = _library(tmp_path)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"NOT-MEDIA")
    link = lib / "escape.jpg"
    link.symlink_to(outside)
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolved = _resolver(tmp_path, lib, immich).resolve(replace(ASSET, original_path=str(link)))
    assert immich.downloads == ["a1"]
    assert resolved.path.read_bytes() == b"XYZ"


def test_a_symlink_staying_inside_the_root_reads_its_real_target(tmp_path):
    lib, original = _library(tmp_path)
    link = lib / "alias.jpg"
    link.symlink_to(original)
    immich = FakeImmichClient()
    resolved = _resolver(tmp_path, lib, immich).resolve(replace(ASSET, original_path=str(link)))
    assert resolved.path == original.resolve()
    assert immich.downloads == []


def test_relative_paths_and_directories_are_never_read_directly(tmp_path):
    lib, _ = _library(tmp_path)
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = _resolver(tmp_path, lib, immich)
    resolver.resolve(replace(ASSET, original_path="library/a.jpg"))
    resolver.resolve(replace(ASSET, original_path=str(lib)))
    assert immich.downloads == ["a1", "a1"]


def test_the_scratch_directory_is_never_a_direct_source_even_inside_a_root(tmp_path):
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch", direct_roots=[tmp_path])
    (tmp_path / "scratch").mkdir()
    planted = tmp_path / "scratch" / "planted.jpg"
    planted.write_bytes(b"PLANTED")
    resolved = resolver.resolve(replace(ASSET, original_path=str(planted)))
    assert immich.downloads == ["a1"]
    assert resolved.path.read_bytes() == b"XYZ"


def test_without_configured_roots_only_immichs_default_location_is_trusted(tmp_path):
    _, original = _library(tmp_path)
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    resolver.resolve(replace(ASSET, original_path=str(original)))
    assert immich.downloads == ["a1"]


def test_an_empty_root_list_disables_direct_reads(tmp_path):
    _, original = _library(tmp_path)
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    ByteResolver(immich, scratch=tmp_path / "s", direct_roots=[]).resolve(
        replace(ASSET, original_path=str(original))
    )
    assert immich.downloads == ["a1"]


# --- direct_read_roots (env -> validated roots) -----------------------------


def test_env_roots_default_to_immichs_upload_location(monkeypatch):
    monkeypatch.setattr(build, "_is_read_only_mount", lambda p: True)
    # Not present on this machine, so nothing survives validation -- the
    # point is only that the default is that one path and nothing broader.
    assert (bytes_mod.Path("/usr/src/app/upload"),) == bytes_mod.DEFAULT_DIRECT_READ_ROOTS
    roots = build.direct_read_roots({})
    assert all(str(r).startswith("/usr/src/app/upload") for r in roots)


def test_env_roots_accept_several_read_only_libraries(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "_is_read_only_mount", lambda p: True)
    a, b = tmp_path / "upload", tmp_path / "external"
    a.mkdir()
    b.mkdir()
    roots = build.direct_read_roots({"IGP_DIRECT_READ_ROOTS": f"{a},{b}{os.pathsep}/does/not/exist"})
    assert roots == (a.resolve(), b.resolve())


def test_env_roots_refuse_writable_mounts_unless_explicitly_allowed(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    env = {"IGP_DIRECT_READ_ROOTS": str(lib)}
    assert build.direct_read_roots(env) == ()  # tmp_path is a writable mount
    assert build.direct_read_roots({**env, "IGP_DIRECT_READ_ALLOW_WRITABLE": "true"}) == (lib.resolve(),)


def test_env_roots_never_include_filesystem_root_or_the_data_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "_is_read_only_mount", lambda p: True)
    data = tmp_path / "data" / "accounts" / "x"
    data.mkdir(parents=True)
    env = {"IGP_DIRECT_READ_ROOTS": f"/,{tmp_path},{tmp_path / 'data'},relative/dir"}
    assert build.direct_read_roots(env, data_dirs=(data,)) == ()


# --- scratch hygiene ---------------------------------------------------------


def test_a_copy_that_fails_part_way_leaves_no_scratch_file(tmp_path, monkeypatch):
    lib, original = _library(tmp_path)
    os.utime(original, (TAKEN_TS + 86400, TAKEN_TS + 86400))  # drifted: needs a copy

    def half_copy(fd, size, dst):
        with open(dst, "wb") as handle:
            handle.write(b"A")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bytes_mod, "_copy_from_fd", half_copy)
    resolved = _resolver(tmp_path, lib).resolve(replace(ASSET, original_path=str(original)))
    assert _scratch_files(tmp_path) == []
    # The backup still happens, from the original, flagged as undated.
    assert resolved.path == original.resolve()
    assert resolved.temporary is False
    assert TIMESTAMP_UNVERIFIED in resolved.notes
    assert original.read_bytes() == b"ABC"
    assert original.stat().st_mtime == TAKEN_TS + 86400


def test_a_failed_download_leaves_no_partial_scratch_file(tmp_path):
    class Exploding(FakeImmichClient):
        def download_original(self, asset_id, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"partial")
            raise ImmichError("connection reset")

    resolver = ByteResolver(Exploding(), scratch=tmp_path / "scratch", direct_roots=[])
    with pytest.raises(ImmichError):
        resolver.resolve(ASSET)
    assert _scratch_files(tmp_path) == []


def test_a_source_changing_during_the_copy_falls_back_to_the_api(tmp_path, monkeypatch):
    lib, original = _library(tmp_path)
    os.utime(original, (TAKEN_TS + 86400, TAKEN_TS + 86400))
    real_copy = bytes_mod._copy_from_fd

    def copy_then_mutate(fd, size, dst):
        real_copy(fd, size, dst)
        with open(original, "ab") as handle:
            handle.write(b"MORE")

    monkeypatch.setattr(bytes_mod, "_copy_from_fd", copy_then_mutate)
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolved = _resolver(tmp_path, lib, immich).resolve(replace(ASSET, original_path=str(original)))
    assert immich.downloads == ["a1"]
    assert resolved.path.read_bytes() == b"XYZ"
    assert _scratch_files(tmp_path) == [resolved.path.name]


def test_hashing_refuses_a_file_whose_identity_changed(tmp_path):
    _, original = _library(tmp_path)
    st = os.stat(original)
    good = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    assert bytes_mod._sha1_base64_of_stable_file(original, good)
    with pytest.raises(bytes_mod.SourceChangedError):
        bytes_mod._sha1_base64_of_stable_file(
            original, (st.st_dev, st.st_ino, st.st_size + 1, st.st_mtime_ns)
        )
    link = tmp_path / "link.jpg"
    link.symlink_to(original)
    with pytest.raises(OSError):  # O_NOFOLLOW: a swapped-in symlink is never followed
        bytes_mod._sha1_base64_of_stable_file(link, good)


def test_a_source_changing_while_being_hashed_falls_back_to_the_api(tmp_path, monkeypatch):
    lib, original = _library(tmp_path)

    def changed(path, expected, root=None):
        raise bytes_mod.SourceChangedError("changed while it was being read")

    monkeypatch.setattr(bytes_mod, "_sha1_base64_of_stable_file", changed)
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolved = _resolver(tmp_path, lib, immich).resolve(replace(ASSET, original_path=str(original)))
    assert immich.downloads == ["a1"]
    assert resolved.temporary is True
    assert resolved.path.read_bytes() == b"XYZ"


def test_a_file_that_disappears_after_the_allowlist_check_is_downloaded(tmp_path, monkeypatch):
    lib, original = _library(tmp_path)
    resolver = _resolver(tmp_path, lib)
    real = resolver._allowed_direct_path

    def check_then_vanish(path):
        found = real(path)
        original.unlink()
        return found

    monkeypatch.setattr(resolver, "_allowed_direct_path", check_then_vanish)
    resolved = resolver.resolve(replace(ASSET, original_path=str(original)))
    assert resolved.path.read_bytes() == b"XYZ"


# --- timestamps --------------------------------------------------------------


def test_mtime_within_filesystem_granularity_is_trusted(tmp_path):
    lib, original = _library(tmp_path)
    os.utime(original, (TAKEN_TS + 1.9, TAKEN_TS + 1.9))  # e.g. FAT/SMB 2s granularity
    resolved = _resolver(tmp_path, lib).resolve(replace(ASSET, original_path=str(original)))
    assert resolved.temporary is False
    assert resolved.notes == ()


def test_mtime_beyond_tolerance_is_copied_stamped_and_verified(tmp_path):
    lib, original = _library(tmp_path)
    os.utime(original, (TAKEN_TS + 3, TAKEN_TS + 3))
    resolved = _resolver(tmp_path, lib).resolve(replace(ASSET, original_path=str(original)))
    assert resolved.temporary is True
    assert resolved.path.stat().st_mtime == TAKEN_TS
    assert resolved.notes == ()
    assert original.stat().st_mtime == TAKEN_TS + 3  # Immich's file untouched


def test_no_known_capture_date_is_flagged_not_assumed(tmp_path):
    lib, original = _library(tmp_path)
    immich = FakeImmichClient(contents={"a1": b"XYZ"})  # lookup returns None
    resolver = _resolver(tmp_path, lib, immich)
    direct = resolver.resolve(replace(ASSET, original_path=str(original), taken_at=None))
    downloaded = resolver.resolve(replace(ASSET, taken_at=None))
    assert TIMESTAMP_UNVERIFIED in direct.notes
    assert TIMESTAMP_UNVERIFIED in downloaded.notes
    assert immich.taken_at_calls == ["a1", "a1"]


def test_a_legacy_row_with_null_taken_at_uses_the_looked_up_date(tmp_path):
    lib, original = _library(tmp_path)
    immich = FakeImmichClient(contents={"a1": b"XYZ"}, taken_at={"a1": TAKEN})
    resolved = _resolver(tmp_path, lib, immich).resolve(
        replace(ASSET, original_path=str(original), taken_at=None)
    )
    assert resolved.temporary is False
    assert resolved.notes == ()


def test_a_stamp_that_fails_is_flagged(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(bytes_mod.os, "utime", refuse)
    resolver = ByteResolver(
        FakeImmichClient(contents={"a1": b"XYZ"}), scratch=tmp_path / "s", direct_roots=[]
    )
    resolved = resolver.resolve(ASSET)
    assert TIMESTAMP_UNVERIFIED in resolved.notes
    assert resolved.path.read_bytes() == b"XYZ"


# --- worker integration ------------------------------------------------------


def _worker_rig(tmp_path, lib, immich=None):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    gphotos = FakeGooglePhotosClient()
    resolver = _resolver(tmp_path, lib, immich)
    worker = Worker(assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock)
    return worker, assets, gphotos


def test_timestamp_diagnostics_are_persisted_and_counted(tmp_path):
    lib, original = _library(tmp_path)
    worker, assets, _ = _worker_rig(tmp_path, lib)
    assets.upsert_pending(replace(ASSET, original_path=str(original), taken_at=None), Priority.WEBHOOK)
    result = worker.process(assets.claim_next()[0])
    assert result.state is AssetState.SYNCED
    assert assets.sync_note_counts() == {TIMESTAMP_UNVERIFIED: 1}


def test_a_library_file_rewritten_during_upload_creates_no_google_item(tmp_path):
    """The rewrite lands after the bytes were streamed but before the media
    item would be created: nothing is committed (no stray item), the row is
    retried, and the retry uploads the new content exactly once."""
    lib, original = _library(tmp_path)
    worker, assets, gphotos = _worker_rig(tmp_path, lib)
    rewritten = []

    def rewrite(_path):
        if not rewritten:
            rewritten.append(True)
            original.write_bytes(b"REWRITTEN-BY-SOMEONE")
            os.utime(original, (TAKEN_TS, TAKEN_TS))

    gphotos.before_commit = rewrite
    assets.upsert_pending(replace(ASSET, original_path=str(original)), Priority.WEBHOOK)
    result = worker.process(assets.claim_next()[0])
    assert result.state is AssetState.PENDING
    assert result.error_class is ErrorClass.ASSET_UNAVAILABLE
    assert assets.get("a1").media_key is None
    assert len(gphotos.streamed) == 1
    assert gphotos.uploads == []  # streamed, never committed: no Google item

    worker._clock.advance(timedelta(days=1))
    result = worker.process(assets.claim_next()[0])
    assert result.state is AssetState.SYNCED
    assert gphotos.uploads == [(_b64sha1(b"REWRITTEN-BY-SOMEONE"), "a.jpg")]
    assert assets.get("a1").upload_sha1 == _b64sha1(b"REWRITTEN-BY-SOMEONE")


def test_a_rewrite_after_the_item_was_created_keeps_that_item(tmp_path):
    """Once committed, the Google item provably holds the hashed bytes; a
    later rewrite is a new version, not a reason to upload a second copy."""
    lib, original = _library(tmp_path)
    worker, assets, gphotos = _worker_rig(tmp_path, lib)
    real_upload = gphotos.upload

    def upload_then_rewrite(path, **kwargs):
        key = real_upload(path, **kwargs)
        original.write_bytes(b"REWRITTEN-LATER")
        return key

    gphotos.upload = upload_then_rewrite
    assets.upsert_pending(replace(ASSET, original_path=str(original)), Priority.WEBHOOK)
    result = worker.process(assets.claim_next()[0])
    assert result.state is AssetState.SYNCED
    assert gphotos.uploads == [(_b64sha1(b"ABC"), "a.jpg")]
    assert assets.get("a1").upload_sha1 == _b64sha1(b"ABC")


def test_bytes_that_do_not_match_the_declared_hash_are_never_committed(tmp_path):
    from immich_gphotos.gphotos.protocol import SourceChangedDuringUpload

    lib, original = _library(tmp_path)
    resolver = _resolver(tmp_path, lib)
    resolved = resolver.resolve(replace(ASSET, original_path=str(original)))
    gphotos = FakeGooglePhotosClient()
    with pytest.raises(SourceChangedDuringUpload):
        gphotos.upload(
            resolved.path,
            checksum=_b64sha1(b"OTHER"),
            filename="a.jpg",
            source=resolver.upload_source(resolved),
        )
    assert gphotos.uploads == []


# --- parent-directory symlink swaps --------------------------------------------


def _nested_library(tmp_path):
    lib = tmp_path / "library"
    album = lib / "2022" / "07"
    album.mkdir(parents=True)
    original = album / "a.jpg"
    original.write_bytes(b"ABC")
    os.utime(original, (TAKEN_TS, TAKEN_TS))
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "a.jpg"
    secret.write_bytes(b"SECRET-OUTSIDE-THE-LIBRARY")
    os.utime(secret, (TAKEN_TS, TAKEN_TS))
    return lib, original, outside


def _swap_parent_for_symlink(lib, outside):
    month = lib / "2022" / "07"
    month.rename(lib / "2022" / "07.moved")
    month.symlink_to(outside, target_is_directory=True)


def test_open_beneath_refuses_a_symlinked_parent(tmp_path):
    lib, original, outside = _nested_library(tmp_path)
    fd = bytes_mod.open_beneath(lib, original)
    os.close(fd)
    _swap_parent_for_symlink(lib, outside)
    with pytest.raises(OSError):
        bytes_mod.open_beneath(lib, original)
    with pytest.raises(PermissionError):
        bytes_mod.open_beneath(lib, lib / "2022" / ".." / "2022" / "07.moved" / "a.jpg")
    with pytest.raises(ValueError):
        bytes_mod.open_beneath(lib, outside / "a.jpg")


def test_a_parent_swapped_after_the_allowlist_check_is_never_read(tmp_path, monkeypatch):
    lib, original, outside = _nested_library(tmp_path)
    resolver = _resolver(tmp_path, lib)
    real = resolver._allowed_direct_path

    def check_then_swap(path):
        found = real(path)
        _swap_parent_for_symlink(lib, outside)
        return found

    monkeypatch.setattr(resolver, "_allowed_direct_path", check_then_swap)
    resolved = resolver.resolve(replace(ASSET, original_path=str(original)))
    assert resolved.temporary is True  # fell back to the API download
    assert resolved.path.read_bytes() == b"XYZ"
    assert resolved.sha1_b64 == _b64sha1(b"XYZ")


def test_a_parent_swapped_between_hashing_and_upload_uploads_nothing(tmp_path):
    lib, original, outside = _nested_library(tmp_path)
    worker, assets, gphotos = _worker_rig(tmp_path, lib)
    real_resolve = worker._resolver.resolve

    def resolve_then_swap(asset, **kwargs):
        resolved = real_resolve(asset, **kwargs)
        assert resolved.temporary is False  # a genuine direct read
        _swap_parent_for_symlink(lib, outside)
        return resolved

    worker._resolver.resolve = resolve_then_swap
    assets.upsert_pending(replace(ASSET, original_path=str(original)), Priority.WEBHOOK)
    result = worker.process(assets.claim_next()[0])
    assert result.state is AssetState.PENDING
    assert result.error_class is ErrorClass.ASSET_UNAVAILABLE
    assert gphotos.streamed == [] and gphotos.uploads == []


def test_a_parent_swapped_mid_upload_creates_no_item(tmp_path):
    lib, original, outside = _nested_library(tmp_path)
    worker, assets, gphotos = _worker_rig(tmp_path, lib)
    gphotos.before_commit = lambda _path: _swap_parent_for_symlink(lib, outside)
    assets.upsert_pending(replace(ASSET, original_path=str(original)), Priority.WEBHOOK)
    result = worker.process(assets.claim_next()[0])
    assert result.state is AssetState.PENDING
    assert len(gphotos.streamed) == 1
    assert gphotos.uploads == []


def test_direct_reads_never_write_to_the_library(tmp_path):
    lib, original = _library(tmp_path)
    os.utime(original, (TAKEN_TS + 86400, TAKEN_TS + 86400))
    before = sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in lib.iterdir())
    worker, assets, _ = _worker_rig(tmp_path, lib)
    assets.upsert_pending(replace(ASSET, original_path=str(original)), Priority.WEBHOOK)
    assert worker.process(assets.claim_next()[0]).state is AssetState.SYNCED
    after = sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in lib.iterdir())
    assert before == after
    assert _scratch_files(tmp_path) == []


def test_an_item_committed_just_before_a_crash_is_reused_not_uploaded_again(tmp_path):
    """Commit landed, then the process died before the row was marked
    synced. Google keys items by content SHA-1, which is durable on its
    side, so the retry finds that item read-only and records it."""
    lib, original = _library(tmp_path)
    worker, assets, gphotos = _worker_rig(tmp_path, lib)
    gphotos.present[_b64sha1(b"ABC")] = "AF1QipFROM-CRASHED-RUN"
    assets.upsert_pending(replace(ASSET, original_path=str(original)), Priority.WEBHOOK)
    result = worker.process(assets.claim_next()[0])
    assert result.state is AssetState.SYNCED
    assert assets.get("a1").media_key == "AF1QipFROM-CRASHED-RUN"
    assert gphotos.uploads == [] and gphotos.streamed == []
