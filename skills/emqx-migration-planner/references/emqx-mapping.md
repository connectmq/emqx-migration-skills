# EMQX to ConnectMQ: Feature and Configuration Mapping

Covers EMQX 5.x (HOCON `emqx.conf`, Dashboard "Integration" menu), EMQX 4.x (`emqx.conf` key = value, plugins,
`emqx_ctl`) and EMQX Cloud (Serverless and Dedicated). Option names differ between minor versions: when a name below
does not exist in your version, find the equivalent in that version's documentation instead of guessing.

## Configuration mapping

| EMQX 5 (HOCON) | EMQX 4 | ConnectMQ | Notes |
| --- | --- | --- | --- |
| `listeners.tcp.default.bind = 1883` | `listener.tcp.external` | `mqtt://HOST:1883` | Tests only |
| `listeners.ssl.default` (+ `ssl_options`) | `listener.ssl.external` | `mqtts://HOST:8883` | Public CA, OS CA store |
| `listeners.ws.default` / `listeners.wss.default` (`/mqtt`) | `listener.ws/wss.external` | `ws(s)://HOST:8083/8084/mqtt` | Path `/mqtt` on both |
| `listeners.quic.*` | n/a | not available | Move to MQTT over TLS |
| `ssl_options.verify = verify_peer`, `peer_cert_as_username/clientid` | `listener.ssl.external.verify`, `peer_cert_as_username` | not available | Username/password over TLS |
| `max_connections` (listener) | `listener.*.max_connections` | Plan concurrent connections | Free 5, Basic 100, Scale 1,000 |
| `mqtt.max_packet_size` | `mqtt.max_packet_size` | Plan max message size | 256 KB on Free/Basic |
| `mqtt.max_inflight` | `zone.*.max_inflight` | Broker-managed receive maximum | No client change |
| `mqtt.max_mqueue_len` | `zone.*.max_mqueue_len` | Session-bound queue | Lost after the plan's session expiry |
| `mqtt.session_expiry_interval` (default 2h) | `zone.*.session_expiry_interval` | Plan max 1 h / 2 h / 24 h | |
| `mqtt.server_keepalive`, `keepalive_multiplier` | `zone.*.server_keepalive` | Cap 600 s | |
| `mqtt.max_subscriptions` | `zone.*.max_subscriptions` | Plan subscriptions per client | 20 / 50 / 100 |
| `mqtt.retain_available`, `retainer.*` | `retainer.*` plugin | Retained on every plan | Count capped per plan |
| `mqtt.shared_subscription` | `zone.*.shared_subscription` | Basic and Scale | `$queue/` must be renamed |
| `mqtt.max_clientid_len` | `mqtt.max_clientid_len` | Client ID = `<ISSUED_ID>_<suffix>` | Keep suffixes short |
| `mountpoint` (listener) | `listener.*.mountpoint` | Topic prefix in the tree | Or strip/add in the bridge |
| `authentication = [...]` | `auth.*` plugins, `allow_anonymous` | Credentials issued in the console | No external backends, no anonymous |
| `authorization.sources` (file, built_in_database, http, mysql, postgresql, mongodb, redis) | `acl_file`, `auth.*` ACL plugins | Access control rules | `emqx_audit.py` converts file + built_in_database |
| `authorization.no_match` | `acl_nomatch` | No-match policy | EMQX default is `allow`; prefer `deny` on ConnectMQ |
| `authorization.deny_action` | `zone.*.acl_deny_action` | Reason code `0x87`, connection stays | Like `ignore` |
| `authorization.cache` | `enable_acl_cache` | Managed | |
| Rule engine (SQL) + Data Integration (connectors, actions, sinks, sources) | Rule engine + resources (Enterprise), plugins | not available | Backend subscriber service |
| `delayed` (`$delayed/<s>/topic`) | `emqx_mod_delayed` | not available | Schedule in publisher/backend |
| `rewrite` | `emqx_mod_rewrite` | not available | Change topics in clients or in the bridge |
| `auto_subscribe` | `emqx_mod_subscription` | not available | Clients subscribe after CONNECT |
| `exhook` | `emqx_exhook` | not available | Client-side service |
| Webhook / HTTP action | `emqx_web_hook` | not available | Subscriber that calls HTTP |
| `flapping_detect`, `limiter`, `overload_protection`, `force_shutdown` | zone options | Managed per plan | Connection rate and per-client message rate caps |
| `$SYS` (`sys_topics`) | `$SYS` | not available | Console dashboard and sessions view |
| Dashboard, API keys, `emqx ctl` | Dashboard, `emqx_ctl` | ConnectMQ console | |
| Prometheus / StatsD / OpenTelemetry exporters | `emqx_prometheus` | not available | Console usage meters and dashboard |
| Cluster (`node`, `cluster`, Mria/Mnesia) | Mnesia cluster | Managed | |

## Authorization semantics

- **acl.conf is first-match**, like ConnectMQ, so `emqx_audit.py` keeps the order.
- The final `{allow, all}.` / `{deny, all}.` becomes the ConnectMQ no-match policy.
- `{deny, all, subscribe, ["$SYS/#", {eq, "#"}]}` (EMQX default) means "nobody may subscribe to the literal filter
  `#`". ConnectMQ rules cover every narrower filter, so the converter skips it and suggests allow rules + no-match
  `deny` instead. Allow rules with `{eq, ...}` are converted but get broader.
- `${clientid}`, `${username}` (EMQX 5) and `%c`, `%u` (EMQX 4) become ConnectMQ placeholders; values change
  (issued prefix, generated usernames), so pass `--client-map` / `--user-map` to keep old IDs in topics.
- Other placeholders (`${peerhost}`, `${cert_common_name}`, `${cert_subject}`, `${zone}`, `${mountpoint}`) and
  `ipaddr`/`ipaddrs`/`and` subjects cannot be expressed: those rules are listed in the notes.
- `{username, {re, "..."}}` regex subjects become `*` globs when simple (`^gw-.*$` -> `gw-*`); others are skipped.
- QoS/retain conditions on actions (EMQX 5) are dropped: ConnectMQ rules match subject, action and topic.
- `built_in_database` rules are exported per username, per client ID and for all clients
  (EMQX 5.1+: `GET /api/v5/authorization/sources/built_in_database/rules/users`, `.../rules/clients`,
  `.../rules/all`; older 5.0 builds use different paths). The converter orders them username -> clientid -> all.
- HTTP, MySQL, PostgreSQL, MongoDB and Redis authorization sources: export the rule rows yourself and turn them into
  the built-in JSON shape (`{"username": "...", "rules": [{"topic", "permission", "action"}]}`) to reuse the converter.

## Replacing the rule engine

Each EMQX rule (`SELECT ... FROM "topic" WHERE ...` + actions) becomes a small service:

1. Subscribe with a shared subscription on Basic/Scale so several instances share the load:
   `$share/ingest/devices/+/telemetry` (QoS 1).
2. Do the SQL's filtering/transformation in code.
3. Write to the action's target (Kafka, PostgreSQL, TimescaleDB, InfluxDB, HTTP, S3 ...).
4. Republish actions (`republish` to another topic) become a publish from the same service.

During the parallel run you can keep the EMQX rules running against EMQX traffic and start the services on ConnectMQ
in shadow mode (different database table or a dry-run flag), then compare outputs before switching.

ConnectMQ's built-in **Rules and Alerts** replace threshold alerting rules (`WHERE payload.temp > 70` + notification
actions) without code: condition on JSON payload fields, alert log, browser notifications.

## Behavioural differences that bite

- **`$queue/topic`** is EMQX shorthand for a shared subscription without a group. Rename to
  `$share/<group>/topic`. Do not depend on a specific dispatch strategy (`round_robin`, `sticky`, `hash_clientid`):
  consumers must be stateless across group members.
- **Delayed publish** (`$delayed/30/topic`) clients publish to a topic that nobody subscribes to on ConnectMQ:
  messages silently go nowhere. Search client code for `$delayed/` before migrating.
- **Topic rewrite and auto-subscribe** hide logic in the broker: list every rule and move it into clients.
- **Large clusters and connection counts.** EMQX is often sized for 100k+ connections. Above Scale limits, ask
  support@connectmq.com for a custom plan before planning waves.
- **Session expiry.** EMQX defaults to 2 hours; Basic allows 2 h and Scale 24 h. Free allows 1 h.
- **MQTT 3.1** clients (proto_ver 3) must be upgraded to 3.1.1 or 5.
- **Anonymous access** (`allow_anonymous = true`, the EMQX 4 default) is common in internal deployments: find those
  clients (`username` empty in `/api/v5/clients`) and give them credentials.
- **Client certificates**: EMQX deployments using `peer_cert_as_username` or `verify_peer` + `fail_if_no_peer_cert`
  move to username/password; the audit marks them as blockers.
- **MQTT over QUIC** clients (EMQX 5 only) must switch to TCP/TLS.

## Inventory sources

| Need | EMQX 5 | EMQX 4 |
| --- | --- | --- |
| Version | `emqx ctl status`, Dashboard | `emqx_ctl status` |
| Connections now / peak | `GET /api/v5/stats` (`connections.count`, `connections.max`, `live_connections.count`) | `GET /api/v4/stats` |
| Message and byte rates | `GET /api/v5/metrics` (`messages.received`, `messages.sent`, `bytes.received`, `bytes.sent`), sample twice and diff | `GET /api/v4/metrics` |
| Retained count | `GET /api/v5/stats` (`retained.count`) | `/api/v4/stats` |
| Subscriptions | `GET /api/v5/stats` (`subscriptions.count`), `GET /api/v5/subscriptions` | `/api/v4/subscriptions` |
| Client list (IDs, usernames, proto_ver, queues) | `GET /api/v5/clients?limit=1000&page=N` | `/api/v4/clients` |
| Rules and integrations | `GET /api/v5/rules`, Dashboard Integration | `emqx_ctl rules list`, Dashboard |
| CLI equivalents | `emqx ctl broker stats`, `emqx ctl clients list`, `emqx ctl rules list` | `emqx_ctl ...` |

The EMQX 5 REST API authenticates with an API key and secret (HTTP basic auth), created in the Dashboard:

```bash
curl -s -u "$EMQX_API_KEY:$EMQX_API_SECRET" "http://EMQX_HOST:18083/api/v5/stats"
for p in 1 2 3; do curl -s -u "$EMQX_API_KEY:$EMQX_API_SECRET" \
  "http://EMQX_HOST:18083/api/v5/clients?limit=1000&page=$p" > migration/clients-$p.json; done
```

## Sizing example

EMQX `/api/v5/stats` shows `connections.max` 640, 590 always connected, 3 rule-engine consumers moving to services.
Concurrent: 640 + 3 services + 1 bridge + 20 % = 773 -> Scale (1,000). Session minutes: 590 x 43,200 = 25.5 M per
month -> more than Scale's 10 M -> custom plan, or devices that report every few minutes disconnect in between.
