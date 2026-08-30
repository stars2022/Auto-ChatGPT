import unittest
from copy import deepcopy
from unittest.mock import patch

import app


class AppLogicTests(unittest.TestCase):
    def _tick(self, state, now, goals=None, threads=None, enqueue_result=(True, "queued")):
        """Run one scheduler tick with all external state held deterministic."""
        written = {}
        state = deepcopy(state)
        state.setdefault("settings", {}).update({
            "official_poll_minutes": 60,
            "notifications": False,
        })
        state["official_usage"] = {
            "status": "ok",
            "checked_at": app.iso(now),
            "windows": [],
        }
        state.setdefault("usage_probe", {"status": "not_configured"})
        with patch.object(app, "now_ms", return_value=now), \
             patch.object(app, "read_app_state", return_value=state), \
             patch.object(app, "write_app_state", side_effect=lambda value: written.setdefault("state", deepcopy(value))), \
             patch.object(app, "get_goal_rows", return_value=goals or []), \
             patch.object(app, "get_thread_rows", return_value=threads or [{"id": "thread-1", "tokens_used": 0}]), \
             patch.object(app, "usage_probe", return_value=state.get("usage_probe", {"status": "not_configured"})), \
             patch.object(app, "enqueue", return_value=enqueue_result), \
             patch.object(app, "notification"):
            app.Scheduler().tick()
        return written["state"]

    def test_schedule_due_for_quota_recovery(self):
        schedule = {"enabled": True, "kind": "quota_recovered", "thread_id": "thread-1"}
        self.assertTrue(app.schedule_due(schedule, app.now_ms(), {"thread-1"}))
        self.assertTrue(app.schedule_due(schedule, app.now_ms(), {"__official__"}))
        self.assertTrue(app.schedule_due(schedule, app.now_ms(), {"__usage_probe__"}))
        self.assertFalse(app.schedule_due(schedule, app.now_ms(), set()))

    def test_interval_schedule_waits_for_first_interval(self):
        now = 1_700_000_000_000
        schedule = {
            "id": "s1", "name": "interval", "kind": "interval", "thread_id": "thread-1",
            "message": "继续", "enabled": True, "interval_minutes": 1,
            "created_at": now, "run_count": 0, "attempt_count": 0,
        }
        state = app.default_app_state(); state["schedules"] = [schedule]
        with patch.object(app, "enqueue") as enqueue:
            result = self._tick(state, now, threads=[{"id": "thread-1", "tokens_used": 0}])
        enqueue.assert_not_called()
        self.assertEqual(result["schedules"][0]["run_count"], 0)

    def test_interval_schedule_checks_active_target_without_sending(self):
        now = 1_700_000_000_000
        schedule = {
            "id": "s1", "name": "watchdog", "kind": "interval", "thread_id": "thread-1",
            "message": "继续", "enabled": True, "interval_minutes": 1,
            "created_at": now - 60_000, "run_count": 0, "attempt_count": 0,
            "retry_pending": True, "next_attempt_at": now,
        }
        state = app.default_app_state(); state["schedules"] = [schedule]
        result = self._tick(state, now, goals=[{"thread_id": "thread-1", "status": "active"}])
        saved = result["schedules"][0]
        self.assertEqual(saved["last_check_at"], now)
        self.assertEqual(saved["last_observed_status"], "active")
        self.assertEqual(saved["attempt_count"], 0)
        self.assertEqual(saved["run_count"], 0)
        self.assertFalse(saved["retry_pending"])
        early = self._tick(result, now + 30_000, goals=[{"thread_id": "thread-1", "status": "complete"}])
        self.assertEqual(early["schedules"][0]["attempt_count"], 0)
        due = self._tick(early, now + 60_000, goals=[{"thread_id": "thread-1", "status": "complete"}])
        self.assertEqual(due["schedules"][0]["attempt_count"], 1)
        self.assertEqual(due["schedules"][0]["run_count"], 1)

    def test_interval_schedule_sends_only_after_target_stops(self):
        now = 1_700_000_000_000
        schedule = {
            "id": "s1", "name": "watchdog", "kind": "interval", "thread_id": "thread-1",
            "message": "继续", "enabled": True, "interval_minutes": 1,
            "created_at": now - 60_000, "run_count": 0, "attempt_count": 0,
        }
        state = app.default_app_state(); state["schedules"] = [schedule]
        result = self._tick(state, now, goals=[{"thread_id": "thread-1", "status": "complete"}])
        saved = result["schedules"][0]
        self.assertEqual(saved["last_observed_status"], "complete")
        self.assertEqual(saved["attempt_count"], 1)
        self.assertEqual(saved["run_count"], 1)
        self.assertTrue(saved["awaiting_active"])
        repeated_stop = self._tick(result, now + 60_000, goals=[{"thread_id": "thread-1", "status": "complete"}])
        self.assertEqual(repeated_stop["schedules"][0]["attempt_count"], 1)
        observed_active = self._tick(repeated_stop, now + 90_000, goals=[{"thread_id": "thread-1", "status": "active"}])
        self.assertFalse(observed_active["schedules"][0]["awaiting_active"])
        stopped_again = self._tick(observed_active, now + 120_000, goals=[{"thread_id": "thread-1", "status": "complete"}])
        self.assertEqual(stopped_again["schedules"][0]["attempt_count"], 2)
        self.assertEqual(stopped_again["schedules"][0]["run_count"], 2)

    def test_at_time_success_completes_and_disables_schedule(self):
        now = 1_700_000_000_000
        schedule = {
            "id": "s1", "name": "once", "kind": "at_time", "thread_id": "thread-1",
            "message": "继续", "enabled": True, "run_at": app.iso(now - 1),
            "created_at": now - 10_000, "run_count": 0, "attempt_count": 0,
        }
        state = app.default_app_state(); state["schedules"] = [schedule]
        result = self._tick(state, now)
        saved = result["schedules"][0]
        self.assertFalse(saved["enabled"])
        self.assertIsNotNone(saved["completed_at"])
        self.assertEqual(saved["run_count"], 1)
        self.assertEqual(saved["attempt_count"], 1)

    def test_network_failure_retries_then_stops_at_limit(self):
        now = 1_700_000_000_000
        schedule = {
            "id": "s1", "name": "network", "kind": "interval", "thread_id": "thread-1",
            "message": "继续", "enabled": True, "interval_minutes": 1,
            "created_at": now - 60_000, "run_count": 0, "attempt_count": 0,
            "retry_on_network": True, "max_attempts": 1, "backoff_seconds": 1,
        }
        state = app.default_app_state(); state["schedules"] = [schedule]
        first = self._tick(state, now, enqueue_result=(False, "network timeout"))
        pending = first["schedules"][0]
        self.assertTrue(pending["enabled"])
        self.assertTrue(pending["retry_pending"])
        self.assertEqual(pending["attempt_count"], 1)
        second = self._tick(first, now + 1_000, enqueue_result=(False, "network timeout"))
        stopped = second["schedules"][0]
        self.assertFalse(stopped["enabled"])
        self.assertFalse(stopped["retry_pending"])
        self.assertEqual(stopped["attempt_count"], 2)

    def test_interval_watchdog_waits_for_quota_then_resumes_stopped_target(self):
        now = 1_700_000_000_000
        schedule = {
            "id": "s1", "name": "quota", "kind": "interval", "thread_id": "thread-1",
            "message": "继续", "enabled": True, "interval_minutes": 1,
            "created_at": now - 60_000, "run_count": 0, "attempt_count": 0,
            "retry_on_quota": True,
        }
        state = app.default_app_state(); state["schedules"] = [schedule]
        first = self._tick(state, now, goals=[{"thread_id": "thread-1", "status": "usage_limited"}])
        waiting = first["schedules"][0]
        self.assertTrue(waiting["enabled"])
        self.assertTrue(waiting["waiting_for_quota"])
        self.assertFalse(waiting["retry_pending"])
        self.assertEqual(waiting["attempt_count"], 0)
        second = self._tick(first, now + 1_000, goals=[{"thread_id": "thread-1", "status": "complete"}], enqueue_result=(True, "queued"))
        resumed = second["schedules"][0]
        self.assertFalse(resumed["waiting_for_quota"])
        self.assertEqual(resumed["run_count"], 1)
        self.assertEqual(resumed["attempt_count"], 1)

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

    def test_official_manual_check_falls_back_to_oauth(self):
        cli_error = {"status": "cli_error", "detail": "app-server unavailable"}
        oauth_ok = {"status": "ok", "credential_source": "auth.json", "windows": []}
        with patch.object(app, "codex_cli_status_probe", return_value=cli_error), patch.object(app, "official_usage_probe", return_value=oauth_ok):
            result = app.current_official_usage_probe()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["credential_source"], "auth.json")
        self.assertEqual(result["cli_fallback"]["status"], "cli_error")

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
