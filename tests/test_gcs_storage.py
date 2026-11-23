from __future__ import annotations

from pathlib import Path

from chiefvoice.storage.gcs import GoogleCloudStorageObjectClient


class _FakeBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.metadata = None
        self.etag = None
        self.size = None
        self.updated = None

    def upload_from_string(self, body, content_type=None):
        self.bucket.objects[self.name] = bytes(body) if not isinstance(body, str) else body.encode()
        self.etag = "fake-etag"
        self.size = len(self.bucket.objects[self.name])

    def upload_from_file(self, file_obj, rewind=False, content_type=None):
        if rewind:
            file_obj.seek(0)
        self.upload_from_string(file_obj.read(), content_type=content_type)

    def download_as_bytes(self):
        return self.bucket.objects[self.name]

    def reload(self):
        self.size = len(self.bucket.objects[self.name])

    def open(self, mode="rb"):
        from io import BytesIO
        return BytesIO(self.bucket.objects[self.name])

    def delete(self):
        self.bucket.objects.pop(self.name, None)

    def upload_from_filename(self, filename, content_type=None):
        self.upload_from_string(Path(filename).read_bytes(), content_type=content_type)

    def download_to_filename(self, filename):
        Path(filename).write_bytes(self.download_as_bytes())


class _FakePageIterator:
    def __init__(self, blobs, token, page_size):
        self._blobs = blobs
        self._token = token
        self._page_size = page_size
        self.next_page_token = None

    @property
    def pages(self):
        start = int(self._token or 0)
        page = self._blobs[start : start + self._page_size]
        next_start = start + len(page)
        self.next_page_token = str(next_start) if next_start < len(self._blobs) else None
        yield page


class _FakeBucket:
    def __init__(self):
        self.objects = {}

    def blob(self, name):
        return _FakeBlob(self, name)


class _FakeGCSClient:
    def __init__(self):
        self.buckets = {}

    def bucket(self, name):
        return self.buckets.setdefault(name, _FakeBucket())

    def list_blobs(self, bucket_name, prefix=None, page_token=None, page_size=None):
        bucket = self.bucket(bucket_name)
        names = sorted(name for name in bucket.objects if not prefix or name.startswith(prefix))
        blobs = [_FakeBlob(bucket, name) for name in names]
        return _FakePageIterator(blobs, page_token, page_size or 1000)


def test_gcs_client_round_trips_objects_and_paginates():
    client = GoogleCloudStorageObjectClient(_FakeGCSClient())
    for key in ("chiefvoice/a", "chiefvoice/b", "chiefvoice/c"):
        client.put_object(Bucket="bucket", Key=key, Body=b"payload", ContentType="application/octet-stream")

    first = client.list_objects_v2(Bucket="bucket", Prefix="chiefvoice/", MaxKeys=2)
    assert [item["Key"] for item in first["Contents"]] == ["chiefvoice/a", "chiefvoice/b"]
    assert first["IsTruncated"] is True

    second = client.list_objects_v2(
        Bucket="bucket", Prefix="chiefvoice/", MaxKeys=2,
        ContinuationToken=first["NextContinuationToken"],
    )
    assert [item["Key"] for item in second["Contents"]] == ["chiefvoice/c"]
    assert second["IsTruncated"] is False

    response = client.get_object(Bucket="bucket", Key="chiefvoice/a")
    assert response["Body"].read() == b"payload"
    response["Body"].close()

    result = client.delete_objects(
        Bucket="bucket", Delete={"Objects": [{"Key": "chiefvoice/a"}, {"Key": "chiefvoice/b"}]}
    )
    assert len(result["Deleted"]) == 2
    assert [item["Key"] for item in client.list_objects_v2(Bucket="bucket", Prefix="chiefvoice/")["Contents"]] == ["chiefvoice/c"]


def test_gcs_client_uploads_and_downloads_files(tmp_path):
    client = GoogleCloudStorageObjectClient(_FakeGCSClient())
    source = tmp_path / "source.bin"
    target = tmp_path / "target.bin"
    source.write_bytes(b"file-payload")

    client.upload_file(str(source), "bucket", "chiefvoice/file.bin", ExtraArgs={"ContentType": "application/octet-stream"})
    client.download_file("bucket", "chiefvoice/file.bin", str(target))

    assert target.read_bytes() == b"file-payload"
