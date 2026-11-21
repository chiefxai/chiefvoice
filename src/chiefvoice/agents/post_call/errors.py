class NoTranscriptDataError(RuntimeError):
    """Terminal post-call condition: neither transcript source yielded usable text."""

    def __init__(self, *, stt_status: str, reason: str):
        super().__init__("No usable transcript is available for post-call analysis")
        self.stt_status = stt_status
        self.reason = reason
