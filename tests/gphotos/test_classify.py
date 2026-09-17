import requests.exceptions

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


def test_requests_connection_and_timeout_errors_are_transient():
    """requests.exceptions.ConnectionError/Timeout are siblings of the builtins, not
    subclasses, so the isinstance fast path must name them explicitly."""
    conn_error = requests.exceptions.ConnectionError("connection reset")
    assert classify_gpmc_error(conn_error) is ErrorClass.TRANSIENT
    assert classify_gpmc_error(requests.exceptions.Timeout("timed out")) is ErrorClass.TRANSIENT


def test_rejected_uploads_are_unsupported_media():
    from gpmc.exceptions import UploadRejectedError

    assert classify_gpmc_error(UploadRejectedError("rejected")) is ErrorClass.UNSUPPORTED_MEDIA


def test_anything_else_is_unknown():
    assert classify_gpmc_error(Exception("weird")) is ErrorClass.UNKNOWN


def test_quota_markers_take_priority_over_auth_markers():
    """Google surfaces storage/rate quota errors as HTTP 403 with a quota reason
    string. Misclassifying that as AUTH_INVALID sends someone to needlessly
    re-extract auth_data from an Android device."""
    assert classify_gpmc_error(Exception("403 Forbidden: quotaExceeded")) is ErrorClass.QUOTA_EXHAUSTED
    assert classify_gpmc_error(Exception("403 dailyLimitExceeded quota")) is ErrorClass.QUOTA_EXHAUSTED


def test_auth_and_transient_markers_together_still_classify_auth_invalid():
    assert classify_gpmc_error(Exception("401 error, connection reset")) is ErrorClass.AUTH_INVALID
