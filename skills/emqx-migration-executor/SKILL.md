---
name: emqx-migration-executor
description: Executes a planned migration from EMQX (4.x, 5.x or EMQX Cloud) to the ConnectMQ managed MQTT broker step by step. Verifies the ConnectMQ broker, sets up an EMQX MQTT bridge (connector, rule and action/source) or an external relay, copies retained messages, brings rule-engine replacement services live, validates message flow on both brokers, moves clients in waves with go/no-go checks, handles rollback and decommissions EMQX while keeping a migration log. Use when an EMQX-to-ConnectMQ plan exists and the team is ready to bridge, run a wave, cut over, roll back, or troubleshoot connection and authorization errors during the move.
license: MIT
metadata:
  source-broker: emqx
  target-broker: connectmq
  companion-skill: emqx-migration-planner
---

# EMQX to ConnectMQ: Migration Executor

You carry out an EMQX to ConnectMQ migration one verified step at a time: change configuration, run the scripts,
read the results, and stop for a human decision at every go/no-go point.

Before starting, read:

- `migration/plan.md` (from the planner skill). If it is missing, offer to run **emqx-migration-planner** first; do
  not improvise waves without a plan and a replacement for every EMQX rule.
- `references/connectmq-target.md` for limits and reason codes.
- `references/emqx-bridge.md` for the bridge or relay setup.
- `references/cutover-and-rollback.md` for wave rules, go/no-go and rollback.

## Rules

- **Ask before every production change** (connector/rule creation, ACL change, client wave, disabling a rule,
  stopping EMQX). Say what changes and how to undo it.
- Keep secrets out of committed files: `CONNECTMQ_USERNAME`, `CONNECTMQ_PASSWORD`, `CONNECTMQ_CLIENT_ID`,
  `SOURCE_USERNAME`, `SOURCE_PASSWORD`, `EMQX_API_KEY`, `EMQX_API_SECRET` as environment variables or a secrets store.
- After each step append to `migration/log.md`: time, step, command, result, numbers, decision.
- On a failed check: stop, diagnose with the reason-code table, propose a fix or rollback. Never start the next wave
  with a failing check.

## Scripts

All in `scripts/` (Python 3.9+, `pip install "paho-mqtt>=2.0"`):

| Script | Purpose |
| --- | --- |
| `verify_connectmq.py` | Connect, QoS 0/1/2, retained, will, session, ACL deny checks |
| `copy_retained.py` | Copy retained messages from EMQX to ConnectMQ (or export/import JSON) |
| `compare_streams.py` | Subscribe on both brokers and compare topics, counts and payloads |
| `relay.py` | External bridge for EMQX Serverless or when integrations cannot be changed |
| `client_id_map.py` | Old -> new client ID mapping (if not done in planning) |

## Phase 1: Prepare ConnectMQ

1. Check the plan in the console matches `migration/plan.md` (connections, minutes, ACL and shared subscriptions).
2. Create the credentials from the plan, one for the bridge (or relay) and one for tests.
3. Enter the rules from `migration/acl-rules.md` in order, **bridge rules first** (see `references/emqx-bridge.md`),
   then rules for replacement services, then devices. Set the no-match policy, then enable ACL.
4. Smoke test:

   ```bash
   export CONNECTMQ_USERNAME=... CONNECTMQ_PASSWORD=... CONNECTMQ_CLIENT_ID=...
   python scripts/verify_connectmq.py --url mqtts://BROKER_HOST:8883 --prefix <a branch the test credential may use>
   ```

   Add `--expect-deny-publish` / `--expect-deny-subscribe` for topics the ACL must block.

## Phase 2: Bridge and replacement services

1. Create the bridge from `references/emqx-bridge.md`: connector, egress rule + action for forward topics, ingress
   source + republish rule for reverse topics (or the 4.x plugin, or `relay.py`). Pool size 1, client ID prefix
   `ISSUED_ID_`.
2. Confirm in the ConnectMQ console sessions view that the bridge client is online with the expected client ID, and
   in EMQX that the rule's `failed` counter stays at 0.
3. Copy retained state (dry run first, exclude `$SYS`, test and junk topics):
   `python scripts/copy_retained.py --source mqtt://EMQX_HOST:1883 --filter 'devices/#' --export migration/retained-<date>.json --target mqtts://BROKER_HOST:8883 --limit <plan retained limit>`.
4. Start each rule-engine replacement service on ConnectMQ in shadow mode (writes to a shadow table / dry run).
   Compare its output with the EMQX rule's output for at least one business cycle.
5. Validate: `python scripts/compare_streams.py --source mqtt://EMQX_HOST:1883 --target mqtts://BROKER_HOST:8883 --filter '<forward filters>' --duration 600`.
   Expect MATCH. "Only on source" topics usually mean an ACL deny for the bridge, a SQL `FROM` that misses a topic,
   or an oversized payload.

## Phase 3: Waves

For each wave in the plan:

1. Run the go/no-go checklist from `references/cutover-and-rollback.md`; get an explicit "go".
2. Consumers first: switch replacement services from shadow to live and disable the matching EMQX rule actions
   (keep the rules, so rollback is re-enabling them).
3. For client waves, change host, port 8883 with TLS, credentials and client ID (from `migration/client-map.csv`).
   Show the exact config diff for each client type. Rename `$queue/` subscriptions to `$share/<group>/` and remove
   `$delayed/` publishes in the same release.
4. Watch 15 minutes: ConnectMQ connected count up by the wave size, EMQX `connections.count` down, no
   `0x85/0x86/0x87/0x8A/0x97` in client logs, `compare_streams.py` still MATCH, service outputs normal.
5. Soak for the agreed time, log the numbers, then propose the next wave. On a rollback trigger, follow the rollback
   actions and log the cause.

Typical client changes:

```text
host: emqx.internal:1883        -> BROKER_HOST:8883 (TLS, verify hostname, system CA store)
username/password: EMQX auth    -> ConnectMQ credential
client_id: sensor-0042          -> <ISSUED_ID>_sensor-0042
subscribe $queue/jobs           -> $share/workers/jobs
publish $delayed/60/reminders   -> schedule in the app, publish reminders at the due time
keepalive > 600                 -> <= 600
reconnect                       -> exponential backoff with jitter, max 60 s
```

## Phase 4: Cutover and decommission

Follow "Final cutover" in `references/cutover-and-rollback.md`: final retained copy, delete the bridge rule,
action/source and connector (or stop the relay), watch EMQX for stragglers (`GET /api/v5/clients`), keep EMQX for the
rollback window, then decommission the cluster, its API keys and authentication data, and remove EMQX exporters from
monitoring.

## Troubleshooting quick reference

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Connector test fails, `0x86` / bad username or password | Wrong credential | Copy from the console |
| Connector connects and drops, `0x85` | Generated client ID does not start with `ISSUED_ID_` | Set the ClientID prefix to `ISSUED_ID_` |
| Bridge keeps reconnecting, `0x8E` session taken over | Pool size > 1 with identical IDs, or two bridges with one ID | Pool size 1, unique prefix per bridge |
| Rule `failed` counter rising | ACL deny (`0x87`), payload too large (`0x97`) | Add bridge ACL rule; check plan max message size |
| Messages loop or duplicate | Overlapping egress/ingress topics, or `#` forwarded | Split topics by direction |
| TLS handshake error on connector | TLS verify without SNI/CA | Enable TLS verify with server name `BROKER_HOST`, system CA |
| Consumers miss messages after rename | `$queue/` subscriptions left in code | Use `$share/<group>/...` (Basic/Scale) |
| Devices `0x8A` banned | Wrong credentials retried in a loop from one NAT | Stop the wave, fix config, wait for the ban to expire |
| `0x97` on connect | Plan connections or minutes exhausted | Upgrade or slow the wave |
| SUBACK `0x87` on `#` or broad filters | ACL rule does not cover the whole requested filter | Add a rule that covers the filter, or subscribe narrower |
