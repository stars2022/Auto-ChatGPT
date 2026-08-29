import unittest
from unittest.mock import patch

import app


class AppLogicTests(unittest.TestCase):
    def test_schedule_due_for_quota_recovery(self):
        schedule = {"enabled": True, "kind": "quota_recovered", "thread_id": "thread-1"}
        self.assertTrue(app.schedule_due(schedule, app.now_ms(), {"thread-1"}))
        self.assertTrue(app.schedule_due(schedule, app.now_ms(), {"__official__"}))
        self.assertTrue(app.schedule_due(schedule, app.now_ms(), {"__usage_probe__"}))
        self.assertFalse(app.schedule_due(schedule, app.now_ms(), set()))

    def test_safe_response_shape_drops_credentials_and_ids(self):
        value = app._safe_response_shape({"remaining": 3, "account_id": "secret", "access_token": "secret", "quota": {"unit": "USD"}})
        self.assertEqual(value, {"remaining": 3, "quota": {"unit": "USD"}})

    def test_effective_config_follows_local_custom_provider(self):
        with patch.object(app, "codex_provider_base_url", return_value="https://example.test"), patch.object(app, "local_auth_token", return_value=("sk-test", "api_key")):
            config = app.effective_usage_config({})
        self.assertEqual(config["base_url"], "https://example.test")
        self.assertTrue(config["enabled"])
        self.assertTrue(config["auto_from_codex"])

    def test_api_key_is_not_treated_as_official_oauth(self):
        with patch.object(app, "local_oauth_credentials", return_value=(None, None, None)), patch.object(app, "local_auth_info", return_value={"kind": "api_key"}):
            result = app.official_usage_probe()
        self.assertEqual(result["status"], "oauth_not_configured")

    def test_official_oauth_projection_maps_windows_without_leaking_ids(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _limit):
                return b'{"account_id":"private","rate_limit":{"primary_window":{"used_percent":42,"limit_window_seconds":18000,"reset_at":1893456000},"secondary_window":{"used_percent":91,"limit_window_seconds":604800,"reset_at":1893456000}}}'

        oauth = {"auth_mode": "chatgpt", "tokens": {"access_token": "oauth-test", "account_id": "acct-test"}}
        with patch.object(app, "_auth_sources", return_value=[oauth]), patch.object(app.urllib.request, "urlopen", return_value=Response()):
            result = app.official_usage_probe()
        self.assertEqual(result["status"], "ok")
        self.assertEqual([w["name"] for w in result["windows"]], ["5_hour", "7_day"])
        self.assertNotIn("account_id", result["data"])


if __name__ == "__main__":
    unittest.main()
