"""Select an object-storage SDK from runtime configuration."""

from chiefvoice.core.config import settings
from chiefvoice.storage.s3 import create_s3_compatible_client


def create_object_storage_client():
    """Create the configured object client; GCS uses ADC, S3 uses its credential chain."""
    provider = settings.object_storage_provider
    if provider == "gcs":
        if settings.object_storage_endpoint_url:
            raise ValueError("OBJECT_STORAGE_ENDPOINT_URL is not supported by native GCS storage")
        if any(
            (
                settings.object_storage_access_key_id,
                settings.object_storage_secret_access_key,
                settings.object_storage_session_token,
            )
        ):
            raise ValueError("GCS storage uses ADC; remove explicit S3 object-storage credentials")
        from chiefvoice.storage.gcs import GoogleCloudStorageObjectClient

        return GoogleCloudStorageObjectClient()
    if provider == "s3":
        return create_s3_compatible_client()
    raise ValueError("OBJECT_STORAGE_PROVIDER must be 'gcs' or 's3'")
