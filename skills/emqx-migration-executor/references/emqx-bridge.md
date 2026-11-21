# Bridging EMQX to ConnectMQ

ConnectMQ does not open bridges itself, so the bridge runs on EMQX (or as `relay.py` next to it). Every bridge
connection to ConnectMQ counts toward the plan's concurrent connections.

EMQX renamed and restructured its bridge feature several times. Use the section for your version and confirm field
names in that version's documentation; the values to enter are the same everywhere:

| Setting | Value for ConnectMQ |
| --- | --- |
| Server | `BROKER_HOST:8883` |
| TLS | Enabled, verify the server certificate (public CA), SNI = `BROKER_HOST` |
| Username / password | Credential issued in the ConnectMQ console |
| Client ID / ClientID prefix | Must start with the issued client ID: `ISSUED_ID_bridge` (prefix `ISSUED_ID_`) |
| Protocol | MQTT v5 or v3.1.1 |
| Clean start | `false` for a persistent session (QoS 1 survives short reconnects) |
| Keepalive | 60 s (ConnectMQ caps at 600 s) |
| Pool size / connections | 1 (each pooled connection uses a ConnectMQ connection and must have a unique ID) |
| QoS | 1 |

## EMQX 5: MQTT connector + rule + action (egress) / source (ingress)

In EMQX 5.4 and later, Dashboard **Integration**:

1. **Connector**: create an *MQTT Broker* connector with the values above. Test connectivity.
2. **Egress (EMQX -> ConnectMQ)**: create a **rule** that selects the forward topics, with an *MQTT Broker* action
   (sink) using the connector:

   ```sql
   SELECT * FROM "devices/+/telemetry", "devices/+/status", "devices/+/events"
   ```

   Action settings: remote topic `${topic}`, QoS `${qos}`, retain `${flag.retain}`, payload `${payload}`.
3. **Ingress (ConnectMQ -> EMQX)**: create an *MQTT Broker* **source** on the same connector subscribing to the
   reverse topics on ConnectMQ (e.g. `devices/+/commands/#`, QoS 1), and a rule that republishes the source's
   messages locally under the same topic.

In EMQX 5.0-5.3 the same thing is one **Data Bridge** of type MQTT with *ingress* and *egress* sections (remote
topic, local topic, QoS, retain, payload templates). The rule SQL for egress uses the bridge as its output.

EMQX builds the connection's client ID from the configured ClientID prefix plus an internal suffix. Set the prefix to
`ISSUED_ID_` and check the actual ID in the ConnectMQ console sessions view: it must start with `ISSUED_ID_`, or
ConnectMQ refuses it with `0x85`.

## EMQX 4: MQTT bridge plugin or rule-engine resource

- Open source 4.x: the `emqx_bridge_mqtt` plugin (`etc/plugins/emqx_bridge_mqtt.conf`). Its settings include the
  remote address, `proto_ver`, `clientid`, `username`, `password`, `ssl` + CA file, `forwards` (local topics sent to
  the remote) and `subscription.N.topic` / `.qos` (remote topics pulled in). Enable with
  `emqx_ctl plugins load emqx_bridge_mqtt`.
- Enterprise 4.x: rule engine *resource* of type MQTT Bridge plus a rule with the *Data bridge to MQTT Broker*
  action.

Use the same values as above; set `forwards` to the forward filters and the subscriptions to the reverse filters.

## EMQX Cloud

- **Dedicated** deployments with data integration: configure the MQTT bridge as in the EMQX 5 section from the
  Cloud console (availability depends on the tier).
- **Serverless**: no outbound bridge. Run `relay.py` with an EMQX Serverless credential as the source:

```bash
export SOURCE_USERNAME=... SOURCE_PASSWORD=...
export CONNECTMQ_USERNAME=... CONNECTMQ_PASSWORD=... CONNECTMQ_CLIENT_ID=...
python scripts/relay.py --source mqtts://YOUR_DEPLOYMENT.emqxsl.com:8883 --source-client-id connectmq-relay \
  --target mqtts://BROKER_HOST:8883 \
  --forward 'devices/+/telemetry' --forward 'devices/+/status' --reverse 'devices/+/commands/#'
```

The same relay works for any EMQX where you cannot change integrations. Run it under a supervisor (systemd, Docker
`restart: always`); it prints counters every 30 s and protects against loops (non-overlapping filters, MQTT 5
`x-relay` user property, no-local).

## ACL rules for the bridge on ConnectMQ

Add before enabling ACL (replace the client ID with the bridge's actual ID or a glob for pooled IDs):

| Permission | Action | Topic filter | Subject | Value |
| --- | --- | --- | --- | --- |
| allow | publish | `devices/+/telemetry` | clientid | `ISSUED_ID_bridge*` |
| allow | publish | `devices/+/status` | clientid | `ISSUED_ID_bridge*` |
| allow | publish | `devices/+/events` | clientid | `ISSUED_ID_bridge*` |
| allow | subscribe | `devices/+/commands/#` | clientid | `ISSUED_ID_bridge*` |

## Loop and duplicate rules

- Forward and reverse filters must not overlap. If a topic is written from both sides, split it by direction first.
- Do not forward `#`: it would include reverse traffic republished locally and loop it back.
- One bridge per EMQX cluster, not one per node with the same client ID: two connections with one client ID take
  over each other's session (DISCONNECT `0x8E`).
- If EMQX already bridges to another system, keep that bridge; it is independent of the ConnectMQ bridge.

## Checking the bridge

```bash
# Rule/action metrics: Dashboard Integration > Rules (matched / success / failed), or
curl -s -u "$EMQX_API_KEY:$EMQX_API_SECRET" http://EMQX_HOST:18083/api/v5/rules
# Both sides
python scripts/compare_streams.py --source mqtt://EMQX_HOST:1883 --target mqtts://BROKER_HOST:8883 \
  --filter 'devices/#' --duration 300
```

Rule `failed` counters rising usually mean ACL denies on ConnectMQ (`0x87`), an oversized payload (`0x97`) or a
dropped connector.

## Removing the bridge at cutover

1. Confirm no device publishes forward topics on EMQX (`compare_streams.py` shows no source traffic, EMQX
   `connections.count` is only the bridge and services).
2. Disable then delete the rule(s), actions/sources and the connector (or unload the 4.x plugin / stop the relay).
3. Remove the bridge ACL rules and credential on ConnectMQ.
