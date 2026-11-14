from chiefvoice.agents.post_call import lambda_handler


def test_lambda_secret_values_are_loaded_once_per_warm_runtime(monkeypatch):
    calls = []

    class _Secrets:
        def get_secret_value(self, *, SecretId):
            calls.append(SecretId)
            return {"SecretString": f"secret:{SecretId}"}

    monkeypatch.setattr("boto3.client", lambda name: _Secrets())
    lambda_handler._read_secret.cache_clear()
    try:
        assert lambda_handler._read_secret("arn:one") == "secret:arn:one"
        assert lambda_handler._read_secret("arn:one") == "secret:arn:one"
        assert calls == ["arn:one"]
    finally:
        lambda_handler._read_secret.cache_clear()
