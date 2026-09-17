import pytest

from immich_gphotos.checksum import ChecksumError, normalize_checksum

# SHA-1 of b"hello": aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d
HEX = "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d"
B64 = "qvTGHdzF6KLavt4PO0gs2a6pQ00="
RAW = bytes.fromhex(HEX)


def test_base64_passes_through():
    assert normalize_checksum(B64) == B64


def test_hex_is_converted():
    assert normalize_checksum(HEX) == B64


def test_bytes_are_converted():
    assert normalize_checksum(RAW) == B64


def test_node_buffer_json_shape_is_converted():
    assert normalize_checksum({"type": "Buffer", "data": list(RAW)}) == B64


def test_bare_int_list_is_converted():
    assert normalize_checksum(list(RAW)) == B64


def test_wrong_length_is_rejected():
    with pytest.raises(ChecksumError):
        normalize_checksum("abc123")


def test_unknown_shape_is_rejected():
    with pytest.raises(ChecksumError):
        normalize_checksum({"nope": 1})
