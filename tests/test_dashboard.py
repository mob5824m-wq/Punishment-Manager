"""Authentication and session tests for the server-side web dashboard."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from dashboard import DashboardServer, _json_safe_ids, _snowflake, ensure_dashboard_token  # noqa: E402


# A realistic Discord snowflake: larger than 2**53, so a JavaScript client
# rounds it if it arrives as a JSON number (…789 comes back as …800).
GUILD_ID = 1234567890123456789


class FakeBot:
    config = {}
    guilds = []
    user = None

    def is_ready(self):
        return False


class FakeGuild:
    id = GUILD_ID
    name = "Test Guild"
    icon = None
    member_count = 7
    owner_id = 222222222222222222
    roles = []
    text_channels = []


class GuildFakeBot(FakeBot):
    guilds = [FakeGuild()]

    def get_guild(self, guild_id):
        return FakeGuild() if int(guild_id) == GUILD_ID else None


class SnowflakeSerialisationTests(unittest.TestCase):
    """Discord ids must cross the JSON boundary as strings.

    Regression test: sending snowflakes as JSON numbers made the browser round
    them, so selecting a server produced "The bot is not connected to that
    server." even when the bot was connected.
    """

    def test_snowflake_helper(self) -> None:
        self.assertEqual(_snowflake(GUILD_ID), str(GUILD_ID))
        self.assertIsNone(_snowflake(None))
        self.assertIsNone(_snowflake(""))

    def test_row_ids_are_stringified(self) -> None:
        row = _json_safe_ids(
            {
                "user_id": 333333333333333333,
                "moderator_id": 444444444444444444,
                "guild_id": 555555555555555555,
                "id": 3,
                "reason": "no reason",
                "count": 2,
            }
        )
        self.assertEqual(row["user_id"], "333333333333333333")
        self.assertEqual(row["moderator_id"], "444444444444444444")
        self.assertEqual(row["guild_id"], "555555555555555555")
        self.assertEqual(row["id"], 3)  # database row key, not a snowflake
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["reason"], "no reason")


class DashboardTokenTests(unittest.TestCase):
    def test_token_is_generated_once_and_saved(self) -> None:
        config = {"dashboard_token": ""}
        writes = []
        token = ensure_dashboard_token(config, writes.append)
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(config["dashboard_token"], token)
        self.assertEqual(writes[0]["dashboard_token"], token)
        self.assertEqual(ensure_dashboard_token(config, writes.append), token)
        self.assertEqual(len(writes), 1)

    def test_environment_token_overrides_stored_value(self) -> None:
        config = {"dashboard_token": "S" * 40}
        with patch.dict(os.environ, {"PUNISHMENT_MANAGER_DASHBOARD_TOKEN": "E" * 40}):
            token = ensure_dashboard_token(
                config,
                lambda _cfg: self.fail("environment token should not be saved"),
            )
        self.assertEqual(token, "E" * 40)

    def test_weak_configured_token_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ensure_dashboard_token({"dashboard_token": "short"}, lambda _cfg: None)


class DashboardSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.saved = []
        self.bot = FakeBot()
        self.server = DashboardServer(
            self.bot,
            save_config=self.saved.append,
            db_fetchall=lambda *_args: [],
            db_fetchone=lambda *_args: None,
            db_execute=lambda *_args: None,
            archive_punishment=lambda *_args, **_kwargs: None,
            get_guild_config=lambda *_args: None,
            get_staff_channel_id=lambda *_args: None,
            should_dm_user=lambda *_args: True,
            is_protected_member=lambda *_args, **_kwargs: None,
            parse_duration=lambda _value: None,
            format_duration=lambda value: str(value),
        )
        self.server._token = "T" * 48

    async def test_login_cookie_csrf_and_logout_flow(self) -> None:
        async with TestClient(TestServer(self.server._build_app())) as client:
            page = await client.get("/")
            self.assertEqual(page.status, 200)
            self.assertIn("Server Console", await page.text())
            hostile_host = await client.get("/", headers={"Host": "attacker.example"})
            self.assertEqual(hostile_host.status, 400)

            denied = await client.get("/api/overview")
            self.assertEqual(denied.status, 401)

            bad_login = await client.post("/api/login", json={"token": "not-the-key"})
            self.assertEqual(bad_login.status, 401)

            login = await client.post("/api/login", json={"token": self.server._token})
            self.assertEqual(login.status, 200)
            body = await login.json()
            csrf = body["csrfToken"]
            self.assertTrue(login.cookies.get("pm_dashboard_session").value)
            self.assertTrue(login.cookies.get("pm_dashboard_session")["httponly"])

            overview = await client.get("/api/overview")
            self.assertEqual(overview.status, 200)
            self.assertEqual((await overview.json())["bot"]["guildCount"], 0)

            csrf_denied = await client.post("/api/logout", json={})
            self.assertEqual(csrf_denied.status, 403)

            logout = await client.post(
                "/api/logout",
                json={},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(logout.status, 200)
            denied_again = await client.get("/api/overview")
            self.assertEqual(denied_again.status, 401)

    async def _login(self, client) -> str:
        login = await client.post("/api/login", json={"token": self.server._token})
        self.assertEqual(login.status, 200)
        return (await login.json())["csrfToken"]

    async def test_guild_ids_cross_the_wire_as_strings(self) -> None:
        self.server.bot = GuildFakeBot()
        async with TestClient(TestServer(self.server._build_app())) as client:
            await self._login(client)

            overview = await client.get("/api/overview")
            self.assertEqual(overview.status, 200)
            raw = await overview.text()
            body = await overview.json()
            self.assertIsInstance(body["guilds"][0]["id"], str)
            self.assertEqual(body["guilds"][0]["id"], str(GUILD_ID))
            # A JSON number here is what the browser rounds.
            self.assertIn(f'"id": "{GUILD_ID}"', raw)

            detail = await client.get(f"/api/guilds/{GUILD_ID}")
            self.assertEqual(detail.status, 200)
            self.assertEqual((await detail.json())["guild"]["ownerId"], "222222222222222222")

    async def test_browser_rounded_id_is_rejected(self) -> None:
        """Documents the failure the string encoding prevents."""
        rounded = int(float(GUILD_ID))  # what JavaScript does to the number
        self.assertNotEqual(rounded, GUILD_ID)
        self.server.bot = GuildFakeBot()
        async with TestClient(TestServer(self.server._build_app())) as client:
            await self._login(client)
            response = await client.get(f"/api/guilds/{rounded}")
            self.assertEqual(response.status, 404)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
