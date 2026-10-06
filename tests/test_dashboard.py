"""Authentication and session tests for the server-side web dashboard."""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import discord  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from dashboard import (  # noqa: E402
    DashboardServer,
    _json_safe_ids,
    _reaction_post_payload,
    _snowflake,
    ensure_dashboard_token,
)
from rules import (  # noqa: E402
    RulesMixin,
    validate_post_channel,
    validate_self_assignable_role,
)


# A realistic Discord snowflake: larger than 2**53, so a JavaScript client
# rounds it if it arrives as a JSON number (…789 comes back as …800).
GUILD_ID = 1234567890123456789
OWNER_ID = 222222222222222222


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


def _self_signed_certificate(case: unittest.TestCase) -> tuple[Path, Path]:
    """Write a throwaway self-signed TLS certificate for a test.

    Generated rather than committed: a checked-in private key is a bad habit
    even for localhost, and openssl is present wherever this suite runs.
    """
    import shutil
    import subprocess
    import tempfile

    if shutil.which("openssl") is None:  # pragma: no cover - CI images have it
        case.skipTest("openssl is not available to generate a test certificate")
    tmp = Path(tempfile.mkdtemp(prefix="sentinel-tls-"))
    case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-subj", "/CN=myhome.duckdns.org",
        ],
        check=True, capture_output=True,
    )
    return cert, key


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
        with patch.dict(os.environ, {"SENTINEL_DASHBOARD_TOKEN": "E" * 40}):
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
            self.assertTrue(login.cookies.get("sentinel_dashboard_session").value)
            self.assertTrue(login.cookies.get("sentinel_dashboard_session")["httponly"])

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


class RemoteAccessTests(unittest.IsolatedAsyncioTestCase):
    """Reaching the dashboard under a public name such as a DuckDNS subdomain.

    A remote visitor is allowed on purpose here, so the failure modes are
    configuration ones, and every one of them looks like a different kind of
    "the dashboard is broken" in the browser:

    * a 400 ``Unrecognized Host header`` from the anti-DNS-rebinding check,
    * a login that succeeds and immediately bounces (a cookie the browser
      refuses to store because it was not marked secure, or one sent over
      plain HTTP and dropped),
    * a connection that never arrives (loopback-only listener, no proxy).

    These tests pin the behaviour and the guidance that turns each symptom into
    the setting to change.
    """

    def make_server(self, **config):
        """A dashboard server over the given config; no socket is bound."""
        bot = FakeBot()
        bot.config = {"dashboard_enabled": True, "dashboard_port": 8765, **config}
        return DashboardServer(
            bot,
            save_config=lambda _cfg: None,
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

    async def test_the_public_name_must_be_allowlisted_and_then_works(self) -> None:
        server = self.make_server()
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            blocked = await client.get("/", headers={"Host": "myhome.duckdns.org"})
            self.assertEqual(blocked.status, 400)

        server = self.make_server(dashboard_allowed_hosts=["myhome.duckdns.org"])
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            allowed = await client.get("/", headers={"Host": "myhome.duckdns.org"})
            self.assertEqual(allowed.status, 200)

    async def test_the_public_url_is_allowlisted_implicitly(self) -> None:
        # One setting instead of two: the host of dashboard_public_url is the
        # host users actually type, so it is allowed without repeating it.
        server = self.make_server(dashboard_public_url="https://myhome.duckdns.org")
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            response = await client.get("/", headers={"Host": "myhome.duckdns.org"})
            self.assertEqual(response.status, 200)

    async def test_a_configured_allowlist_replaces_rather_than_extends_localhost(self) -> None:
        server = self.make_server(dashboard_allowed_hosts=["myhome.duckdns.org"])
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            # localhost still works: it cannot be forged by a hostile page.
            self.assertEqual((await client.get("/")).status, 200)
            self.assertEqual(
                (await client.get("/", headers={"Host": "attacker.example"})).status, 400
            )

    async def test_urls_put_the_public_address_first(self) -> None:
        server = self.make_server(dashboard_public_url="https://myhome.duckdns.org")
        server._apply_settings(server.bot.config)
        self.assertEqual(server.urls()[0], "https://myhome.duckdns.org/")
        self.assertIn("http://127.0.0.1:8765/", server.urls())
        self.assertEqual(server.scheme, "http")
        self.assertEqual(server.browser_scheme, "https")

    def test_tls_settings_are_validated_before_binding(self) -> None:
        server = self.make_server(dashboard_tls_cert="/tmp/only-cert.pem")
        with self.assertRaises(ValueError) as ctx:
            server._apply_settings(server.bot.config)
        self.assertIn("dashboard_tls_key", str(ctx.exception))

        server = self.make_server(
            dashboard_tls_cert="/tmp/only-cert.pem", dashboard_tls_key="/tmp/only-key.pem"
        )
        with self.assertRaises(ValueError) as ctx:
            server._apply_settings(server.bot.config)
        message = str(ctx.exception)
        self.assertIn("dashboard_tls_cert", message)

    def test_a_bad_certificate_fails_loudly(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            cert.write_text("not a certificate", encoding="utf-8")
            key.write_text("not a key", encoding="utf-8")
            server = self.make_server(dashboard_tls_cert=str(cert), dashboard_tls_key=str(key))
            # Fail at startup with the file name, rather than serving a
            # certificate no browser will trust.
            with self.assertRaises(ValueError) as ctx:
                server._apply_settings(server.bot.config)
            self.assertIn("cert", str(ctx.exception).lower())

    async def test_a_tls_listener_serves_https_and_forces_a_secure_cookie(self) -> None:
        cert, key = _self_signed_certificate(self)
        server = self.make_server(dashboard_tls_cert=str(cert), dashboard_tls_key=str(key))
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            response = await client.post("/api/login", json={"token": server._token})
            self.assertEqual(response.status, 200)
            cookie = response.cookies.get("sentinel_dashboard_session")
            self.assertTrue(cookie["secure"], "the session cookie must not travel over HTTP")
        self.assertEqual(server.scheme, "https")
        self.assertEqual(server.browser_scheme, "https")

    async def test_forwarded_for_is_ignored_from_an_untrusted_peer(self) -> None:
        server = self.make_server(dashboard_trusted_proxies=["10.0.0.1"])
        server._apply_settings(server.bot.config)
        request = SimpleNamespace(remote="127.0.0.1", headers={"X-Forwarded-For": "198.51.100.9"})
        self.assertEqual(server._client_ip(request), "127.0.0.1")

    async def test_forwarded_for_is_believed_from_a_trusted_proxy(self) -> None:
        server = self.make_server(dashboard_trusted_proxies=["127.0.0.1"])
        server._apply_settings(server.bot.config)
        request = SimpleNamespace(remote="127.0.0.1", headers={"X-Forwarded-For": "198.51.100.9"})
        self.assertEqual(server._client_ip(request), "198.51.100.9")

        # Behind two trusted hops, walk past our own proxies to the client.
        server = self.make_server(dashboard_trusted_proxies=["127.0.0.1", "10.0.0.0/8"])
        server._apply_settings(server.bot.config)
        request = SimpleNamespace(
            remote="127.0.0.1", headers={"X-Forwarded-For": "198.51.100.9, 10.1.2.3"}
        )
        self.assertEqual(server._client_ip(request), "198.51.100.9")

    async def test_the_login_throttle_sees_the_client_not_the_proxy(self) -> None:
        # Without trusting the proxy every request looks like it came from the
        # proxy's address, so five bad guesses by a stranger would lock the
        # whole internet out of the dashboard.
        server = self.make_server(dashboard_trusted_proxies=["127.0.0.1"])
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            for _ in range(6):
                response = await client.post(
                    "/api/login",
                    json={"token": "wrong"},
                    headers={"X-Forwarded-For": "198.51.100.9"},
                )
            self.assertEqual(response.status, 429, "the abusive client is throttled")

            other = await client.post(
                "/api/login",
                json={"token": server._token},
                headers={"X-Forwarded-For": "198.51.100.10"},
            )
            self.assertEqual(other.status, 200, "an innocent client must not be locked out")

    async def test_an_attacker_cannot_invent_a_client_address(self) -> None:
        # With no trusted proxy configured, X-Forwarded-For is just a header a
        # stranger can write, so it must not let them dodge the throttle.
        server = self.make_server()
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            for index in range(6):
                response = await client.post(
                    "/api/login",
                    json={"token": "wrong"},
                    headers={"X-Forwarded-For": f"203.0.113.{index}"},
                )
            self.assertEqual(response.status, 429)

    def test_remote_access_warnings_name_the_fix(self) -> None:
        # The dashboard knows the listener's shape; the DuckDNS module knows the
        # public name. `bot.py --dashboard` prints both lists, and between them
        # a user has to be told each of the three things that must change.
        import duckdns

        config = {"duckdns_domain": "myhome", "duckdns_token": "t"}
        server = self.make_server(**config)
        server._apply_settings(server.bot.config)
        text = "\n".join(list(server.remote_access_warnings()) + duckdns.dashboard_warnings(server.bot.config))
        self.assertIn("dashboard_allowed_hosts", text)
        self.assertIn("loopback", text)
        self.assertIn("dashboard_public_url", text)

    def test_an_exposed_listener_without_an_allowlist_is_called_out(self) -> None:
        server = self.make_server(dashboard_host="0.0.0.0")
        server._apply_settings(server.bot.config)
        text = "\n".join(server.remote_access_warnings())
        self.assertIn("dashboard_allowed_hosts", text)
        self.assertIn("clear text", text)


    def test_no_warnings_once_the_remote_setup_is_complete(self) -> None:
        # The proxy-terminated shape: plain HTTP on loopback (only the proxy can
        # reach it), HTTPS in the URL users open, and a secure cookie.
        server = self.make_server(
            dashboard_public_url="https://myhome.duckdns.org",
            dashboard_secure_cookie=True,
        )
        server._apply_settings(server.bot.config)
        self.assertEqual(server.remote_access_warnings(), [])
        self.assertEqual(server.browser_scheme, "https")
        self.assertTrue(server._secure_cookie)


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


class _FakeTextChannel:
    """Stands in for discord.TextChannel (patched in during the test)."""

    def __init__(
        self,
        guild,
        channel_id: int = 555,
        name: str = "rules",
        *,
        next_message_id: int = 777,
    ) -> None:
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.mention = f"#{name}"
        self.sent: list[dict] = []
        self.reactions: list[str] = []
        self.messages: dict[int, _FakeMessage] = {}
        self._next_id = next_message_id

    def permissions_for(self, _member):
        return SimpleNamespace(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            add_reactions=True,
        )

    async def send(self, *, content=None, embed=None, allowed_mentions=None):
        message = _FakeMessage(self, self._next_id)
        self._next_id += 1
        self.messages[message.id] = message
        self.sent.append({"content": content, "embed": embed, "message": message})
        return message

    async def fetch_message(self, message_id):
        message = self.messages.get(int(message_id))
        if message is None:
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"), "Unknown Message"
            )
        return message


class _FakeMessage:
    def __init__(self, channel, message_id: int = 777) -> None:
        self.id = message_id
        self.channel = channel
        self.author = SimpleNamespace(id=OWNER_ID)
        self.deleted = False
        self.edits: list[dict] = []
        self.reactions: list[str] = []

    async def add_reaction(self, emoji) -> None:
        self.reactions.append(str(emoji))
        self.channel.reactions.append(str(emoji))

    async def remove_reaction(self, emoji, member) -> None:
        self.reactions = [item for item in self.reactions if item != str(emoji)]

    async def edit(self, *, content=None, embed=None, allowed_mentions=None):
        self.edits.append({"content": content, "embed": embed})
        return self

    async def delete(self) -> None:
        self.deleted = True


class _PublishFakeGuild:
    id = GUILD_ID
    name = "Test Guild"

    def __init__(self, channel) -> None:
        self._channel = channel
        self._channels = {channel.id: channel}
        # _ReactionRole/_ReactionPerms below are discord-like enough for the
        # shared validators in rules.py (is_default, managed, positions).
        self._role = _ReactionRole(888, name="Verified")
        self._roles = {self._role.id: self._role}
        self.me = SimpleNamespace(
            guild_permissions=_ReactionPerms(manage_roles=True),
            top_role=_ReactionRole(1, name="bot", position=50),
        )

    def get_channel(self, channel_id):
        return self._channels.get(int(channel_id))

    def get_role(self, role_id):
        return self._roles.get(int(role_id))


def _publish_bot_stub():
    """A bot object good enough for RulesMixin._mark_post_disabled."""
    return SimpleNamespace(user=SimpleNamespace(id=OWNER_ID))


class _DashboardRulesMixin(RulesMixin):
    """The rules half of the real cog, wired to this test's fakes.

    The dashboard finds the helpers by looking for a RulesMixin among the
    bot's cogs (they belong to the cog that owns /manage), so the test has to
    provide a real one rather than a stand-in namespace.
    """

    def __init__(self, bot) -> None:
        super().__init__(bot, lambda _config: None)

    @staticmethod
    def _validate_role(guild, channel, role):
        return validate_self_assignable_role(guild, role) or validate_post_channel(
            guild, channel
        )

    async def _mark_post_disabled(self, guild, settings):
        # Same work, against a stub bot: this test cares about what is posted,
        # not about the reaction cleanup (see tests/test_rules.py).
        mixin = RulesMixin(_publish_bot_stub(), lambda _config: None)
        await mixin._mark_post_disabled(guild, settings)


class _PublishFakeBot:
    def __init__(self, guild) -> None:
        self.config = {"guilds": {str(GUILD_ID): {}}}
        self.user = None
        self._guild = guild
        self._cogs = {"SentinelCog": _DashboardRulesMixin(self)}

    @property
    def cogs(self):
        return self._cogs

    def get_guild(self, guild_id):
        return self._guild if int(guild_id) == GUILD_ID else None

    def is_ready(self):
        return True

    def get_cog(self, name):
        return self._cogs.get(name)


class DashboardPublishRulesTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard's publish button posts the same prompt as /rules publish."""

    async def test_publish_posts_the_prompt_and_embed(self) -> None:
        channel = _FakeTextChannel(guild=None)
        guild = _PublishFakeGuild(channel)
        channel.guild = guild
        saved: list[dict] = []
        server = DashboardServer(
            _PublishFakeBot(guild),
            save_config=saved.append,
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
        server._token = "T" * 48

        with patch.object(discord, "TextChannel", _FakeTextChannel):
            async with TestClient(TestServer(server._build_app())) as client:
                login = await client.post("/api/login", json={"token": server._token})
                csrf = (await login.json())["csrfToken"]
                response = await client.put(
                    f"/api/guilds/{GUILD_ID}/rules",
                    json={
                        "name": "Server rules",
                        "channelId": str(channel.id),
                        "roleId": "888",
                        "text": "Be kind.",
                    },
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status, 201)
                payload = await response.json()

        # Message ids are snowflakes: they must cross the wire as strings.
        self.assertEqual(payload["messageId"], "777")
        self.assertEqual(len(channel.sent), 1)
        posted = channel.sent[0]
        from rules import RULES_POST_CONTENT

        self.assertEqual(posted["content"], RULES_POST_CONTENT)
        # The post named "Server rules" is a custom set now, so its title is
        # the name as typed (only the default, "Zone rules", gets "Guild Rules").
        self.assertEqual(posted["embed"].title, "Test Guild — Server rules")
        self.assertEqual(posted["embed"].description, "Be kind.")
        self.assertEqual(channel.reactions, ["✅"])
        stored = saved[-1]["rules"][str(GUILD_ID)]
        self.assertEqual(stored[0]["message_id"], 777)
        self.assertEqual(stored[0]["name"], "Server rules")
        self.assertEqual(payload["rulesetId"], stored[0]["ruleset_id"])


class _ReactionPerms:
    """Answers False for every permission that was not explicitly granted."""

    def __init__(self, **granted) -> None:
        self.__dict__.update(granted)

    def __getattr__(self, _name):
        return False


class _ReactionRole:
    """A role that can be compared by position, like discord.Role."""

    def __init__(self, role_id, *, name="Gaming", position=10, privileged=False) -> None:
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = False
        self.permissions = _ReactionPerms(ban_members=True) if privileged else _ReactionPerms()

    def is_default(self) -> bool:
        return False

    def __ge__(self, other): return self.position >= other.position

    def __gt__(self, other): return self.position > other.position

    def __lt__(self, other): return self.position < other.position

    def __le__(self, other): return self.position <= other.position


class _FakeReactionMessage:
    def __init__(self, channel, message_id: int) -> None:
        self.id = message_id
        self.channel = channel
        self.author = SimpleNamespace(id=OWNER_ID)
        self.deleted = False
        self.edits: list[dict] = []
        self.reactions: list[SimpleNamespace] = []

    async def add_reaction(self, emoji) -> None:
        self.reactions.append(SimpleNamespace(emoji=str(emoji), me=True))
        self.channel.reactions.append(str(emoji))

    async def remove_reaction(self, emoji, member) -> None:
        self.reactions = [item for item in self.reactions if str(item.emoji) != str(emoji)]

    async def edit(self, *, content=None, embed=None, allowed_mentions=None):
        self.edits.append({"content": content, "embed": embed})
        return self

    async def delete(self) -> None:
        self.deleted = True


class _FakeReactionChannel:
    """Stands in for discord.TextChannel (patched in during the tests)."""

    def __init__(
        self, guild, channel_id: int = 555, name: str = "roles", next_message_id: int = 777
    ) -> None:
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.mention = f"#{name}"
        self.sent: list[dict] = []
        self.reactions: list[str] = []
        self.messages: dict[int, _FakeReactionMessage] = {}
        self._next_id = next_message_id
        self.permissions = None

    def permissions_for(self, _member):
        if self.permissions is not None:
            return self.permissions
        return SimpleNamespace(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            add_reactions=True,
        )

    async def send(self, *, content=None, embed=None, allowed_mentions=None):
        message = _FakeReactionMessage(self, self._next_id)
        self._next_id += 1
        self.messages[message.id] = message
        self.sent.append({"content": content, "embed": embed, "message": message})
        return message

    async def fetch_message(self, message_id):
        message = self.messages.get(int(message_id))
        if message is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        return message


class _ReactionFakeGuild:
    id = GUILD_ID
    name = "Test Guild"

    def __init__(self, *channels, roles=(), member=None) -> None:
        self._channels = {channel.id: channel for channel in channels}
        self._roles = {role.id: role for role in roles}
        self._member = member
        self.me = SimpleNamespace(
            guild_permissions=_ReactionPerms(manage_roles=True),
            top_role=_ReactionRole(1, name="bot", position=50),
        )

    def get_channel(self, channel_id):
        return self._channels.get(int(channel_id))

    def get_role(self, role_id):
        return self._roles.get(int(role_id))

    def get_member(self, user_id):
        return self._member


class _ReactionFakeBot:
    def __init__(self, guild, config=None) -> None:
        self.config = config if config is not None else {"guilds": {str(GUILD_ID): {}}}
        self.user = SimpleNamespace(id=OWNER_ID)
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if int(guild_id) == GUILD_ID else None

    def is_ready(self):
        return True


class DashboardRuleSetsTests(unittest.IsolatedAsyncioTestCase):
    """Multiple rule sets per server, driven from the dashboard.

    The endpoint contract mirrors the reaction-role menus: PUT creates, POST
    edits in place, DELETE disables one set by id (and only one).
    """

    def setUp(self) -> None:
        self.channel = _FakeTextChannel(guild=None)
        # A distinct id range: Discord message ids are unique across channels,
        # so a repost must be able to tell "same message" from "new message".
        self.other_channel = _FakeTextChannel(
            guild=None, channel_id=556, name="events", next_message_id=880
        )
        self.guild = _PublishFakeGuild(self.channel)
        self.channel.guild = self.guild
        self.other_channel.guild = self.guild
        self.guild._channels = {self.channel.id: self.channel, self.other_channel.id: self.other_channel}
        self.bot = _PublishFakeBot(self.guild)
        self.saved: list[dict] = []
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

    @property
    def _sets(self) -> list:
        return self.bot.config.get("rules", {}).get(str(GUILD_ID), [])

    def _body(self, **overrides):
        body = {
            "name": "Server rules",
            "channelId": str(self.channel.id),
            "roleId": "888",
            "text": "Be kind.",
        }
        body.update(overrides)
        return body

    async def test_publish_edit_list_and_disable_one_set(self) -> None:
        with patch.object(discord, "TextChannel", _FakeTextChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                first = await client.put(
                    f"/api/guilds/{GUILD_ID}/rules",
                    json=self._body(),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(first.status, 201)
                first_id = (await first.json())["rulesetId"]

                second = await client.put(
                    f"/api/guilds/{GUILD_ID}/rules",
                    json=self._body(name="Event rules", channelId=str(self.other_channel.id), text="No spam."),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(second.status, 201, await second.text())
                second_id = (await second.json())["rulesetId"]
                self.assertNotEqual(first_id, second_id)
                self.assertEqual(len(self.channel.sent), 1, "the first post must survive")

                listed = await client.get(f"/api/guilds/{GUILD_ID}/rules")
                body = await listed.json()
                self.assertEqual([item["name"] for item in body["rulesets"]], ["Server rules", "Event rules"])
                self.assertEqual([item["roleName"] for item in body["rulesets"]], ["Verified", "Verified"])
                self.assertEqual(body["rulesets"][0]["channelId"], str(self.channel.id))
                self.assertEqual(body["defaultName"], "Zone rules")
                self.assertEqual(body["maxRulesets"], 25)
                self.assertTrue(body["prompt"])

                updated = await client.post(
                    f"/api/guilds/{GUILD_ID}/rules/{second_id}",
                    json=self._body(name="Event rules", channelId=str(self.other_channel.id), text="Be excellent."),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(updated.status, 200, await updated.text())
                self.assertEqual(len(self.other_channel.sent), 1, "the message is edited, not reposted")
                self.assertEqual(self.other_channel.sent[0]["message"].edits[-1]["embed"].description, "Be excellent.")
                self.assertEqual(self._sets[1]["rules_text"], "Be excellent.")

                removed = await client.delete(
                    f"/api/guilds/{GUILD_ID}/rules/{second_id}",
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(removed.status, 200)
                self.assertEqual([item["name"] for item in self._sets], ["Server rules"])

        # A set named "Server rules" keeps that name: it is a custom name now,
        # so it gets its own title instead of the default set's short one.
        self.assertEqual(
            self.channel.sent[0]["embed"].title, "Test Guild — Server rules"
        )

    async def test_naming_a_set_server_rules_is_not_auto_corrected(self) -> None:
        """The regression: a typed title must survive untouched.

        Publishing (or renaming to) "Server Rules" must store and list exactly
        that, title the embed with it, and leave the default name alone — the
        title must never be rewritten to the default ("Zone rules").
        """
        with patch.object(discord, "TextChannel", _FakeTextChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                created = await client.put(
                    f"/api/guilds/{GUILD_ID}/rules",
                    json=self._body(name="Server Rules"),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(created.status, 201, await created.text())
                ruleset_id = (await created.json())["rulesetId"]

                listed = await client.get(f"/api/guilds/{GUILD_ID}/rules")
                body = await listed.json()

                # Renaming it back to itself keeps it, and the default stays
                # "Zone rules" for sets that have no name of their own.
                renamed = await client.post(
                    f"/api/guilds/{GUILD_ID}/rules/{ruleset_id}",
                    json=self._body(name="Server Rules", text="Be kind, still."),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(renamed.status, 200, await renamed.text())

        self.assertEqual(self._sets[0]["name"], "Server Rules", "stored verbatim")
        self.assertEqual([item["name"] for item in body["rulesets"]], ["Server Rules"])
        self.assertEqual(body["defaultName"], "Zone rules")
        self.assertEqual(
            self.channel.sent[0]["embed"].title, "Test Guild — Server Rules"
        )

    async def test_edit_reposts_when_the_channel_changes(self) -> None:
        with patch.object(discord, "TextChannel", _FakeTextChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                created = await client.put(
                    f"/api/guilds/{GUILD_ID}/rules", json=self._body(), headers={"X-CSRF-Token": csrf}
                )
                ruleset_id = (await created.json())["rulesetId"]
                old_message = self.channel.sent[0]["message"]

                moved = await client.post(
                    f"/api/guilds/{GUILD_ID}/rules/{ruleset_id}",
                    json=self._body(channelId=str(self.other_channel.id)),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(moved.status, 200, await moved.text())
                self.assertEqual((await moved.json())["messageId"], "880")

        self.assertTrue(old_message.deleted)
        self.assertEqual(len(self.other_channel.sent), 1)
        self.assertEqual(self.other_channel.reactions, ["✅"])
        self.assertEqual(self._sets[0]["message_id"], 880)

    async def test_disabling_without_an_id_is_refused_when_there_are_several(self) -> None:
        with patch.object(discord, "TextChannel", _FakeTextChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                await client.put(f"/api/guilds/{GUILD_ID}/rules", json=self._body(), headers={"X-CSRF-Token": csrf})
                await client.put(
                    f"/api/guilds/{GUILD_ID}/rules",
                    json=self._body(name="Event rules", channelId=str(self.other_channel.id)),
                    headers={"X-CSRF-Token": csrf},
                )
                response = await client.delete(
                    f"/api/guilds/{GUILD_ID}/rules", headers={"X-CSRF-Token": csrf}
                )
                self.assertEqual(response.status, 400)
                self.assertIn("several rule sets", (await response.json())["error"])
                self.assertEqual(len(self._sets), 2)
                unknown = await client.delete(
                    f"/api/guilds/{GUILD_ID}/rules/nope", headers={"X-CSRF-Token": csrf}
                )
                self.assertEqual(unknown.status, 404)

    async def test_duplicate_names_and_missing_sets_are_rejected(self) -> None:
        with patch.object(discord, "TextChannel", _FakeTextChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                created = await client.put(
                    f"/api/guilds/{GUILD_ID}/rules", json=self._body(), headers={"X-CSRF-Token": csrf}
                )
                ruleset_id = (await created.json())["rulesetId"]

                duplicate = await client.put(
                    f"/api/guilds/{GUILD_ID}/rules",
                    json=self._body(name="server RULES"),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(duplicate.status, 400)
                self.assertIn("already named", (await duplicate.json())["error"])

                too_long = await client.put(
                    f"/api/guilds/{GUILD_ID}/rules",
                    json=self._body(name="x" * 81),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(too_long.status, 400)
                self.assertIn("80", (await too_long.json())["error"])

                # Editing a set keeps its own name valid (case-insensitively).
                renamed = await client.post(
                    f"/api/guilds/{GUILD_ID}/rules/{ruleset_id}",
                    json=self._body(name="server rules", text="Still kind."),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(renamed.status, 200, await renamed.text())
                self.assertEqual(self._sets[0]["name"], "server rules")

                missing = await client.post(
                    f"/api/guilds/{GUILD_ID}/rules/nope",
                    json=self._body(),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(missing.status, 404)

    async def test_a_full_server_is_refused(self) -> None:
        self.bot.config = {
            "rules": {
                str(GUILD_ID): [
                    {"ruleset_id": f"id{index}", "name": f"Set {index}", "message_id": index}
                    for index in range(25)
                ]
            }
        }
        with patch.object(discord, "TextChannel", _FakeTextChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                response = await client.put(
                    f"/api/guilds/{GUILD_ID}/rules", json=self._body(), headers={"X-CSRF-Token": csrf}
                )
                self.assertEqual(response.status, 400)
                self.assertIn("25", (await response.json())["error"])
        self.assertEqual(self.channel.sent, [])


class DashboardReactionRolesTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard's reaction-role menu endpoints.

    Publishing posts a bot message, attaches every emoji, and stores the
    emoji → role mapping; editing re-syncs the reactions; removing deletes the
    message. tests/test_reaction_roles.py covers what the bot then does with
    the stored mappings when a member reacts.
    """

    def setUp(self) -> None:
        self.channel = _FakeReactionChannel(guild=None)
        self.other_channel = _FakeReactionChannel(
            guild=None, channel_id=556, name="pings", next_message_id=880
        )
        self.role = _ReactionRole(888)
        self.guild = _ReactionFakeGuild(
            self.channel, self.other_channel, roles=[self.role]
        )
        self.channel.guild = self.guild
        self.other_channel.guild = self.guild
        self.bot = _ReactionFakeBot(self.guild)
        self.saved: list[dict] = []
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

    @property
    def _stored_posts(self) -> list:
        return self.bot.config.get("reaction_roles", {}).get(str(GUILD_ID), [])

    def _post_body(self, **overrides):
        body = {
            "channelId": str(self.channel.id),
            "useEmbed": True,
            "title": "Choose your roles",
            "message": "React below to pick your pings.",
            "removeOnUnreact": True,
            "entries": [{"emoji": "🎮", "roleId": str(self.role.id)}],
        }
        body.update(overrides)
        return body

    async def test_publish_posts_the_message_and_stores_the_pairs(self) -> None:
        with patch.object(discord, "TextChannel", _FakeReactionChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                response = await client.put(
                    f"/api/guilds/{GUILD_ID}/reaction-roles",
                    json=self._post_body(
                        entries=[
                            {"emoji": "🎮", "roleId": str(self.role.id)},
                            {"emoji": "gaming:123456789", "roleId": str(self.role.id)},
                        ]
                    ),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status, 201)
                payload = await response.json()

                listed = await client.get(f"/api/guilds/{GUILD_ID}/reaction-roles")
                self.assertEqual(listed.status, 200)
                listing = await listed.json()

        # Ids cross the wire as strings, or the browser rounds them.
        self.assertEqual(payload["messageId"], "777")
        self.assertTrue(payload["postId"])
        self.assertEqual(len(self.channel.sent), 1)
        posted = self.channel.sent[0]
        self.assertIsNone(posted["content"])
        self.assertEqual(posted["embed"].title, "Choose your roles")
        self.assertEqual(posted["embed"].description, "React below to pick your pings.")
        self.assertEqual(self.channel.reactions, ["🎮", "<:gaming:123456789>"])

        stored = self._stored_posts[0]
        self.assertEqual(stored["message_id"], 777)
        self.assertEqual(stored["channel_id"], self.channel.id)
        self.assertEqual(
            stored["entries"][0],
            {"emoji": "🎮", "role_id": self.role.id, "action": "add"},
        )
        self.assertEqual(stored["entries"][1]["emoji"], "<:gaming:123456789>")
        self.assertEqual(self.saved[-1]["reaction_roles"][str(GUILD_ID)][0]["post_id"], payload["postId"])

        self.assertEqual(listing["maxEntries"], 20)
        self.assertTrue(listing["defaultMessage"])
        self.assertEqual(len(listing["posts"]), 1)
        self.assertEqual(listing["posts"][0]["entries"][0]["roleId"], str(self.role.id))
        self.assertEqual(listing["posts"][0]["channelId"], str(self.channel.id))
        self.assertTrue(listing["posts"][0]["useEmbed"])

    async def test_pairs_can_give_or_remove_a_role(self) -> None:
        with patch.object(discord, "TextChannel", _FakeReactionChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                response = await client.put(
                    f"/api/guilds/{GUILD_ID}/reaction-roles",
                    json=self._post_body(
                        entries=[
                            {"emoji": "🎮", "roleId": str(self.role.id), "action": "add"},
                            {"emoji": "🔕", "roleId": str(self.role.id), "action": "remove"},
                        ]
                    ),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status, 201)
                listed = await client.get(f"/api/guilds/{GUILD_ID}/reaction-roles")
                listing = await listed.json()

        self.assertEqual(self.channel.reactions, ["🎮", "🔕"])
        self.assertEqual(
            self._stored_posts[0]["entries"],
            [
                {"emoji": "🎮", "role_id": self.role.id, "action": "add"},
                {"emoji": "🔕", "role_id": self.role.id, "action": "remove"},
            ],
        )
        self.assertEqual(
            [entry["action"] for entry in listing["posts"][0]["entries"]],
            ["add", "remove"],
        )

    async def test_a_bad_action_is_rejected_before_anything_is_posted(self) -> None:
        with patch.object(discord, "TextChannel", _FakeReactionChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                response = await client.put(
                    f"/api/guilds/{GUILD_ID}/reaction-roles",
                    json=self._post_body(
                        entries=[
                            {
                                "emoji": "🎮",
                                "roleId": str(self.role.id),
                                "action": "strip",
                            }
                        ]
                    ),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status, 400)
                self.assertIn("strip", await response.text())

        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self._stored_posts, [])

    def test_a_stored_entry_without_an_action_is_reported_as_give(self) -> None:
        # Posts saved before the Give/Remove option existed.
        payload = _reaction_post_payload(
            {"post_id": "abc123", "entries": [{"emoji": "🎮", "role_id": 89}]}
        )
        self.assertEqual(payload["entries"][0]["action"], "add")

    async def test_plain_posts_send_the_message_without_an_embed(self) -> None:
        with patch.object(discord, "TextChannel", _FakeReactionChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                response = await client.put(
                    f"/api/guilds/{GUILD_ID}/reaction-roles",
                    json=self._post_body(useEmbed=False, message="Pick a role."),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status, 201)

        posted = self.channel.sent[0]
        self.assertEqual(posted["content"], "Pick a role.")
        self.assertIsNone(posted["embed"])

    async def test_empty_message_falls_back_to_the_default_prompt(self) -> None:
        with patch.object(discord, "TextChannel", _FakeReactionChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                await client.put(
                    f"/api/guilds/{GUILD_ID}/reaction-roles",
                    json=self._post_body(message="   "),
                    headers={"X-CSRF-Token": csrf},
                )

        from reaction_roles import DEFAULT_POST_MESSAGE

        self.assertEqual(self.channel.sent[0]["embed"].description, DEFAULT_POST_MESSAGE)
        self.assertEqual(self._stored_posts[0]["message"], DEFAULT_POST_MESSAGE)

    async def test_update_edits_the_message_and_syncs_reactions(self) -> None:
        with patch.object(discord, "TextChannel", _FakeReactionChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                created = await client.put(
                    f"/api/guilds/{GUILD_ID}/reaction-roles",
                    json=self._post_body(),
                    headers={"X-CSRF-Token": csrf},
                )
                post_id = (await created.json())["postId"]

                response = await client.post(
                    f"/api/guilds/{GUILD_ID}/reaction-roles/{post_id}",
                    json=self._post_body(
                        title="Pick a ping",
                        message="Updated text.",
                        entries=[{"emoji": "🎬", "roleId": str(self.role.id)}],
                    ),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["messageId"], "777")

        message = self.channel.messages[777]
        self.assertEqual(len(message.edits), 1)
        self.assertEqual(message.edits[-1]["embed"].title, "Pick a ping")
        self.assertEqual(message.edits[-1]["embed"].description, "Updated text.")
        # The stale 🎮 reaction is dropped and the new 🎬 reaction is added.
        self.assertEqual([str(item.emoji) for item in message.reactions], ["🎬"])
        self.assertEqual(
            self._stored_posts[0]["entries"],
            [{"emoji": "🎬", "role_id": self.role.id, "action": "add"}],
        )
        self.assertEqual(len(self._stored_posts), 1, "an edit must not add a second post")

    async def test_update_moves_a_post_to_another_channel(self) -> None:
        with patch.object(discord, "TextChannel", _FakeReactionChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                created = await client.put(
                    f"/api/guilds/{GUILD_ID}/reaction-roles",
                    json=self._post_body(),
                    headers={"X-CSRF-Token": csrf},
                )
                post_id = (await created.json())["postId"]
                old_message = self.channel.messages[777]

                response = await client.post(
                    f"/api/guilds/{GUILD_ID}/reaction-roles/{post_id}",
                    json=self._post_body(channelId=str(self.other_channel.id)),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status, 200)

        self.assertTrue(old_message.deleted)
        self.assertEqual(len(self.other_channel.sent), 1)
        self.assertEqual(self.other_channel.reactions, ["🎮"])
        self.assertEqual(self._stored_posts[0]["channel_id"], self.other_channel.id)
        self.assertEqual(self._stored_posts[0]["message_id"], 880)

    async def test_delete_removes_the_config_and_the_message(self) -> None:
        with patch.object(discord, "TextChannel", _FakeReactionChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                created = await client.put(
                    f"/api/guilds/{GUILD_ID}/reaction-roles",
                    json=self._post_body(),
                    headers={"X-CSRF-Token": csrf},
                )
                post_id = (await created.json())["postId"]
                response = await client.delete(
                    f"/api/guilds/{GUILD_ID}/reaction-roles/{post_id}?deleteMessage=true",
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status, 200)
                self.assertTrue((await response.json())["messageDeleted"])

                listed = await client.get(f"/api/guilds/{GUILD_ID}/reaction-roles")
                self.assertEqual((await listed.json())["posts"], [])

        self.assertTrue(self.channel.messages[777].deleted)
        self.assertNotIn(str(GUILD_ID), self.bot.config["reaction_roles"])

    async def test_preview_uses_the_default_prompt_and_escapes_html(self) -> None:
        from reaction_roles import DEFAULT_POST_MESSAGE

        url = f"/api/guilds/{GUILD_ID}/reaction-roles/preview"
        async with TestClient(TestServer(self.server._build_app())) as client:
            csrf = await self._login(client)
            empty = await client.post(url, json={"message": ""}, headers={"X-CSRF-Token": csrf})
            self.assertEqual(empty.status, 200)
            body = await empty.json()
            self.assertIn(DEFAULT_POST_MESSAGE, body["html"])
            self.assertEqual(body["length"], len(DEFAULT_POST_MESSAGE))

            escaped = await client.post(
                url,
                json={"message": "<img src=x onerror=alert(1)>", "useEmbed": False},
                headers={"X-CSRF-Token": csrf},
            )
            escaped_html = (await escaped.json())["html"]
            self.assertNotIn("<img", escaped_html)
            self.assertIn("&lt;img", escaped_html)

            overlong = await client.post(
                url, json={"message": "x" * 2001, "useEmbed": False}, headers={"X-CSRF-Token": csrf}
            )
            self.assertEqual(overlong.status, 400)
            self.assertIn("2000", (await overlong.json())["error"])

        # A preview must never post anything.
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.saved, [])

    async def test_publish_rejects_bad_pairs_and_roles(self) -> None:
        url = f"/api/guilds/{GUILD_ID}/reaction-roles"
        cases = {
            "no pairs": {"entries": []},
            "emoji is text": {"entries": [{"emoji": "gaming", "roleId": "888"}]},
            "duplicate emoji": {
                "entries": [
                    {"emoji": "🎮", "roleId": "888"},
                    {"emoji": "🎮", "roleId": "888"},
                ]
            },
            "too many pairs": {
                "entries": [
                    {"emoji": chr(0x1F600 + index), "roleId": "888"} for index in range(21)
                ]
            },
            "unknown role": {"entries": [{"emoji": "🎮", "roleId": "404"}]},
            "privileged role": {"entries": [{"emoji": "🎮", "roleId": "889"}]},
            "unknown channel": {"channelId": "999"},
            "plain message too long": {"useEmbed": False, "message": "x" * 2001},
        }
        self.guild._roles[889] = _ReactionRole(889, name="Admin", privileged=True)
        with patch.object(discord, "TextChannel", _FakeReactionChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                for label, overrides in cases.items():
                    with self.subTest(case=label):
                        response = await client.put(
                            url,
                            json=self._post_body(**overrides),
                            headers={"X-CSRF-Token": csrf},
                        )
                        self.assertEqual(response.status, 400, await response.text())
        self.assertEqual(self.channel.sent, [], "nothing may be posted when validation fails")
        self.assertEqual(self.saved, [])

    async def test_publish_requires_channel_permissions(self) -> None:
        locked = _FakeReactionChannel(guild=self.guild, channel_id=557, name="locked")
        locked.permissions = SimpleNamespace(
            view_channel=True, send_messages=True, embed_links=True, add_reactions=False
        )
        self.guild._channels[557] = locked
        with patch.object(discord, "TextChannel", _FakeReactionChannel):
            async with TestClient(TestServer(self.server._build_app())) as client:
                csrf = await self._login(client)
                response = await client.put(
                    f"/api/guilds/{GUILD_ID}/reaction-roles",
                    json=self._post_body(channelId="557"),
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status, 400)
                self.assertIn("add reactions", (await response.json())["error"])
        self.assertEqual(locked.sent, [])
        self.assertEqual(self.saved, [])

    async def test_reaction_role_endpoints_need_a_session_and_csrf(self) -> None:
        url = f"/api/guilds/{GUILD_ID}/reaction-roles"
        async with TestClient(TestServer(self.server._build_app())) as client:
            self.assertEqual((await client.get(url)).status, 401)
            self.assertEqual((await client.put(url, json=self._post_body())).status, 401)
            csrf = await self._login(client)
            self.assertEqual((await client.put(url, json=self._post_body())).status, 403)
            missing = await client.post(
                f"{url}/nope",
                json=self._post_body(),
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(missing.status, 404)
            unknown = await client.get("/api/guilds/999/reaction-roles")
            self.assertEqual(unknown.status, 404)


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

    def test_reaction_role_editor_is_wired_up(self) -> None:
        for element in (
            'id="reaction-form"',
            'id="reaction-channel"',
            'id="reaction-style"',
            'id="reaction-title"',
            'id="reaction-message"',
            'id="reaction-entries"',
            'id="reaction-add-entry"',
            'id="reaction-insert-legend"',
            'id="reaction-remove-on-unreact"',
            'id="reaction-preview"',
            'id="reaction-preview-legend"',
            'id="reaction-posts"',
            'id="reaction-submit"',
        ):
            with self.subTest(element=element):
                self.assertIn(element, self.html)

    def test_toolbar_buttons_resolve_to_a_declared_editor(self) -> None:
        """Both Markdown editors dispatch through the script's `editors` map.

        A button whose data-editor name does not match a key there (or a
        mistyped textarea id inside a key) would silently do nothing, which is
        exactly the kind of breakage no server-side test can see.
        """
        targets = set(re.findall(r'data-editor="([^"]+)"', self.html))
        self.assertEqual(targets, {"rules", "reaction"})
        block = self.script.split("const editors = {", 1)[1].split("\n      };", 1)[0]
        declared = set(re.findall(r"^\s{8}(\w+): \{", block, re.M))
        self.assertEqual(declared, targets)
        markup_ids = set(re.findall(r'id="([^"]+)"', self.html))
        wired = set(re.findall(r'text: "([^"]+)"', block))
        self.assertEqual(wired - markup_ids, set(), "editor textarea ids missing from the markup")

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

    def test_preview_shows_the_same_prompt_the_bot_posts(self) -> None:
        """The preview must not quote a different prompt than the bot sends."""
        from rules import RULES_POST_CONTENT

        self.assertIn('id="rules-preview-content"', self.html)
        shown = re.search(
            r'id="rules-preview-content"[^>]*>([^<]+)<', self.html
        )
        self.assertIsNotNone(shown, "preview prompt element is empty")
        self.assertEqual(shown.group(1).strip(), RULES_POST_CONTENT)

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
