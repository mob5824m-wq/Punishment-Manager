"""Authentication and session tests for the server-side web dashboard."""

from __future__ import annotations

import os
import re
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


class RulesPreviewTests(unittest.IsolatedAsyncioTestCase):
    """The rules Markdown preview used by the dashboard editor.

    Rendering lives in discord_markdown.py (well covered by
    tests/test_markdown.py); these tests pin down the HTTP contract: the
    endpoint is session- and CSRF-protected, it never mutates anything, and it
    shares the publish path's length limit.
    """

    def setUp(self) -> None:
        self.bot = GuildFakeBot()
        self.saved = []
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

    async def _login(self, client) -> str:
        login = await client.post("/api/login", json={"token": self.server._token})
        self.assertEqual(login.status, 200)
        return (await login.json())["csrfToken"]

    async def test_preview_renders_markdown_and_hints(self) -> None:
        url = f"/api/guilds/{GUILD_ID}/rules/preview"
        async with TestClient(TestServer(self.server._build_app())) as client:
            csrf = await self._login(client)
            response = await client.post(
                url,
                json={"text": "# Rules\n**be kind**\n- no spam\n\n**unclosed"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(response.status, 200)
            body = await response.json()
            self.assertIn('class="dm-h1">Rules<', body["html"])
            self.assertIn("<strong>be kind</strong>", body["html"])
            self.assertIn("<li>no spam</li>", body["html"])
            self.assertEqual(body["length"], len("# Rules\n**be kind**\n- no spam\n\n**unclosed"))
            self.assertEqual(body["limit"], 4096)
            self.assertTrue(any("bold" in note for note in body["warnings"]))
            # Rendering a preview must not publish, save, or create a post.
            self.assertEqual(self.saved, [])
            self.assertFalse(hasattr(self.bot, "sent"))

    async def test_preview_escapes_html_from_the_rules_text(self) -> None:
        url = f"/api/guilds/{GUILD_ID}/rules/preview"
        async with TestClient(TestServer(self.server._build_app())) as client:
            csrf = await self._login(client)
            response = await client.post(
                url,
                json={"text": "<img src=x onerror=alert(1)>"},
                headers={"X-CSRF-Token": csrf},
            )
            body = await response.json()
            self.assertNotIn("<img", body["html"])
            self.assertIn("&lt;img", body["html"])

    async def test_preview_rejects_overlong_text(self) -> None:
        url = f"/api/guilds/{GUILD_ID}/rules/preview"
        async with TestClient(TestServer(self.server._build_app())) as client:
            csrf = await self._login(client)
            response = await client.post(
                url,
                json={"text": "x" * 4097},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(response.status, 400)
            self.assertIn("4096", (await response.json())["error"])

    async def test_preview_requires_a_session_and_csrf(self) -> None:
        url = f"/api/guilds/{GUILD_ID}/rules/preview"
        async with TestClient(TestServer(self.server._build_app())) as client:
            anonymous = await client.post(url, json={"text": "# hi"})
            self.assertEqual(anonymous.status, 401)
            csrf = await self._login(client)
            no_csrf = await client.post(url, json={"text": "# hi"})
            self.assertEqual(no_csrf.status, 403)
            unknown_guild = await client.post(
                "/api/guilds/999/rules/preview",
                json={"text": "# hi"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(unknown_guild.status, 404)


class DashboardMarkupTests(unittest.TestCase):
    """Static checks on the served UI, which no test can click through.

    The rules editor added a live preview whose elements are looked up by id in
    the page script; a typo there fails silently in the browser (a null
    dereference on the first keystroke), so every ``$("id")`` the script uses
    must exist in the markup.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (REPO_ROOT / "dashboard.html").read_text(encoding="utf-8")
        cls.script = cls.html.rsplit("<script>", 1)[1]

    def test_every_id_the_script_looks_up_exists(self) -> None:
        markup_ids = set(re.findall(r'id="([^"]+)"', self.html))
        used_ids = set(re.findall(r'\$\("([^"]+)"\)', self.script))
        self.assertTrue(used_ids, "no element lookups found - did the script move?")
        self.assertEqual(used_ids - markup_ids, set())

    def test_rules_editor_exposes_a_preview_and_toolbar(self) -> None:
        for element in (
            'id="rules-text"',
            'id="rules-preview"',
            'id="rules-preview-title"',
            'id="rules-hints"',
            'id="rules-count"',
            'id="rules-form"',
            'class="md-toolbar"',
        ):
            with self.subTest(element=element):
                self.assertIn(element, self.html)

    def test_every_toolbar_button_has_a_handler(self) -> None:
        kinds = set(re.findall(r'data-md="([^"]+)"', self.html))
        self.assertTrue(kinds)
        # insertMarkdown() dispatches through these tables: wrapping pairs for
        # inline formatting, line prefixes for block-level formatting.
        handled: set[str] = set()
        for table in ("MD_WRAPS", "MD_LINE_PREFIXES", "MD_LINE_MARKERS"):
            block = self.script.split(table, 1)[1].split("};", 1)[0]
            # Object keys are preceded by the opening brace or a comma, which
            # keeps a "https://…" value from being mistaken for an entry.
            handled |= set(
                re.findall(r"(?:^|[{,])\s*([A-Za-z]\w*)\s*:", block, re.M)
            )
        self.assertEqual(kinds - handled, set(), "toolbar buttons without a handler")
        self.assertEqual(handled - kinds, set(), "handlers for buttons that do not exist")

    def test_unsupported_toolbar_buttons_are_not_offered(self) -> None:
        # Discord shows tables, images and horizontal rules literally in an
        # embed, so the editor must not suggest them.
        lowered = self.html.lower()
        for absent in ('data-md="table"', 'data-md="image"', 'data-md="hr"'):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, lowered)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
