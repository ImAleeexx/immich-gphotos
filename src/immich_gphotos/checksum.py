import base64
import binascii

SHA1_BYTES = 20


class ChecksumError(ValueError):
    """The value could not be read as a SHA-1 checksum."""


def _encode(raw: bytes) -> str:
    if len(raw) != SHA1_BYTES:
        raise ChecksumError(f"expected {SHA1_BYTES} bytes, got {len(raw)}")
    return base64.b64encode(raw).decode("ascii")


def normalize_checksum(value: object) -> str:
    """Accept every shape Immich might hand us and return base64 SHA-1.

    Immich's REST API returns base64. The workflow webhook serialises a Wasm
    Buffer, whose JSON form may be a base64 string or {"type":"Buffer","data":[...]}.
    """
    if isinstance(value, bytes | bytearray):
        return _encode(bytes(value))

    if isinstance(value, str):
        if len(value) == SHA1_BYTES * 2:
            try:
                return _encode(bytes.fromhex(value))
            except ValueError as exc:
                raise ChecksumError(f"not valid hex: {value!r}") from exc
        try:
            return _encode(base64.b64decode(value, validate=True))
        except ValueError as exc:
            raise ChecksumError(f"not a valid checksum string: {value!r}") from exc

    if isinstance(value, dict) and value.get("type") == "Buffer":
        data = value.get("data")
        if isinstance(data, list):
            return _encode(bytes(data))

    if isinstance(value, list) and all(isinstance(i, int) for i in value):
        return _encode(bytes(value))

    raise ChecksumError(f"unrecognised checksum shape: {type(value).__name__}")
