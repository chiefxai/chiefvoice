"""Google Cloud Storage object client using Application Default Credentials.

The small S3-shaped surface is an internal compatibility adapter for the storage
services. All network calls use google-cloud-storage; no HMAC/S3 credentials are used.
"""

from __future__ import annotations



def _is_not_found(exc: Exception) -> bool:
    return exc.__class__.__name__ == "NotFound" or getattr(exc, "code", None) == 404


class GoogleCloudStorageObjectClient:
    """Expose the storage operations Chiefvoice needs over the native GCS SDK."""

    uri_scheme = "gs"

    def __init__(self, client=None):
        if client is None:
            from google.cloud import storage

            client = storage.Client()  # Uses ADC from the attached runtime service account.
        self._client = client

    def put_object(self, *, Bucket, Key, Body, ContentType=None, Metadata=None, **_kwargs):
        blob = self._client.bucket(Bucket).blob(Key)
        if Metadata:
            blob.metadata = Metadata
        if hasattr(Body, "read"):
            blob.upload_from_file(Body, rewind=True, content_type=ContentType)
        else:
            blob.upload_from_string(Body, content_type=ContentType)
        return {"ETag": blob.etag}

    def get_object(self, *, Bucket, Key):
        blob = self._client.bucket(Bucket).blob(Key)
        blob.reload()
        return {
            "Body": blob.open("rb"),
            "ContentLength": blob.size,
            "Metadata": blob.metadata,
        }

    def delete_object(self, *, Bucket, Key):
        try:
            self._client.bucket(Bucket).blob(Key).delete()
        except Exception as exc:
            if not _is_not_found(exc):
                raise
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def delete_objects(self, *, Bucket, Delete):
        deleted = []
        errors = []
        for item in Delete.get("Objects", []):
            key = item.get("Key")
            if not isinstance(key, str) or not key:
                errors.append({"Key": key, "Code": "InvalidKey"})
                continue
            try:
                self._client.bucket(Bucket).blob(key).delete()
                deleted.append({"Key": key})
            except Exception as exc:
                # S3 delete is idempotent for absent objects; preserve that behavior.
                if _is_not_found(exc):
                    deleted.append({"Key": key})
                else:
                    errors.append({"Key": key, "Code": type(exc).__name__})
        result = {"Deleted": deleted}
        if errors:
            result["Errors"] = errors
        return result

    def list_objects_v2(self, *, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None):
        page_size = max(1, min(int(MaxKeys), 1000))
        iterator = self._client.list_blobs(
            Bucket,
            prefix=Prefix or None,
            page_token=ContinuationToken,
            page_size=page_size,
        )
        page = next(iter(iterator.pages), [])
        contents = [
            {"Key": blob.name, "Size": blob.size, "LastModified": blob.updated}
            for blob in page
        ]
        next_token = iterator.next_page_token
        result = {"Contents": contents, "IsTruncated": bool(next_token)}
        if next_token:
            result["NextContinuationToken"] = next_token
        return result

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None, **_kwargs):
        content_type = (ExtraArgs or {}).get("ContentType")
        self._client.bucket(Bucket).blob(Key).upload_from_filename(
            str(Filename), content_type=content_type
        )

    def download_file(self, Bucket, Key, Filename):
        self._client.bucket(Bucket).blob(Key).download_to_filename(str(Filename))

    def close(self):
        close = getattr(self._client, "close", None)
        if close is not None:
            close()
