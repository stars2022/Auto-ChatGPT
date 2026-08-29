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

    def test_restore_goal_unarchives_before_queueing(self):
        class Completed:
            returncode = 0
            stdout = "restored"
            stderr = ""

        with patch.object(app, "get_thread_rows", return_value=[{"id": "thread-1", "archived": 1}]), \
             patch.object(app, "codex_command", return_value="/usr/local/bin/codex"), \
             patch.object(app.subprocess, "run", return_value=Completed()) as run, \
             patch.object(app, "enqueue", return_value=(True, "queued")) as enqueue:
            ok, detail = app.restore_goal("thread-1", "继续")
        self.assertTrue(ok)
        self.assertEqual(detail, "queued")
        run.assert_called_once_with(["/usr/local/bin/codex", "unarchive", "thread-1"], capture_output=True, text=True, timeout=20)
        enqueue.assert_called_once_with("thread-1", "继续")

    def test_https_context_requires_certificate_verification(self):
        self.assertEqual(app.HTTPS_CONTEXT.verify_mode, app.ssl.CERT_REQUIRED)

    def test_projects_group_threads_by_working_directory(self):
        rows = [
            {"id": "a", "cwd": "/tmp/alpha", "tokens_used": 100, "goal_status": "active", "updated_at": "2026-01-01T00:00:00+00:00"},
            {"id": "b", "cwd": "/tmp/alpha", "tokens_used": 50, "goal_status": "usage_limited", "updated_at": "2026-01-02T00:00:00+00:00"},
            {"id": "c", "cwd": "/tmp/beta", "tokens_used": 10, "goal_status": "complete", "updated_at": "2026-01-03T00:00:00+00:00"},
        ]
        with patch.object(app, "get_thread_rows", return_value=rows):
            projects = app.get_project_rows()
        alpha = next(item for item in projects if item["cwd"] == "/tmp/alpha")
        self.assertEqual(alpha["thread_count"], 2)
        self.assertEqual(alpha["tokens_used"], 150)
        self.assertEqual(alpha["active"], 1)
        self.assertEqual(alpha["limited"], 1)


if __name__ == "__main__":
    unittest.main()
