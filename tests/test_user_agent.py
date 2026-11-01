import json
import unittest
from unittest import mock

from migration_check import __version__, providers


class _Resp:
    def __init__(self):
        self.status = 200

    def read(self):
        return json.dumps({"ok": True}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class UserAgentTest(unittest.TestCase):
    def test_post_names_the_tool_not_urllib(self):
        seen = {}

        class Opener:
            def open(self, request, timeout=None):
                seen.update({k.lower(): v for k, v in request.header_items()})
                return _Resp()

        with mock.patch.object(providers.urllib.request, "build_opener", return_value=Opener()):
            try:
                providers.post("https://example.invalid/v1/chat/completions", {"x": 1}, {"Authorization": "Bearer t"}, 5)
            except Exception:
                pass
        self.assertEqual(seen.get("user-agent"), f"migration-check/{__version__}")
        self.assertNotIn("python-urllib", seen.get("user-agent", "").lower())


if __name__ == "__main__":
    unittest.main()
