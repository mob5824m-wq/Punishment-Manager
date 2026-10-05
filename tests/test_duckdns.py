"""
Tests for the DuckDNS integration (`duckdns.py`).

Motivation: reaching a home-hosted dashboard under a dynamic-DNS name is a
four-part setup (a current record, an allow-listed name, a listener that is
reachable, TLS in front of it) and every part fails differently from the
browser's point of view - a connection that never arrives, a 400 that looks
like a server bug, a login that bounces because the cookie was dropped. These
tests pin down the updater's protocol behaviour and the checks that turn those
symptoms into the setting to change.

The HTTP tests run against a real local aiohttp server standing in for
`www.duckdns.org`, so the request shape, the timeout path and the response
parsing are exercised together.

Run with either:
    python -m pytest tests/test_duckdns.py
    python tests/test_duckdns.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

import duckdns  # noqa: E402

CONFIGURED = {
    "duckdns_domain": "myhome",
    "duckdns_token": "secret-token",
    "dashboard_enabled": True,
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8765,
    "dashboard_allowed_hosts": [],
}


class DomainNormalisationTests(unittest.TestCase):
    def test_accepts_what_a_user_pastes(self) -> None:
        for value in ("myhome", "myhome.duckdns.org", "MYHOME.duckdns.org",
                      "https://myhome.duckdns.org/", "http://myhome.duckdns.org"):
            with self.subTest(value=value):
                self.assertEqual(duckdns.normalize_domain(value), "myhome")

    def test_empty_and_junk(self) -> None:
        self.assertEqual(duckdns.normalize_domain(None), "")
        self.assertEqual(duckdns.normalize_domain("   "), "")

    def test_fqdn(self) -> None:
        self.assertEqual(duckdns.domain_fqdn("myhome"), "myhome.duckdns.org")
        self.assertEqual(duckdns.domain_fqdn("myhome.duckdns.org"), "myhome.duckdns.org")
        self.assertEqual(duckdns.domain_fqdn(""), "")


class ConfigTests(unittest.TestCase):
    def test_credentials_need_both_halves(self) -> None:
        self.assertEqual(duckdns.credentials({}), None)
        self.assertEqual(duckdns.credentials({"duckdns_domain": "myhome"}), None)
        self.assertEqual(duckdns.credentials({"duckdns_token": "t"}), None)
        self.assertEqual(duckdns.credentials(CONFIGURED), ("myhome", "secret-token"))

    def test_environment_overrides_the_file(self) -> None:
        env = {"SENTINEL_DUCKDNS_DOMAIN": "otherenv", "SENTINEL_DUCKDNS_TOKEN": "env-token"}
        original = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        self.addCleanup(
            lambda: [os.environ.pop(key, None) if value is None else os.environ.update({key: value})
                     for key, value in original.items()]
        )
        self.assertEqual(duckdns.credentials(CONFIGURED), ("otherenv", "env-token"))

    def test_disabled_is_not_configured(self) -> None:
        self.assertTrue(duckdns.configured(CONFIGURED))
        self.assertFalse(duckdns.configured({**CONFIGURED, "duckdns_enabled": False}))

    def test_interval_is_clamped_to_what_the_service_tolerates(self) -> None:
        # DuckDNS asks for at most one update every five minutes.
        self.assertEqual(duckdns.interval_seconds(CONFIGURED), 5 * 60)
        self.assertEqual(duckdns.interval_seconds({**CONFIGURED, "duckdns_interval_minutes": 0}), 5 * 60)
        self.assertEqual(duckdns.interval_seconds({**CONFIGURED, "duckdns_interval_minutes": 0.1}),
                         duckdns.MIN_INTERVAL_SECONDS)
        self.assertEqual(duckdns.interval_seconds({**CONFIGURED, "duckdns_interval_minutes": 100000}),
                         duckdns.MAX_INTERVAL_SECONDS)
        self.assertEqual(duckdns.interval_seconds({**CONFIGURED, "duckdns_interval_minutes": "junk"}),
                         5 * 60)


class ResponseParsingTests(unittest.TestCase):
    def test_verbose_success(self) -> None:
        ok, ip, changed, _ = duckdns.parse_update_response("OK\n203.0.113.7\nUPDATED")
        self.assertTrue(ok)
        self.assertEqual(ip, "203.0.113.7")
        self.assertTrue(changed)

    def test_verbose_no_change(self) -> None:
        ok, ip, changed, _ = duckdns.parse_update_response("OK\n203.0.113.7\nNOCHANGE")
        self.assertTrue(ok)
        self.assertFalse(changed)

    def test_bare_ok(self) -> None:
        ok, ip, changed, _ = duckdns.parse_update_response("OK")
        self.assertTrue(ok)
        self.assertIsNone(ip)
        self.assertFalse(changed)

    def test_ko_is_a_failure_carrying_what_the_service_said(self) -> None:
        ok, _ip, _changed, detail = duckdns.parse_update_response("KO")
        self.assertFalse(ok)
        self.assertEqual(detail, "KO")

    def test_empty_response_is_a_failure_not_a_success(self) -> None:
        ok, _ip, _changed, detail = duckdns.parse_update_response("")
        self.assertFalse(ok)
        self.assertIn("empty", detail)


class UpdateRequestTests(unittest.IsolatedAsyncioTestCase):
    """`update_record` against a stand-in for www.duckdns.org."""

    async def asyncSetUp(self) -> None:
        self.queries: list[dict] = []
        self.reply = "OK\n203.0.113.7\nUPDATED"
        self.status = 200
        self.delay = 0.0

        async def handler(request: web.Request) -> web.Response:
            self.queries.append(dict(request.query))
            if self.delay:
                await asyncio.sleep(self.delay)
            return web.Response(text=self.reply, status=self.status)

        app = web.Application()
        app.router.add_get("/update", handler)
        self.server = TestServer(app)
        await self.server.start_server()
        self.url = str(self.server.make_url("/update"))
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)
        self.addAsyncCleanup(self.server.close)

    async def test_update_sends_the_protocol_duckdns_expects(self) -> None:
        result = await duckdns.update_record(
            "myhome.duckdns.org", "secret-token", session=self.session, url=self.url
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.ip, "203.0.113.7")
        self.assertTrue(result.changed)
        self.assertEqual(result.domain, "myhome.duckdns.org")

        query = self.queries[-1]
        # The subname only, the token, and an *empty* ip so DuckDNS records the
        # address the request came from - that is what makes dynamic DNS work.
        self.assertEqual(query["domains"], "myhome")
        self.assertEqual(query["token"], "secret-token")
        self.assertEqual(query["ip"], "")
        self.assertEqual(query["verbose"], "true")

    async def test_a_ko_is_reported_without_leaking_the_token(self) -> None:
        self.reply = "KO"
        result = await duckdns.update_record("myhome", "wrong-token", session=self.session, url=self.url)
        self.assertFalse(result.ok)
        self.assertIn("KO", result.describe())
        self.assertNotIn("wrong-token", result.describe())

    async def test_http_errors_are_reported(self) -> None:
        self.status = 500
        result = await duckdns.update_record("myhome", "t", session=self.session, url=self.url)
        self.assertFalse(result.ok)
        self.assertIn("HTTP 500", result.detail)

    async def test_an_unreachable_service_is_reported_not_raised(self) -> None:
        # A closed port: the updater must survive a network outage.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = await duckdns.update_record(
            "myhome", "t", session=self.session,
            url=f"http://127.0.0.1:{port}/update", timeout=2,
        )
        self.assertFalse(result.ok)
        self.assertIn("unreachable", result.detail)

    async def test_a_timeout_is_reported(self) -> None:
        self.delay = 0.5
        result = await duckdns.update_record(
            "myhome", "t", session=self.session, url=self.url, timeout=0.05
        )
        self.assertFalse(result.ok)
        self.assertIn("unreachable", result.detail)

    async def test_missing_configuration_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            await duckdns.update_record("", "t", session=self.session, url=self.url)
        with self.assertRaises(ValueError):
            await duckdns.update_record("myhome", "", session=self.session, url=self.url)

    async def test_describe_never_mentions_the_token(self) -> None:
        ok = await duckdns.update_record("myhome", "secret-token", session=self.session, url=self.url)
        self.assertIn("203.0.113.7", ok.describe())
        self.assertNotIn("secret-token", ok.describe())


class UpdaterLoopTests(unittest.IsolatedAsyncioTestCase):
    """The timer that keeps the record fresh while the bot runs.

    These wait for the updater to make progress instead of sleeping for a
    fixed time and asserting it has. A 50 ms timer on a loaded two-core
    Windows runner is not the laptop's 50 ms timer, and a suite that assumes
    otherwise fails for reasons that have nothing to do with the code.
    """

    async def wait_until(self, predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
        """Poll `predicate` until it is true or `timeout` elapses."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(interval)
        return bool(predicate())

    async def asyncSetUp(self) -> None:
        self.calls = 0

        async def handler(_request: web.Request) -> web.Response:
            self.calls += 1
            return web.Response(text="OK\n203.0.113.7\nNOCHANGE")

        app = web.Application()
        app.router.add_get("/update", handler)
        self.server = TestServer(app)
        await self.server.start_server()
        self.url = str(self.server.make_url("/update"))
        self.addAsyncCleanup(self.server.close)

    async def test_it_updates_repeatedly_and_stops_cleanly(self) -> None:
        config = {**CONFIGURED, "dashboard_port": 8765}
        session = aiohttp.ClientSession()
        self.addAsyncCleanup(session.close)
        results: list[duckdns.UpdateResult] = []
        updater = duckdns.DuckDNSUpdater(
            config,
            session=session,
            interval_seconds=0.05,
            on_result=results.append,
            url=self.url,  # the stand-in for www.duckdns.org
        )

        self.assertTrue(await updater.start())
        self.assertTrue(
            await self.wait_until(lambda: self.calls >= 2),
            "the updater should refresh on its interval",
        )
        self.assertTrue(await self.wait_until(lambda: len(results) >= 2))
        await updater.stop()

        self.assertFalse(updater.running)
        self.assertTrue(all(result.ok for result in results))
        # stop() releases the timer: nothing new may start, and whatever was in
        # flight when it was cancelled is allowed to land first.
        await asyncio.sleep(0.3)
        seen = self.calls
        await asyncio.sleep(0.3)
        self.assertEqual(self.calls, seen, "stop() must stop the timer")

    async def test_start_is_a_no_op_when_not_configured(self) -> None:
        updater = duckdns.DuckDNSUpdater({})
        self.assertFalse(await updater.start())
        self.assertFalse(updater.running)
        self.assertIsNone(await updater.update_now())
        await updater.stop()

    async def test_a_failure_does_not_stop_the_loop(self) -> None:
        with socket.socket() as probe:  # a URL nothing is listening on
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        updater = duckdns.DuckDNSUpdater(
            CONFIGURED,
            interval_seconds=0.05,
            url=f"http://127.0.0.1:{port}/update",
        )

        self.assertTrue(await updater.start())
        self.assertTrue(
            await self.wait_until(lambda: updater.last_result is not None),
            "the first update should have run by now",
        )
        self.assertTrue(updater.running, "a failed update must not kill the updater")
        self.assertFalse(updater.last_result.ok)
        await updater.stop()


class DashboardWarningTests(unittest.TestCase):
    """The checks that translate a browser symptom into the setting to change."""

    def warnings(self, **overrides) -> str:
        return " \n".join(duckdns.dashboard_warnings({**CONFIGURED, **overrides}))

    def test_nothing_configured_says_nothing(self) -> None:
        self.assertEqual(duckdns.dashboard_warnings({}), [])
        self.assertEqual(duckdns.dashboard_warnings({"duckdns_domain": "myhome"}), [])

    def test_the_allowlist_is_checked(self) -> None:
        # The Host header allowlist is the DNS-rebinding defence, and it is
        # what makes a DuckDNS name answer 400 until it is listed.
        self.assertIn("dashboard_allowed_hosts", self.warnings())
        self.assertNotIn(
            "not in 'dashboard_allowed_hosts'",
            self.warnings(dashboard_allowed_hosts=["myhome.duckdns.org"]),
        )

    def test_the_public_url_counts_as_allowlisted(self) -> None:
        self.assertNotIn(
            "not in 'dashboard_allowed_hosts'",
            self.warnings(dashboard_public_url="https://myhome.duckdns.org"),
        )

    def test_a_loopback_listener_is_called_out(self) -> None:
        self.assertIn("loopback only", self.warnings(dashboard_allowed_hosts=["myhome.duckdns.org"]))
        self.assertNotIn(
            "loopback only",
            self.warnings(
                dashboard_allowed_hosts=["myhome.duckdns.org"],
                dashboard_host="0.0.0.0",
                dashboard_public_url="https://myhome.duckdns.org",
            ),
        )

    def test_plain_http_is_called_out(self) -> None:
        self.assertIn("clear text", self.warnings(dashboard_allowed_hosts=["myhome.duckdns.org"]))
        self.assertNotIn(
            "clear text",
            self.warnings(
                dashboard_allowed_hosts=["myhome.duckdns.org"],
                dashboard_public_url="https://myhome.duckdns.org",
            ),
        )

    def test_a_disabled_dashboard_is_named(self) -> None:
        warnings = self.warnings(dashboard_enabled=False)
        self.assertIn("disabled", warnings)

    def test_https_terminated_upstream_still_needs_a_secure_cookie(self) -> None:
        # This module's job: with dashboard_public_url on https:// but the
        # cookie not marked secure, a downgrade anywhere would leak it.
        self.assertIn(
            "dashboard_secure_cookie",
            self.warnings(
                dashboard_public_url="https://myhome.duckdns.org",
                dashboard_secure_cookie=False,
            ),
        )
        self.assertNotIn(
            "dashboard_secure_cookie",
            self.warnings(
                dashboard_public_url="https://myhome.duckdns.org",
                dashboard_secure_cookie=True,
            ),
        )
        # The key's default is false, so a missing entry has to be caught too.
        self.assertIn(
            "dashboard_secure_cookie",
            self.warnings(dashboard_public_url="https://myhome.duckdns.org"),
        )

    def test_the_reverse_case_belongs_to_the_dashboard(self) -> None:
        # A secure cookie with no HTTPS anywhere is dashboard.py's warning (it
        # knows the listener's own scheme); here it must not be duplicated.
        self.assertNotIn(
            "dashboard_secure_cookie",
            self.warnings(dashboard_secure_cookie=True),
        )

    def test_status_line_names_the_domain(self) -> None:
        self.assertIn("myhome.duckdns.org", duckdns.describe_status(CONFIGURED))
        self.assertIn("not configured", duckdns.describe_status({}))
        self.assertIn("disabled", duckdns.describe_status({**CONFIGURED, "duckdns_enabled": False}))


class CommandLineTests(unittest.TestCase):
    """`sentinel --duckdns` / `--dashboard`, as documented in the README."""

    def setUp(self) -> None:
        self.tmp = Path(__import__("tempfile").mkdtemp(prefix="sentinel-duckdns-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def write_config(self, config: dict) -> None:
        (self.tmp / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def run_bot(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "bot.py"), *args],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            env={**os.environ, "SENTINEL_CONFIG": str(self.tmp / "config.json")},
            timeout=60,
        )

    def test_duckdns_without_configuration_explains_itself(self) -> None:
        self.write_config({"bot_token": "", "dashboard_enabled": True})
        result = self.run_bot("--duckdns")
        self.assertEqual(result.returncode, 1)
        self.assertIn("duckdns_domain", result.stderr)

    def test_dashboard_command_reports_the_remote_access_state(self) -> None:
        self.write_config({
            "bot_token": "",
            "dashboard_enabled": True,
            "dashboard_host": "127.0.0.1",
            "dashboard_port": 8765,
            "duckdns_domain": "myhome",
            "duckdns_token": "t",
        })
        result = self.run_bot("--dashboard")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("Listening on:", result.stdout)
        self.assertIn("myhome.duckdns.org", result.stdout)
        # The actionable part: what to fix before a remote browser can log in.
        self.assertIn("dashboard_allowed_hosts", result.stdout)

    def test_help_lists_the_new_commands(self) -> None:
        result = self.run_bot("--help")
        self.assertEqual(result.returncode, 0)
        for flag in ("--dashboard ", "--duckdns"):
            self.assertIn(flag, result.stdout)


class BotWiringTests(unittest.TestCase):
    """The updater has to be part of the bot's lifecycle, not just importable.

    A timer that is never started, or never stopped, looks identical to a
    broken one from the dashboard: the record quietly goes stale. These are
    source-level assertions in the same spirit as the build-script wiring tests.
    """

    def setUp(self) -> None:
        self.bot_source = (REPO_ROOT / "bot.py").read_text(encoding="utf-8")

    def test_default_config_has_the_duckdns_keys(self) -> None:
        for key in ("duckdns_enabled", "duckdns_domain", "duckdns_token",
                    "duckdns_interval_minutes"):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', self.bot_source)

    def test_the_updater_is_started_and_stopped(self) -> None:
        self.assertIn("await self.duckdns.stop()", self.bot_source)
        # start() belongs in setup_hook. Two definitions exist (the class
        # method, then the module-level override that keeps the cog
        # registration), and it is the last one the bot runs.
        hook = self.bot_source.rsplit("async def setup_hook", 1)[1]
        hook = hook.split("SentinelBot.setup_hook", 1)[0]
        self.assertIn("await self.duckdns.start()", hook)
        # A DuckDNS outage must not stop the Discord bot from starting.
        self.assertIn("except Exception:", hook.split("await self.duckdns.start()", 1)[1])

    def test_startup_reports_remote_access_problems(self) -> None:
        self.assertIn("_log_remote_access_notes(self.config)", self.bot_source)
        notes = self.bot_source.split("def _log_remote_access_notes", 1)[1]
        self.assertIn("duckdns.dashboard_warnings(config)", notes)

    def test_the_packaging_lists_include_the_module(self) -> None:
        # bot.py imports duckdns, so a bundle that omits it fails at import.
        spec = (REPO_ROOT / "build" / "pyinstaller.spec").read_text(encoding="utf-8")
        self.assertIn("'duckdns'", spec)
        paths_test = (REPO_ROOT / "tests" / "test_paths.py").read_text(encoding="utf-8")
        self.assertIn('"duckdns.py"', paths_test)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
