# EMQX Migration Skills for ConnectMQ

**AI agent skills for planning and executing a migration from EMQX (4.x, 5.x, EMQX Cloud) to the [ConnectMQ](https://connectmq.com/?utm_source=github&utm_medium=readme&utm_campaign=migration-skills&utm_content=emqx) managed MQTT broker.**

Give your AI coding agent (Claude Code, Claude.ai, or any agent that reads `SKILL.md` skills) the knowledge and the
tools of an MQTT migration engineer: it audits `emqx.conf`, converts `acl.conf` and built-in database authorization
rules, inventories clients through the EMQX REST API, finds the broker-side logic (rule engine, data integrations,
delayed publish, topic rewrite, `$queue`) that must move into services, writes a phased plan, then sets up the EMQX
bridge, runs the client waves and handles rollback.

> Part of the **MQTT Broker Migration Skills** collection:
> [HiveMQ](https://github.com/connectmq/hivemq-migration-skills) ·
> [Mosquitto](https://github.com/connectmq/mosquitto-migration-skills) ·
> **EMQX** ·
> [VerneMQ](https://github.com/connectmq/vernemq-migration-skills) ·
> [AWS IoT Core](https://github.com/connectmq/aws-iot-core-migration-skills) ·
> [Azure IoT Hub](https://github.com/connectmq/azure-iot-hub-migration-skills)

## What's inside

| Skill | What it does |
| --- | --- |
| [`emqx-migration-planner`](skills/emqx-migration-planner/SKILL.md) | Audits EMQX config, converts `acl.conf` (EMQX 4 and 5 syntax) and `built_in_database` rules, reads `/api/v5/clients`, `/stats`, `/metrics` and `/rules`, checks the workload against ConnectMQ plan limits, maps client IDs, designs rule-engine replacements, writes `migration/plan.md` |
| [`emqx-migration-executor`](skills/emqx-migration-executor/SKILL.md) | Verifies the ConnectMQ broker, configures the EMQX MQTT bridge (connector + rule + action/source) or an external relay, copies retained messages, brings replacement services live, compares message flow, runs client waves with go/no-go gates, rolls back, decommissions; keeps `migration/log.md` |

### Scripts (Python 3.9+)

| Script | Purpose |
| --- | --- |
| `emqx_audit.py` | `emqx.conf` (HOCON or 4.x) + `acl.conf` + built-in database JSON + clients JSON -> audit report, ConnectMQ ACL rules, users and clients lists (standard library only) |
| `client_id_map.py` | Old client IDs -> `<ISSUED_ID>_<old-id>`, flags unsafe characters and collisions |
| `verify_connectmq.py` | Smoke test: TLS connect, QoS 0/1/2, retained, Last Will, persistent session, ACL denies |
| `copy_retained.py` | Copies retained messages from EMQX to ConnectMQ (or exports JSON) |
| `compare_streams.py` | Subscribes on both brokers and compares topics, counts and payloads |
| `relay.py` | External two-way bridge with loop protection, for EMQX Serverless or when integrations can't change |

MQTT scripts need `pip install "paho-mqtt>=2.0"`.

## Install

### Claude Code (plugin)

```text
/plugin marketplace add connectmq/emqx-migration-skills
/plugin install emqx-migration-skills@connectmq-emqx-migration
```

### Claude Code (manual)

Copy the folders in `skills/` to `~/.claude/skills/` (all projects) or `.claude/skills/` (one project):

```bash
git clone https://github.com/connectmq/emqx-migration-skills
cp -r emqx-migration-skills/skills/* ~/.claude/skills/
```

### Claude.ai

Zip a skill folder (e.g. `skills/emqx-migration-planner`) and upload it under Settings > Capabilities > Skills.

### Other agents

Each skill is a self-contained folder with a `SKILL.md` (YAML front matter + instructions), `references/` and
`scripts/`. Agents that support the Agent Skills format can load the folders directly; for others, add the
`SKILL.md` content to the agent's instructions.

## Example prompts

- "We run a 3-node EMQX 5 cluster with 600 devices. Plan a move to ConnectMQ."
- "Convert our EMQX acl.conf and built-in database rules to ConnectMQ access control rules."
- "List every EMQX rule and data integration we use and propose a replacement for each."
- "Set up an EMQX MQTT bridge to our ConnectMQ broker for telemetry and commands."
- "We are on EMQX Serverless. Start a relay to ConnectMQ and compare both sides."
- "Consumers using $queue/jobs stopped receiving after the switch. Why?"

## How the migration works

1. **Plan**: audit, REST API inventory, plan limits, credentials/client IDs/ACLs, replacement for every rule.
2. **Bridge**: EMQX forwards device topics to ConnectMQ and pulls commands back (or `relay.py`).
3. **Consumers first**: replacement services and dashboards read from ConnectMQ, first in shadow mode.
4. **Devices in waves**: canary, then growing waves, each with go/no-go and rollback.
5. **Cutover**: remove the bridge, keep EMQX for the rollback window, decommission.

Key ConnectMQ facts the skills plan around: credentials are generated in the console and come with an issued client
ID (devices connect as `<ISSUED_ID>_<suffix>`), authentication is username/password over TLS (no client
certificates, no external auth backends), there is no broker-side rule engine (services subscribe with shared
subscriptions instead), per-topic ACL and shared subscriptions are on the Basic and Scale plans, and each plan caps
connections, session minutes, connection rate, message size, retained messages and session expiry. See
[plans and limits](https://github.com/connectmq/connectmq/wiki/Plans-and-Limits).

## Repository layout

```
.claude-plugin/              plugin + marketplace manifests (Claude Code)
skills/
  emqx-migration-planner/
    SKILL.md
    references/              connectmq-target.md, emqx-mapping.md, cutover-and-rollback.md
    assets/                  migration-plan-template.md
    scripts/                 emqx_audit.py, client_id_map.py, connectmq_acl.py
  emqx-migration-executor/
    SKILL.md
    references/              connectmq-target.md, emqx-bridge.md, cutover-and-rollback.md
    scripts/                 verify_connectmq.py, relay.py, copy_retained.py, compare_streams.py, ...
```

## Learn more

- Blog: [EMQX Migration Skills: Move from EMQX to ConnectMQ with an AI Agent](https://connectmq.com/blog/emqx-migration-skills/?utm_source=github&utm_medium=readme&utm_campaign=migration-skills&utm_content=emqx)
- [ConnectMQ docs (wiki)](https://github.com/connectmq/connectmq/wiki) · [MQTT code samples](https://github.com/connectmq/mqtt-code-samples)
- Start free (no credit card): [app.connectmq.com/signup](https://app.connectmq.com/signup?utm_source=github&utm_medium=readme&utm_campaign=migration-skills&utm_content=emqx)

## Contributing

Issues and pull requests are welcome: migration patterns, EMQX options or authorization sources the audit does not
know yet, fixes to the scripts. Please do not include real credentials or customer data in examples.

## License

MIT. EMQX is a trademark of EMQ Technologies; this project is not affiliated with or endorsed by EMQ Technologies.
