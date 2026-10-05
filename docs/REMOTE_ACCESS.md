# Reaching the dashboard from outside the house

The dashboard binds to `127.0.0.1:8765` by default, so it is reachable only on
the machine running the bot. That is the safe default: its login key is a
bot-wide credential, and it has no user accounts — whoever has the key is the
owner.

Sooner or later, though, you want to check the dashboard from a phone or from
work. This guide covers the four ways to do that, in order of preference, and
the [DuckDNS](#duckdns-a-name-that-follows-your-house) updater that keeps a
`yourname.duckdns.org` name pointed at a home connection whose public address
changes. Run `sentinel --dashboard` (or `python3 bot.py --dashboard`) at any
point to print the effective settings and the exact next step.

## The shape of every remote setup

```
browser ──HTTPS──▶ public name ──▶ router ──▶ [TLS endpoint] ──▶ dashboard
                 (DuckDNS or          (port            (proxy, or
                  a real domain)       forward)         Sentinel itself)
```

There are four things to get right, and each one fails differently if you miss
it. That is why the dashboard log and `--dashboard` name the missing setting
instead of leaving you with a blank page:

| Piece | Setting | If it is wrong you see |
|-------|---------|------------------------|
| Where it listens | `dashboard_host`, `dashboard_port` | connection times out |
| Which hostnames it answers for | `dashboard_allowed_hosts`, `dashboard_public_url` | `400 Unrecognized Host header` |
| HTTPS | `dashboard_tls_cert`/`_key`, or the proxy's certificate | browser cookie warnings; a login that succeeds and bounces |
| Who the visitor is | `dashboard_trusted_proxies` | log lines naming the proxy; the login throttle locking out everyone at once |

The `400 Unrecognized Host header` deserves a word, because it looks like a
bug and is not one. The dashboard answers only for hostnames you list, so that
a hostile page on the internet cannot point its own name at your machine and
have your browser talk to it with your cookies (a DNS-rebinding attack).
Use `sentinel --dashboard` to see what is allowed, and add the name you
actually type — including the port if you use a non-standard one, e.g.
`yourname.duckdns.org:8443`.

## Option 1 — HTTPS reverse proxy on the same machine (recommended)

TLS is terminated by a proxy that listens on 443, and Sentinel keeps hearing
from the proxy over loopback HTTP. Everything stays local; only the proxy is
exposed.

1. Point a name at the machine (see [DuckDNS](#duckdns-a-name-that-follows-your-house), or use a domain you own).
2. Forward **80 and 443** from the router to the bot host. Do *not* forward
   8765 — the dashboard should stay unreachable directly, including from your
   own network.
3. Install a proxy that can obtain a certificate. [Caddy](https://caddyserver.com)
   does it with one line:

   ```caddyfile
   yourname.duckdns.org {
       reverse_proxy 127.0.0.1:8765
   }
   ```

   nginx with a certificate you already have (Certbot, or a DuckDNS
   certificate) needs a little more:

   ```nginx
   server {
       listen 443 ssl;
       server_name yourname.duckdns.org;

       ssl_certificate     /etc/letsencrypt/live/yourname.duckdns.org/fullchain.pem;
       ssl_certificate_key /etc/letsencrypt/live/yourname.duckdns.org/privkey.pem;

       location / {
           proxy_pass http://127.0.0.1:8765;
           proxy_set_header Host $host;              # the allowlist checks this
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```

   The `Host` header must be the public name — the allowlist checks it. If the
   proxy strips it or rewrites it to `localhost`, add that rewritten value too
   (or fix the proxy).

4. Configure Sentinel:

   ```json
   {
     "dashboard_host": "127.0.0.1",
     "dashboard_allowed_hosts": ["yourname.duckdns.org"],
     "dashboard_public_url": "https://yourname.duckdns.org",
     "dashboard_secure_cookie": true,
     "dashboard_trusted_proxies": ["127.0.0.1", "::1"]
   }
   ```

   `dashboard_public_url` gives the log and `--dashboard` the address users
   will actually open, allows that hostname implicitly, and tells the warnings
   that HTTPS is in front even though the listener itself is plain HTTP. List
   no proxies if the proxy is not on this machine — instead list the proxy's
   address or its Docker network range (e.g. `"172.18.0.0/16"`). See
   [Behind a proxy](#behind-a-proxy-trusting-x-forwarded-for).

5. Restart the bot and run `sentinel --dashboard`: it should end with
   `No remote-access problems found.`

## Option 2 — Serve HTTPS from Sentinel itself

No proxy to install, but the certificate is yours to obtain and renew, and the
dashboard port is the one exposed to the internet.

```json
{
  "dashboard_host": "0.0.0.0",
  "dashboard_port": 8765,
  "dashboard_tls_cert": "/etc/sentinel/fullchain.pem",
  "dashboard_tls_key": "/etc/sentinel/privkey.pem",
  "dashboard_allowed_hosts": ["yourname.duckdns.org"],
  "dashboard_public_url": "https://yourname.duckdns.org"
}
```

* Both `dashboard_tls_cert` and `dashboard_tls_key` are required; setting one
  alone is an error at startup rather than a half-configured listener.
* A certificate that cannot be loaded stops the dashboard from starting, on
  purpose: serving a certificate the browser rejects is worse than not
  starting.
* A TLS listener always marks the session cookie `secure`.
* Forward port 8765 from the router (not 80/443), and make sure the certificate
  was issued for the exact name in `dashboard_allowed_hosts`.
* A self-signed certificate produces a browser warning and no error in the
  log; the dashboard works if you accept it.

## Option 3 — SSH tunnel (no DNS, no ports)

Nothing is exposed at all, and nothing has to change in the config:

```bash
ssh -L 8765:127.0.0.1:8765 user@your-server
```

Then open <http://127.0.0.1:8765> on your workstation. The tunnel carries the
connection over SSH, which is already encrypted. This is the right answer when
only you use the dashboard; it is not practical for a phone.

## Option 4 — Bind `0.0.0.0` without TLS (just don't)

It works: set `dashboard_host` to `0.0.0.0`, forward the port, and add the
name to `dashboard_allowed_hosts`. But the login key then crosses the network
in clear text, and anyone who reads it owns the bot. On a home LAN with a
trusted network this is a judgement call; over the internet it is a mistake.
If you do it anyway, set `dashboard_secure_cookie` to `false` — with it `true`
over plain HTTP the browser refuses to store the session cookie and every
login bounces straight back to the login screen.

## DuckDNS: a name that follows your house

[DuckDNS](https://www.duckdns.org) gives you a free `yourname.duckdns.org`
subdomain and an API to keep it pointed at your current public address. Two
halves: the dashboard has to answer for that name, and the record has to stay
current (home connections usually get a new address every few days).

### 1. Create the subdomain and get the token

Sign in at <https://www.duckdns.org>, add a subdomain (say `myhome`), and copy
the **account token** shown at the top of the page.

### 2. Tell Sentinel about it

```json
{
  "duckdns_domain": "myhome",
  "duckdns_token": "the-account-token-from-duckdns.org",
  "duckdns_enabled": true,
  "duckdns_interval_minutes": 5,
  "dashboard_allowed_hosts": ["myhome.duckdns.org"]
}
```

`duckdns_domain` accepts `myhome` or the full `myhome.duckdns.org`, and the
environment variables `SENTINEL_DUCKDNS_DOMAIN` / `SENTINEL_DUCKDNS_TOKEN`
override the file, so you can keep the token out of `config.json` in a
service unit.

> The DuckDNS token is **not** the dashboard key (`dashboard_token`) and not
> the Discord bot token. If the updater reports `KO`, it is almost always the
> wrong token: copy the one from the top of the DuckDNS page.

While the bot runs, the updater sends an update every five minutes (the
shortest interval DuckDNS asks users to keep), with a little jitter so a
restart does not create a burst. An unreachable service, a timeout or a `KO`
is logged and retried on the next tick — the bot does not stop, and the
previous record stays in place until an update succeeds.

### 3. Check it

```bash
sentinel --duckdns      # send one update now and print the result
sentinel --dashboard    # show the URLs and anything blocking remote access
```

`--duckdns` prints the address DuckDNS recorded, and `--dashboard` prints the
public URL first and then, if anything is missing, a list of what to change.
Both are safe to run while the bot is running; the running updater keeps its
own schedule.

### 4. Open it

With Option 1 (proxy) you open `https://myhome.duckdns.org`. Without a proxy,
Sentinel is still loopback-only, and that is the one thing DuckDNS cannot fix:
the name resolves to your router, not into the machine. `--dashboard` says so
in as many words, with the two ways out (run a proxy, or bind `0.0.0.0` with
`dashboard_allowed_hosts` set).

## Behind a proxy: trusting `X-Forwarded-For`

A proxied request arrives from the proxy's address, so without help the
dashboard would treat the whole internet as a single client: the login
throttle (five failures in five minutes) would lock everyone out after one
stranger's guessing, and every log line would name the proxy.

`dashboard_trusted_proxies` fixes that. It lists the addresses — an IP, or a
CIDR range as `ipaddress` understands it — whose `X-Forwarded-For` header may
be believed:

| Setup | Value |
|-------|-------|
| Proxy on the same machine | `["127.0.0.1", "::1"]` |
| Proxy on the LAN | `["192.0.2.10"]` |
| Proxy in Docker | `["172.18.0.0/16"]` (the bridge subnet) |
| Cloudflare / a CDN | the CDN's published ranges — trusted only if the origin is firewalled to accept traffic *only* from them |

The header is ignored for any peer not in the list, because it is just text
until you know which hop wrote it. That also means an attacker cannot dodge
the throttle by inventing addresses, and it is why a missing entry is a
configuration problem rather than a security hole.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `400 Unrecognized Host header` | The name you opened is not in the allowlist | Add it to `dashboard_allowed_hosts`, or set `dashboard_public_url` to that URL; check `sentinel --dashboard` |
| Login appears to succeed, then returns to the login screen | The browser refused to store the session cookie: `dashboard_secure_cookie` is `true` but the page is plain HTTP | Put HTTPS in front (Option 1 or 2) and set `dashboard_public_url` to the `https://` URL, or set `dashboard_secure_cookie` to `false` |
| The page never loads; `sentinel --duckdns` works | The record is current but nothing is listening on the port — loopback-only listener, or the router forwards to the wrong host | Run a proxy on the same machine (Option 1), or bind `0.0.0.0` and forward the port (Option 4) |
| A certificate warning only | Self-signed certificate | Use a real certificate (a proxy that obtains one, or a Let's Encrypt certificate) |
| `KO` from `--duckdns` | Wrong token or wrong subdomain name | Copy the account token from the top of the DuckDNS page; the subdomain is the part before `.duckdns.org` |
| Everyone gets `429 Too many attempts` at once | The dashboard sees all visitors as the proxy | Add the proxy to `dashboard_trusted_proxies` |
| Log lines name the proxy instead of the culprit | Same as above | Same as above |

The bot logs everything relevant at startup (and after a failed update) with
the prefix `Remote access:`; `data/bot.log` is the first place to look, and
`sentinel --dashboard` reports the same findings on demand.

## Reference

| Key | Default | Meaning |
|-----|---------|---------|
| `dashboard_host` | `127.0.0.1` | Address to listen on; `0.0.0.0` means every interface |
| `dashboard_port` | `8765` | Port to listen on |
| `dashboard_allowed_hosts` | `[]` | Hostnames the dashboard answers for (`Host` header allowlist) |
| `dashboard_public_url` | `""` | The URL users open, e.g. `https://myhome.duckdns.org`; also allowlisted and used to decide whether HTTPS is in front |
| `dashboard_secure_cookie` | `false` | Mark the session cookie `secure` (forced on by TLS); requires HTTPS end to end |
| `dashboard_tls_cert` / `dashboard_tls_key` | `""` | Serve HTTPS here; set both or neither |
| `dashboard_trusted_proxies` | `[]` | Addresses/CIDR ranges whose `X-Forwarded-For` may be believed |
| `duckdns_enabled` | `true` | Run the updater when a domain and token are set |
| `duckdns_domain` | `""` | `myhome` or `myhome.duckdns.org` |
| `duckdns_token` | `""` | DuckDNS account token (not the dashboard key) |
| `duckdns_interval_minutes` | `5` | Update interval; lower values are clamped to DuckDNS's 5-minute guidance |

Environment overrides: `SENTINEL_DASHBOARD_HOST`, `SENTINEL_DASHBOARD_PORT`,
`SENTINEL_DASHBOARD_PUBLIC_URL`, `SENTINEL_DASHBOARD_TLS_CERT`,
`SENTINEL_DASHBOARD_TLS_KEY`, `SENTINEL_DUCKDNS_DOMAIN`,
`SENTINEL_DUCKDNS_TOKEN`.

## Security checklist

* [ ] The dashboard is behind HTTPS end to end, with `dashboard_secure_cookie` on.
* [ ] Only the TLS endpoint (443, or 8765 for Option 2) is forwarded at the
      router; 8765 stays closed when a proxy fronts it.
* [ ] The login key is long and private, and rotates if it has ever been typed
      into a plain-HTTP page.
* [ ] `config.json` is not world-readable, and the DuckDNS token is not shared.
* [ ] `dashboard_trusted_proxies` names only machines you control.
* [ ] The host firewall allows 80/443 (or the dashboard port) and nothing else
      from the internet.
