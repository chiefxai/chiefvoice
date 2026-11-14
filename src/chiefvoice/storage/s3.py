"""S3-compatible object-storage client factory."""

from urllib.parse import urlsplit

from chiefvoice.core.config import settings


def validate_s3_configuration() -> None:
    if not settings.object_storage_bucket:
        raise RuntimeError("OBJECT_STORAGE_BUCKET is required for S3 storage")
    if not settings.object_storage_region.strip():
        raise ValueError("OBJECT_STORAGE_REGION must not be empty")
    if settings.object_storage_addressing_style not in {"path", "virtual", "auto"}:
        raise ValueError("OBJECT_STORAGE_ADDRESSING_STYLE must be path, virtual, or auto")
    if bool(settings.object_storage_access_key_id) != bool(settings.object_storage_secret_access_key):
        raise ValueError(
            "OBJECT_STORAGE_ACCESS_KEY_ID and OBJECT_STORAGE_SECRET_ACCESS_KEY must be set together"
        )
    if settings.object_storage_session_token and not settings.object_storage_access_key_id:
        raise ValueError("OBJECT_STORAGE_SESSION_TOKEN requires explicit object-storage credentials")
    endpoint = settings.object_storage_endpoint_url
    if endpoint:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ({"http", "https"} if settings.app_env.lower() == "local" else {"https"}):
            raise ValueError("S3 endpoint must use HTTPS outside local development")
        if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "OBJECT_STORAGE_ENDPOINT_URL must not include credentials, query, or fragment"
            )


def create_s3_compatible_client():
    """Create a boto3 client configured for AWS S3 or an S3-compatible endpoint."""
    validate_s3_configuration()
    import boto3
    from botocore.config import Config

    options = {
        "service_name": "s3",
        "region_name": settings.object_storage_region,
        "config": Config(
            connect_timeout=3,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": settings.object_storage_addressing_style},
        ),
    }
    if settings.object_storage_endpoint_url:
        options["endpoint_url"] = settings.object_storage_endpoint_url
    if settings.object_storage_access_key_id:
        options["aws_access_key_id"] = settings.object_storage_access_key_id
        options["aws_secret_access_key"] = settings.object_storage_secret_access_key
    if settings.object_storage_session_token:
        options["aws_session_token"] = settings.object_storage_session_token
    return boto3.client(**options)


def object_key(*parts: str) -> str:
    """Join trusted application object-key components under the configured prefix."""
    normalized = (
        ([settings.object_storage_prefix] if settings.object_storage_prefix else [])
        + [part.strip("/") for part in parts]
    )
    if any(
        not part
        or "\\" in part
        or any(segment in {".", ".."} for segment in part.split("/"))
        for part in normalized
    ):
        raise ValueError("Object-storage key contains an invalid path component")
    return "/".join(normalized)
