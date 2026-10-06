"""Keep a DuckDNS record pointed at this machine, and check the dashboard agrees.

DuckDNS (<https://www.duckdns.org>) is a free dynamic-DNS service: you register
`yourname.duckdns.org` once, and a tiny HTTP request keeps it pointed at
whatever public address your ISP hands you next. That is the usual way to reach
a home-hosted Sentinel dashboard from outside the house - but it is only half of
the setup, which is why this module also checks the other half:

  * the record has to keep up with the connection (otherwise the name resolves
    to a stale address and the dashboard "just doesn't load");
  * the dashboard has to accept the name in the HTTP `Host` header, which is a
    deliberate anti-DNS-rebinding allowlist, not an accident;
  * something has to actually reach the dashboard - port-forwarded HTTPS on the
    bot host, or a reverse proxy (the recommended shape), because the default
    listener is loopback-only;
  * if TLS is terminated anywhere but the dashboard itself, the browser needs
    `dashboard_public_url` so `dashboard_secure_cookie` can work.

Everything here is opt-in and offline-safe: with no `duckdns_domain` /
`duckdns_token` in the config, `DuckDNSUpdater.start()` does nothing, and
`update_record()` is the only thing that touches the network.

Configuration (config.json):

    "duckdns_domain":  "yourname",              # or "yourname.duckdns.org"
    "duckdns_token":   "a7c4d0ad-114e-40ef-...", # the DuckDNS account token
    "duckdns_enabled": true,
    "duckdns_interval_minutes": 5,

`duckdns_token` is the *DuckDNS account* token from the site, not the
dashboard login key (`dashboard_token`) - mixing the two is the most common
confusion, so the updater reports "KO" without ever echoing the token back.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import random
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union
from urllib.parse import urlsplit

import aiohttp

logger = logging.getLogger("sentinel.duckdns")

UPDATE_URL = "https://www.duckdns.org/update"
REQUEST_TIMEOUT_SECONDS = 15.0

# DuckDNS asks for no more than one update per five minutes per domain, and
# treats a record that is already correct as a no-op, so the defaults match the
# service's own guidance. The spread keeps a fleet of bots from firing at the
# same second after a restart.
DEFAULT_INTERVAL_SECONDS = 5 * 60
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 24 * 60 * 60
INTERVAL_JITTER_SECONDS = 30

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@dataclass(frozen=True)
class UpdateResult:
    """One DuckDNS update attempt, as reported back by the service."""

    ok: bool
    domain: str
    ip: Optional[str] = None
    changed: bool = False
    detail: str = ""

    def describe(self) -> str:
        """A one-line, token-free summary for logs and the CLI."""
        if not self.ok:
            return f"DuckDNS update failed for {self.domain}: {self.detail or 'KO'}"
        if not self.ip:
            return f"DuckDNS record for {self.domain} is up to date."
        if self.changed:
            return f"DuckDNS record for {self.domain} set to {self.ip}."
        return f"DuckDNS record for {self.domain} already points at {self.ip}."


def normalize_domain(value: Any) -> str:
    """`yourname`, `yourname.duckdns.org`, or a full URL -> `yourname`.

    DuckDNS expects the subname only, and happily accepts the full hostname;
    this accepts both, plus whatever a user pasted from the browser bar.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "//" in text:
        text = urlsplit(text).hostname or ""
    text = text.split("/", 1)[0].strip().lower().rstrip(".")
    if text.endswith(".duckdns.org"):
        text = text[: -len(".duckdns.org")]
    return text.strip(".")


def domain_fqdn(domain: Any) -> str:
    """The public hostname for a configured domain (`yourname.duckdns.org`)."""
    normalized = normalize_domain(domain)
    return f"{normalized}.duckdns.org" if normalized else ""


def credentials(config: dict) -> Optional[tuple[str, str]]:
    """(domain, token) if DuckDNS is configured, else None."""
    domain = normalize_domain(
        os.environ.get("SENTINEL_DUCKDNS_DOMAIN") or config.get("duckdns_domain", "")
    )
    token = str(
        os.environ.get("SENTINEL_DUCKDNS_TOKEN") or config.get("duckdns_token", "")
    ).strip()
    if not domain or not token:
        return None
    return domain, token


def configured(config: dict) -> bool:
    """True when the domain and token are present *and* enabled."""
    if not bool(config.get("duckdns_enabled", True)):
        return False
    return credentials(config) is not None


def interval_seconds(config: dict) -> int:
    """The refresh interval, clamped to something the service tolerates."""
    raw = config.get("duckdns_interval_minutes", DEFAULT_INTERVAL_SECONDS // 60)
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        minutes = DEFAULT_INTERVAL_SECONDS / 60
    if minutes <= 0:
        minutes = DEFAULT_INTERVAL_SECONDS / 60
    return int(min(max(minutes * 60, MIN_INTERVAL_SECONDS), MAX_INTERVAL_SECONDS))


def parse_update_response(text: str) -> tuple[bool, Optional[str], bool, str]:
    """Read a DuckDNS `/update` reply.

    Returns `(ok, ip, changed, detail)`. With `verbose=true` a good reply is::

        OK
        203.0.113.7
        UPDATED          # or NOCHANGE

    and a bad one is `KO`. Anything else is treated as a failure, with the
    (short) body kept as the detail so the log says what the service said.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return False, None, False, "empty response"
    status = lines[0].upper()
    if status != "OK":
        return False, None, False, lines[0][:120]

    ip: Optional[str] = None
    changed = False
    for line in lines[1:]:
        upper = line.upper()
        if upper == "UPDATED":
            changed = True
        elif upper == "NOCHANGE":
            changed = False
        else:
            ip = line
    return True, ip, changed, "".join(lines)


def _redact(url: str) -> str:
    """A URL safe to log: the update URL carries the account token."""
    return url.split("?", 1)[0]


async def update_record(
    domain: str,
    token: str,
    *,
    ip: str = "",
    session: Optional[aiohttp.ClientSession] = None,
    url: str = UPDATE_URL,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> UpdateResult:
    """Point `domain.duckdns.org` at this machine (or at `ip`).

    An empty `ip` is what makes this work from a dynamic connection: DuckDNS
    then records the address the request came from. Pass a `session` to reuse an
    existing connection pool; otherwise one is created and closed here.
    """
    normalized = normalize_domain(domain)
    if not normalized or not token:
        raise ValueError("DuckDNS needs both a domain and a token")

    params = {"domains": normalized, "token": token, "ip": ip, "verbose": "true"}
    owns_session = session is None
    if session is None:
        session = aiohttp.ClientSession()
    try:
        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                body = await response.text()
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return UpdateResult(
                ok=False,
                domain=domain_fqdn(normalized),
                detail=f"{_redact(url)} unreachable ({exc.__class__.__name__})",
            )
    finally:
        if owns_session:
            await session.close()

    if status != 200:
        return UpdateResult(
            ok=False,
            domain=domain_fqdn(normalized),
            detail=f"{_redact(url)} replied HTTP {status}",
        )

    ok, ip_reported, changed, detail = parse_update_response(body)
    return UpdateResult(
        ok=ok,
        domain=domain_fqdn(normalized),
        ip=ip_reported,
        changed=changed,
        detail=detail,
    )


class DuckDNSUpdater:
    """Refreshes one DuckDNS record on a timer, and narrates what it means.

    Started by the bot when `duckdns_domain` and `duckdns_token` are set. A
    failure never propagates: a dynamic-DNS glitch must not take the Discord
    bot down, and the next tick retries anyway.

    ``config`` may be the config dict itself or a zero-argument callable
    returning it. The bot passes ``lambda: self.config`` because the
    interactive installer replaces its config after the bot object is built,
    and a stale dict here would leave the record quietly never updated.
    """

    def __init__(
        self,
        config: Union[dict, Callable[[], dict]],
        *,
        session: Optional[aiohttp.ClientSession] = None,
        interval_seconds: Optional[float] = None,
        on_result: Optional[Callable[[UpdateResult], None]] = None,
        url: str = UPDATE_URL,
    ) -> None:
        self.config = config
        self._session = session
        self._owns_session = session is None
        self._interval = interval_seconds
        self._on_result = on_result
        self._url = url
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._token_hint_logged = False
        self.last_result: Optional[UpdateResult] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _current_config(self) -> dict:
        """The live config, whichever way it was handed over (see the class)."""
        return self.config() if callable(self.config) else self.config

    def _delay(self) -> float:
        base = (
            self._interval
            if self._interval is not None
            else interval_seconds(self._current_config())
        )
        return float(base) + random.uniform(0, min(INTERVAL_JITTER_SECONDS, base / 10))

    async def start(self) -> bool:
        """Begin refreshing the record. False when DuckDNS is not configured."""
        if self.running:
            return True
        config = self._current_config()
        if not configured(config):
            if credentials(config) and not bool(config.get("duckdns_enabled", True)):
                logger.info("DuckDNS updating disabled by config.")
            return False
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="duckdns-updater")
        domain, _token = credentials(config) or ("", "")
        logger.info(
            "DuckDNS: keeping %s pointed at this machine (every %ss).",
            domain_fqdn(domain),
            int(self._delay()),
        )
        return True

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                await self.update_now()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._delay())
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive, keeps the bot alive
            logger.exception("DuckDNS updater crashed; the record will go stale.")

    async def update_now(self) -> Optional[UpdateResult]:
        """Run one update and log it. Returns None when not configured."""
        config = self._current_config()
        pair = credentials(config)
        if pair is None:
            return None
        domain, token = pair
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        try:
            result = await update_record(
                domain, token, session=self._session, url=self._url
            )
        except ValueError as exc:  # malformed config
            result = UpdateResult(ok=False, domain=domain_fqdn(domain), detail=str(exc))
        self.last_result = result
        if self._on_result is not None:
            try:
                self._on_result(result)
            except Exception:  # pragma: no cover - a callback must not break the loop
                logger.exception("DuckDNS result callback failed.")
        if result.ok:
            # Only a *change* is interesting at info level; a steady record
            # would otherwise fill the log every five minutes.
            (logger.info if result.changed else logger.debug)("%s", result.describe())
            if result.changed:
                log_dashboard_url(config)
        else:
            logger.warning("%s", result.describe())
            if not self._token_hint_logged:
                # A "KO" is almost always the wrong credential, and the two
                # tokens in this project are easy to mix up. Say so once.
                self._token_hint_logged = True
                logger.warning(
                    "Check duckdns_token: it is the DuckDNS *account* token from "
                    "duckdns.org, not the dashboard login key (dashboard_token)."
                )
        return result

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._owns_session and self._session is not None:
            session, self._session = self._session, None
            await session.close()


def log_dashboard_url(config: dict) -> None:
    """Log the URL the dashboard is meant to be reached at, once it is known."""
    public = str(config.get("dashboard_public_url") or "").strip()
    if public:
        logger.info("Dashboard: %s", public)
        return
    pair = credentials(config)
    if pair is None or not bool(config.get("dashboard_enabled", True)):
        return
    domain, _token = pair
    scheme = "https" if config.get("dashboard_tls_cert") else "http"
    port = config.get("dashboard_port", 8765)
    suffix = "" if (scheme, port) in {("https", 443), ("http", 80)} else f":{port}"
    logger.info(
        "Dashboard: %s://%s%s/ (set dashboard_public_url if it is reached another way)",
        scheme,
        domain_fqdn(domain),
        suffix,
    )


def _split_host_port(value: str) -> str:
    """`[::1]:8765` / `127.0.0.1:8765` / `myhost.duckdns.org` -> the host part."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("["):
        return urlsplit("//" + text).hostname or text
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        if port.isdigit():
            return host
    return text


def _host_is_loopback(host: str) -> bool:
    name = _split_host_port(host).lower()
    if name in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _allowlist(config: dict) -> list[str]:
    value = config.get("dashboard_allowed_hosts", [])
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def dashboard_warnings(config: dict) -> list[str]:
    """Everything about this DuckDNS/dashboard combination worth fixing.

    These are the failures that are hard to diagnose from the browser side -
    a record that is correct but rejected, a name that is allow-listed but
    unreachable, a secure cookie that no browser will store over plain HTTP -
    phrased as the change that fixes each one.
    """
    warnings: list[str] = []
    pair = credentials(config)
    if pair is None:
        return warnings
    domain, _token = pair
    fqdn = domain_fqdn(domain)

    if not bool(config.get("dashboard_enabled", True)):
        warnings.append(
            f"DuckDNS is keeping {fqdn} updated, but the dashboard is disabled "
            "('dashboard_enabled': false), so that name has nothing to serve."
        )
        return warnings

    allowed = {_split_host_port(item).lower().rstrip(".") for item in _allowlist(config)}
    public_url = str(config.get("dashboard_public_url") or "").strip()
    public_host = _split_host_port(urlsplit(public_url).netloc).lower() if public_url else ""
    if fqdn.lower() not in allowed and public_host != fqdn.lower():
        warnings.append(
            f"{fqdn} is not in 'dashboard_allowed_hosts', so requests arriving "
            f"with that Host header are answered with 400 Unrecognized Host "
            f"header. Add it: \"dashboard_allowed_hosts\": [\"{fqdn}\"]."
        )

    host = str(config.get("dashboard_host") or "127.0.0.1")
    trusted = config.get("dashboard_trusted_proxies", [])
    trusted = [str(item) for item in trusted] if isinstance(trusted, list) else []
    if _host_is_loopback(host) and not trusted:
        warnings.append(
            "The dashboard listens on loopback only, so '{}' cannot reach it "
            "directly. Either run a reverse proxy on this machine (recommended: "
            "keep dashboard_host on 127.0.0.1, point the proxy at 8765, and set "
            "dashboard_trusted_proxies to the proxy's address) or set "
            "\"dashboard_host\": \"0.0.0.0\" and forward the port to this host."
            .format(fqdn)
        )

    tls_here = bool(str(config.get("dashboard_tls_cert") or "").strip())
    if not tls_here and not public_url.lower().startswith("https://"):
        warnings.append(
            "The dashboard login key would cross the network in clear text: "
            "requests for '{}' are plain HTTP. Terminate HTTPS in a reverse "
            "proxy (set dashboard_public_url to the https:// URL) or serve TLS "
            "here with dashboard_tls_cert/dashboard_tls_key.".format(fqdn)
        )
    elif not tls_here and not bool(config.get("dashboard_secure_cookie", False)):
        # TLS is terminated upstream but the cookie is not marked secure, so a
        # downgrade would leak it.
        warnings.append(
            "HTTPS is terminated upstream (dashboard_public_url is https), but "
            "'dashboard_secure_cookie' is false, so the session cookie would "
            "also be sent over plain HTTP. Set it to true."
        )
    return warnings


def describe_status(config: dict) -> str:
    """A one-line status for `sentinel --dashboard` / `--duckdns`."""
    pair = credentials(config)
    if pair is None:
        return "DuckDNS: not configured (set duckdns_domain and duckdns_token)."
    domain, _token = pair
    if not bool(config.get("duckdns_enabled", True)):
        return f"DuckDNS: {domain_fqdn(domain)} configured but disabled."
    return (
        f"DuckDNS: {domain_fqdn(domain)} (updating every "
        f"{interval_seconds(config) // 60} min)."
    )

