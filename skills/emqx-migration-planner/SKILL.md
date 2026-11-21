---
name: emqx-migration-planner
description: Plans a migration from EMQX (4.x or 5.x, open source, Enterprise or EMQX Cloud) to the ConnectMQ managed MQTT broker. Audits emqx.conf, converts acl.conf and built_in_database authorization rules to ConnectMQ access control rules, inventories clients and load through the EMQX REST API, maps client IDs, finds rule-engine, delayed-publish, rewrite and $queue dependencies, checks the workload against ConnectMQ plan limits and writes a phased migration plan with waves and rollback. Use when someone wants to move off EMQX, replace EMQX Cloud or a self-hosted EMQX cluster with a managed broker, or asks how EMQX authentication, ACLs, rules or data integrations translate to ConnectMQ.
license: MIT
metadata:
  source-broker: emqx
  target-broker: connectmq
  companion-skill: emqx-migration-executor
---

# EMQX to ConnectMQ: Migration Planner

You are an MQTT migration engineer. You produce a migration plan from EMQX to ConnectMQ that the team can execute
without losing messages: an audit, converted ACL rules, a client ID map, a list of broker-side logic that must move
into services, and a filled-in plan.

Read `references/connectmq-target.md` first: generated credentials, the issued client ID prefix, no client
certificates, no rule engine and the plan limits shape most decisions. `references/emqx-mapping.md` has the
setting-by-setting mapping, authorization semantics, rule-engine replacement and EMQX pitfalls.

## Principles

- **EMQX hides logic in the broker.** Rules, data integrations, delayed publish, topic rewrite, auto-subscribe,
  ExHook and webhooks all disappear on ConnectMQ. Find every one before planning waves; each needs an owner and a
  replacement.
- **Bridge and migrate.** EMQX can bridge to ConnectMQ itself (MQTT connector / bridge); EMQX Serverless cannot,
  so use `relay.py` there. Consumers move first, devices in waves, rollback stays open.
- **Measure from the REST API**, not from the cluster size.
- **Every client gets new credentials and a new client ID** (`<ISSUED_ID>_<old-id>`), rolled out in one config change.

## Workflow

### Step 1: Collect the facts

Ask for (or find) and do not proceed on assumptions for the starred items:

- EMQX edition and version (4.x / 5.x, open source / Enterprise / Cloud Serverless / Cloud Dedicated) *
- `emqx.conf` (and `cluster.hocon` / overrides on EMQX 5, `etc/plugins/*.conf` on EMQX 4) *
- `acl.conf` and/or the authorization sources in use; for `built_in_database`, the JSON exports *
- Authentication backends in use (built-in DB, HTTP, JWT, MySQL, PostgreSQL, Redis, LDAP, client certificates) *
- Every rule and data integration (Dashboard > Integration, or `GET /api/v5/rules`) *
- Client groups (backend services, dashboards, gateways, devices), who changes each group's config and how *
- Downtime tolerance per group and target dates

### Step 2: Export and audit

```bash
# EMQX 5 REST API (API key + secret from the Dashboard)
A="-u $EMQX_API_KEY:$EMQX_API_SECRET"; E=http://EMQX_HOST:18083/api/v5
curl -s $A "$E/authorization/sources/built_in_database/rules/users?limit=1000" > migration/authz-users.json
curl -s $A "$E/authorization/sources/built_in_database/rules/clients?limit=1000" > migration/authz-clients.json
curl -s $A "$E/authorization/sources/built_in_database/rules/all" > migration/authz-all.json
curl -s $A "$E/clients?limit=1000&page=1" > migration/clients-1.json      # repeat per page
curl -s $A "$E/rules" > migration/rules.json

python scripts/emqx_audit.py --conf /etc/emqx/emqx.conf --acl /etc/emqx/acl.conf \
  --builtin migration/authz-users.json --builtin migration/authz-clients.json --builtin migration/authz-all.json \
  --clients migration/clients-1.json --out migration/
```

Outputs: `migration/audit.md` (blockers, actions, plan checks), `migration/acl-rules.{md,csv,json}`,
`migration/users.csv`, `migration/clients.csv`. Walk through every blocker with the user. Read `rules.json` and
list each rule with its SQL, actions and the service that will replace it.

Also search client code and configs for EMQX-specific topics: `$queue/`, `$delayed/`, `$share/` (strategy
assumptions), `$SYS/`, `$exclusive/`.

### Step 3: Measure the workload

Sample `GET /api/v5/stats` and `GET /api/v5/metrics` every few minutes for at least 24 hours (busiest day):
peak `connections.count`, `retained.count`, `subscriptions.count`, and message/byte rates from the difference of
two `metrics` samples. From `clients.csv`: protocol versions, empty usernames (anonymous), queued messages. Find the
largest payload and the fastest publisher (Dashboard topic metrics, or a temporary subscriber).

### Step 4: Fit check and plan choice

Fill section 2 of `assets/migration-plan-template.md`:

- Concurrent connections = peak + bridge/relay + replacement services + dashboards + 20 %.
- Session minutes per month = sum of connected minutes (always-on client ~ 43,200).
- Retained count vs plan, largest payload vs max message size, `session_expiry_interval` vs plan cap.
- Shared subscriptions, ACL or rule-engine replacement services with `$share` -> Basic or Scale.
- Above Scale -> contact support@connectmq.com for a custom plan before planning waves.

### Step 5: Identities and ACL

1. Decide the credential scope (per device / group / site); explain revocation trade-offs.
2. `python scripts/client_id_map.py migration/clients.csv --issued-id <ISSUED_ID> -o migration/client-map.csv`
   (`--credential-column` for several credentials). Resolve every `error` row.
3. After credentials exist, fill `new_username` in `migration/users.csv` and re-run the audit with
   `--user-map migration/users.csv --client-map migration/client-map.csv`.
4. Review every converted rule note: skipped `ipaddr`/`and` rules, `{eq, ...}` rules, dropped QoS/retain conditions,
   and the no-match policy (EMQX defaults to `allow`; recommend `deny` with explicit allows).

### Step 6: Replacement design

For each rule/integration, delayed publish, rewrite, auto-subscribe, ExHook and webhook, write: what it does,
replacement (service, client change, ConnectMQ Rules and Alerts), owner, and when it must be live (before the wave
that moves its publishers). Rename `$queue/` subscriptions to `$share/<group>/`.

### Step 7: Parallel run design

- EMQX 5 / 4 self-hosted or Cloud Dedicated with data integration: an EMQX MQTT bridge to ConnectMQ
  (see the executor's `references/emqx-bridge.md`).
- EMQX Serverless or no admin access: `relay.py`.
- Forward topics (EMQX -> ConnectMQ) = what devices publish; reverse topics = commands for devices still on EMQX.
  They must not overlap.

### Step 8: Write the plan

Copy `assets/migration-plan-template.md` to `migration/plan.md` and fill every section with real numbers, the
replacement table and waves with go criteria and rollback triggers (`references/cutover-and-rollback.md`). Summarise
for the user: plan chosen, blockers and replacements, waves, duration, open questions.

## Output checklist

- [ ] `migration/audit.md` reviewed; every blocker has a resolution and owner
- [ ] Every EMQX rule/integration has a replacement that goes live before its publishers move
- [ ] Workload numbers from `/api/v5/stats` and `/api/v5/metrics`
- [ ] Plan selected with headroom, or custom plan requested
- [ ] `migration/client-map.csv` without errors; `migration/acl-rules.md` with final usernames
- [ ] Bridge or relay topics defined without overlap
- [ ] `migration/plan.md` complete, including rollback per wave

Hand over to the **emqx-migration-executor** skill to carry out the plan.
