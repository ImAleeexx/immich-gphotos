from immich_gphotos.gphotos.client import classify_gpmc_error
from immich_gphotos.models import ErrorClass


def test_auth_failures_are_classified_as_auth_invalid():
    assert classify_gpmc_error(Exception("401 Client Error: Unauthorized")) is ErrorClass.AUTH_INVALID
    assert classify_gpmc_error(Exception("Failed to get auth token")) is ErrorClass.AUTH_INVALID


def test_rate_limiting_is_its_own_class():
    assert classify_gpmc_error(Exception("429 Too Many Requests")) is ErrorClass.RATE_LIMITED


def test_quota_exhaustion_is_its_own_class():
    assert classify_gpmc_error(Exception("storage quota exceeded")) is ErrorClass.QUOTA_EXHAUSTED


def test_connection_problems_are_transient():
    assert classify_gpmc_error(ConnectionError("connection reset")) is ErrorClass.TRANSIENT
    assert classify_gpmc_error(TimeoutError("timed out")) is ErrorClass.TRANSIENT


def test_rejected_uploads_are_unsupported_media():
    from gpmc.exceptions import UploadRejectedError

    assert classify_gpmc_error(UploadRejectedError("rejected")) is ErrorClass.UNSUPPORTED_MEDIA


def test_anything_else_is_unknown():
    assert classify_gpmc_error(Exception("weird")) is ErrorClass.UNKNOWN
