
from scheduled_focus_push import write_with_retry

class MockFailedDocument:
    def set(self, payload):
        raise RuntimeError("Simulated Firebase write failure")

mock_document = MockFailedDocument()

result = write_with_retry(
    mock_document,
    {
        "date": "2026-10-13",
        "hour": 10,
        "app": "TEST_ONLY",
        "duration_minutes": 1.0
    }
)

print("Write result:", result)
