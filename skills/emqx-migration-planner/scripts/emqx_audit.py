#!/usr/bin/env python3
"""Audits an EMQX installation for a migration to ConnectMQ.

Reads any combination of:

  --conf     emqx.conf (EMQX 5 HOCON or EMQX 4 key = value); known keys are mapped to ConnectMQ
  --acl      acl.conf (Erlang terms, EMQX 4 or 5 syntax); converted to ConnectMQ access control rules
  --builtin  JSON exported from the EMQX 5 REST API for the built_in_database authorization source
             (GET /api/v5/authorization/sources/built_in_database/rules/users, .../rules/clients, .../rules/all);
             repeat the flag for several files
  --clients  JSON from GET /api/v5/clients (one or more pages) -> clients.csv with client IDs and usernames

and writes:

  <out>/audit.md          findings: settings mapped to ConnectMQ, blockers and actions
  <out>/acl-rules.md/.csv/.json   ConnectMQ rules (order preserved: EMQX file authorization is first-match too)
  <out>/users.csv         usernames found in rules/clients with an empty column for the new ConnectMQ username
  <out>/clients.csv       client IDs (when --clients is given), input for client_id_map.py

Examples:
  python emqx_audit.py --conf /etc/emqx/emqx.conf --acl /etc/emqx/acl.conf --out migration/
  python emqx_audit.py --builtin users.json --builtin clients.json --builtin all.json --out migration/
  python emqx_audit.py --acl acl.conf --client-map client-map.csv --user-map users.csv --out migration/

--client-map: CSV old_client_id,new_client_id (from client_id_map.py). Rules for a client ID get the new ID as
subject; ${clientid}/%c topic rules are expanded per client so topics keep the old client ID.
--user-map:   CSV old_username,new_username.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from connectmq_acl import Rule, check, expand_pattern, load_map, render  # noqa: E402


# --------------------------------------------------------------------------------------------------------------
# Minimal Erlang term parser (tuples, lists, strings, atoms, numbers, binaries <<"...">>)

class Atom(str):
    pass


TOKEN = re.compile(r'\s+|%[^\n]*|<<"(?:\\.|[^"\\])*">>|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}\[\],.]|[^\s{}\[\],"\'%]+')


def tokenize(text: str) -> list[str]:
    out, pos = [], 0
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"cannot parse near: {text[pos:pos + 40]!r}")
        tok = m.group(0)
        pos = m.end()
        if tok.isspace() or tok.startswith("%"):
            continue
        out.append(tok)
    return out


def parse_terms(text: str) -> list:
    toks = tokenize(text)
    i = 0

    def value():
        nonlocal i
        t = toks[i]
        i += 1
        if t == "{":
            items = seq("}")
            return tuple(items)
        if t == "[":
            return seq("]")
        if t.startswith('<<"'):
            return bytes(t[3:-3], "utf-8").decode("unicode_escape")
        if t.startswith('"'):
            return t[1:-1].encode("utf-8").decode("unicode_escape")
        if t.startswith("'"):
            return Atom(t[1:-1])
        if re.fullmatch(r"-?\d+", t):
            return int(t)
        return Atom(t)

    def seq(close):
        nonlocal i
        items = []
        if toks[i] == close:
            i += 1
            return items
        while True:
            items.append(value())
            t = toks[i]
            i += 1
            if t == close:
                return items
            if t != ",":
                raise ValueError(f"expected ',' or '{close}', got {t!r}")

    terms = []
    while i < len(toks):
        terms.append(value())
        if i < len(toks) and toks[i] == ".":
            i += 1
        else:
            raise ValueError("expected '.' after term")
    return terms


# --------------------------------------------------------------------------------------------------------------
# acl.conf -> ConnectMQ rules

UNSUPPORTED_PLACEHOLDER = re.compile(r"\$\{(?!clientid\}|username\})[^}]*\}")


def norm_topic(topic: str) -> str:
    return topic.replace("%c", "${clientid}").replace("%u", "${username}")


def regex_to_glob(rx: str) -> str | None:
    """Converts simple regexes (^abc$, ^abc.*$, ^.*abc$) to a ConnectMQ glob; None when not possible."""
    body = rx
    anchored_start = body.startswith("^")
    anchored_end = body.endswith("$") and not body.endswith("\\$")
    body = body.lstrip("^").rstrip("$") if anchored_end else body.lstrip("^")
    glob = body.replace(".*", "\x00")
    if re.search(r"[\\.()\[\]{}+?|]", glob):
        return None
    glob = glob.replace("\x00", "*")
    if not anchored_start:
        glob = "*" + glob
    if not anchored_end:
        glob = glob + "*"
    return glob


def convert_who(who, notes) -> list[tuple[str, str]] | None:
    """Returns [(subject_type, subject_value)] or None when the subject cannot be expressed."""
    if isinstance(who, str) and who == "all":
        return [("all", "")]
    if isinstance(who, tuple) and len(who) == 2:
        kind, val = who
        if kind in ("username", "user", "clientid", "client"):
            st = "username" if kind in ("username", "user") else "clientid"
            if isinstance(val, str):
                return [(st, val)]
            if isinstance(val, tuple) and len(val) == 2 and val[0] == "re":
                g = regex_to_glob(val[1])
                if g is None:
                    notes.append(f"regex subject {who!r} cannot be converted to a glob - rule skipped, review manually")
                    return None
                return [(st, g)]
        if kind in ("ipaddr", "ipaddrs"):
            notes.append(f"IP-address subject {who!r} has no ConnectMQ equivalent - rule skipped")
            return None
        if kind == "or" and isinstance(val, list):
            out = []
            for w in val:
                r = convert_who(w, notes)
                if r is None:
                    return None
                out += r
            return out
        if kind == "and" and isinstance(val, list):
            parts = [convert_who(w, []) for w in val]
            names = [p for p in parts if p and p[0][0] != "all"]
            if len(val) == 1 and parts[0]:
                return parts[0]
            notes.append(f"'and' subject {who!r} (e.g. username AND ipaddr) cannot be expressed - rule skipped"
                         + ("; nearest single subject: " + repr(names[0]) if names else ""))
            return None
    notes.append(f"unknown subject {who!r} - rule skipped")
    return None


def convert_action(action, notes, where) -> str | None:
    if isinstance(action, str) and action == "pubsub":  # EMQX 4 spelling of "all"
        return "all"
    if isinstance(action, str) and action in ("publish", "subscribe", "all"):
        return str(action)
    if isinstance(action, tuple) and len(action) == 2 and action[0] in ("publish", "subscribe", "all"):
        notes.append(f"{where}: QoS/retain conditions {action[1]!r} dropped (ConnectMQ rules match topic only)")
        return str(action[0])
    notes.append(f"{where}: unknown action {action!r} - rule skipped")
    return None


def acl_conf_to_rules(path: str, notes: list[str]) -> tuple[list[Rule], str | None]:
    with open(path, encoding="utf-8", errors="replace") as f:
        terms = parse_terms(f.read())
    rules: list[Rule] = []
    no_match = None
    for n, term in enumerate(terms, 1):
        where = f"acl.conf rule {n}"
        if not isinstance(term, tuple) or not term or term[0] not in ("allow", "deny"):
            notes.append(f"{where}: unrecognised term {term!r} - skipped")
            continue
        perm = str(term[0])
        if len(term) == 2 and term[1] == "all":
            if n == len(terms):
                no_match = perm
                notes.append(f"final catch-all {{{perm}, all}} mapped to the ConnectMQ no-match policy '{perm}'")
            else:
                rules.append(Rule(perm, "all", "#", "all", "", f"{where}: {{{perm}, all}}"))
            continue
        if len(term) != 4:
            notes.append(f"{where}: unexpected arity in {term!r} - skipped")
            continue
        _, who, action, topics = term
        sub_notes: list[str] = []
        subjects = convert_who(who, sub_notes)
        notes += [f"{where}: {x}" for x in sub_notes]
        act = convert_action(action, notes, where)
        if subjects is None or act is None:
            continue
        if not isinstance(topics, list):
            topics = [topics]
        for t in topics:
            exact = False
            if isinstance(t, tuple) and len(t) == 2 and t[0] == "eq":
                t, exact = t[1], True
            if not isinstance(t, str):
                notes.append(f"{where}: unsupported topic term {t!r} - skipped")
                continue
            topic = norm_topic(t)
            if topic.startswith("$SYS"):
                notes.append(f"{where}: $SYS rule '{t}' skipped (ConnectMQ does not publish $SYS)")
                continue
            if UNSUPPORTED_PLACEHOLDER.search(topic):
                notes.append(f"{where}: placeholder in '{t}' is not supported (only ${{clientid}}/${{username}}) - skipped")
                continue
            warnings = []
            if exact and re.search(r"(^|/)[+#](/|$)", topic):
                if perm == "deny":
                    notes.append(f"{where}: {{deny, ..., [{{eq, \"{t}\"}}]}} blocks only the literal filter '{t}'. "
                                 "ConnectMQ would deny every filter under it - skipped. Restrict wildcard "
                                 "subscriptions with allow rules + no-match deny instead.")
                    continue
                warnings.append(f"{{eq, \"{t}\"}} allowed only the literal filter; ConnectMQ allows every filter it covers")
            for st, sv in subjects:
                rules.append(Rule(perm, act, topic, st, sv, f"{where}: {perm} {act} {t}", list(warnings)))
    return rules, no_match


def builtin_to_rules(paths: list[str], notes: list[str]) -> list[Rule]:
    """EMQX 5 built_in_database export. Accepts the REST API page object ({"data": [...]}) or a list, with
    items {"username"|"clientid": ..., "rules": [{"topic", "permission", "action", ...}]}, or {"rules": [...]}
    for the 'all' subject."""
    groups = {"username": [], "clientid": [], "all": []}
    for p in paths:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(items, dict) and "rules" in items:
            items = [items]
        for item in items:
            if "username" in item:
                st, sv = "username", item["username"]
            elif "clientid" in item:
                st, sv = "clientid", item["clientid"]
            else:
                st, sv = "all", ""
            for r in item.get("rules", []):
                topic = norm_topic(r.get("topic", ""))
                act = r.get("action", "all")
                where = f"built_in_database {st} {sv or '*'}"
                extra = {k: v for k, v in r.items() if k in ("qos", "retain") and v not in (None, "all", [0, 1, 2])}
                warnings = [f"QoS/retain condition {extra} dropped"] if extra else []
                if topic.startswith("$SYS"):
                    notes.append(f"{where}: $SYS rule skipped")
                    continue
                groups[st].append(Rule(r.get("permission", "allow"), act, topic, st, sv,
                                       f"{where}: {r.get('permission')} {act} {r.get('topic')}", warnings))
    notes.append("built_in_database rules ordered username -> clientid -> all, each in export order. Check that "
                 "this matches how your EMQX version evaluated them.")
    return groups["username"] + groups["clientid"] + groups["all"]


def apply_maps(rules: list[Rule], client_map: dict, user_map: dict) -> list[Rule]:
    out = []
    for r in rules:
        old_user = r.subject_value
        if r.subject_type == "username":
            if r.subject_value in user_map:
                r.subject_value = user_map[r.subject_value]
            elif "*" not in r.subject_value:
                r.warnings.append(f"old username '{r.subject_value}': replace with its ConnectMQ username")
        if r.subject_type == "clientid":
            if "*" not in r.subject_value and "${clientid}" in r.topic_filter.lower():
                r.topic_filter = re.sub(r"\$\{clientid\}", lambda _m, v=r.subject_value: v, r.topic_filter,
                                        flags=re.IGNORECASE)
            if r.subject_value in client_map:
                r.subject_value = client_map[r.subject_value]
            elif "*" not in r.subject_value:
                r.warnings.append(f"old client ID '{r.subject_value}': use its ConnectMQ client ID (<ISSUED>_<old>)")
        expanded = [r]
        if "${clientid}" in r.topic_filter:
            if client_map and r.subject_type == "all":
                expanded = expand_pattern(r, client_map, "${clientid}")
            elif not client_map:
                r.warnings.append("${clientid} is now the full ConnectMQ ID (<ISSUED>_<old>); pass --client-map to "
                                  "keep old IDs in topics")
        if "${username}" in r.topic_filter and r.subject_type == "username" and "*" not in r.subject_value:
            r.topic_filter = r.topic_filter.replace("${username}", old_user)
        elif "${username}" in r.topic_filter and r.subject_type != "all":
            r.warnings.append("${username} is now the generated ConnectMQ username; replace with explicit topics "
                              "or keep the new usernames in the topic tree")
        if "${username}" in r.topic_filter and r.subject_type == "all":
            if user_map:
                tmp = []
                for e in expanded:
                    for x in expand_pattern(e, user_map, "${username}"):
                        if x is not e:
                            x.subject_type = "username"
                        tmp.append(x)
                expanded = tmp
            else:
                r.warnings.append("${username} is now the generated ConnectMQ username; pass --user-map")
        out += expanded
    seen, res = set(), []
    for r in out:
        if r.key() not in seen:
            seen.add(r.key())
            res.append(r)
    return res


# --------------------------------------------------------------------------------------------------------------
# emqx.conf report

def flatten_conf(path: str) -> list[tuple[str, str]]:
    """Flattens HOCON blocks (key { ... }) and key = value / key: value lines into dotted keys."""
    out, stack = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = re.sub(r"\s(#|//).*$", "", raw).strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            while line:
                m = re.match(r'^"?([\w.\-]+)"?\s*\{\s*(.*)$', line)
                if m:
                    stack.append(m.group(1))
                    line = m.group(2).strip()
                    continue
                if line.startswith("}"):
                    if stack:
                        stack.pop()
                    line = line[1:].strip().lstrip(",").strip()
                    continue
                m = re.match(r'^"?([\w.\-]+)"?\s*[=:]\s*(.*)$', line)
                if m:
                    key, rest = m.group(1), m.group(2)
                    # value ends at ',' or '}' when inline, unless quoted/array
                    vm = re.match(r'^("(?:\\.|[^"])*"|\[[^\]]*\]|[^,}]*)\s*,?\s*(.*)$', rest)
                    value, line = vm.group(1).strip(), vm.group(2).strip()
                    out.append((".".join(stack + [key]), value))
                    continue
                break
    return out


# (regex on flattened key, severity, text)
CONF_RULES = [
    (r"quic", "blocker", "MQTT over QUIC is not supported; clients move to MQTT over TLS (8883)."),
    (r"(verify\s*=\s*verify_peer|\.verify$|peer_cert_as_username|peer_cert_as_clientid|fail_if_no_peer_cert)", "blocker", "Client certificate (mTLS) authentication is not supported; move devices to username/password over TLS."),
    (r"max_connections$", "check", "Compare with the plan's concurrent connections (Free 5, Basic 100, Scale 1,000)."),
    (r"^listeners?\.(tcp|ssl|ws|wss|quic)", "info", "ConnectMQ listens on 8883 (TLS), 8084 (wss, /mqtt), 1883 and 8083 (tests only). Clients change host and port."),
    (r"^listener\.(tcp|ssl|ws|wss)\.", "info", "ConnectMQ listens on 8883 (TLS), 8084 (wss, /mqtt), 1883 and 8083 (tests only)."),
    (r"max_packet_size$", "check", "Plan max message size (256 KB on Free/Basic)."),
    (r"max_inflight$", "info", "ConnectMQ sets receive-maximum per plan; no client change needed."),
    (r"max_mqueue_len$", "check", "Offline queueing lasts only as long as the session (plan max 1 h / 2 h / 24 h)."),
    (r"session_expiry_interval$", "check", "ConnectMQ caps session expiry per plan (Free 1 h, Basic 2 h, Scale 24 h)."),
    (r"(server_keepalive|max_keepalive|keepalive_multiplier|keepalive_backoff)$", "info", "ConnectMQ caps keep-alive at 600 s."),
    (r"max_clientid_len$", "info", "ConnectMQ client IDs are <ISSUED_ID>_<suffix>; keep suffixes short."),
    (r"max_topic_levels$", "info", "Check deep topic trees against the target; keep levels reasonable."),
    (r"max_subscriptions$", "check", "Plan subscriptions per client (20 / 50 / 100)."),
    (r"retain_available$", "info", "Retained messages are available; the plan caps the count (100 / 1,000 / 10,000)."),
    (r"(shared_subscription|shared_subscription_strategy)$", "check", "Shared subscriptions ($share/<group>/...) need Basic or Scale; $queue/ must become $share/<group>/."),
    (r"wildcard_subscription$", "info", "Wildcard subscriptions are available on every plan."),
    (r"allow_anonymous$", "action", "ConnectMQ has no anonymous access: every client needs credentials."),
    (r"^authentication", "action", "EMQX authenticators (built-in DB, HTTP, JWT, MySQL, PostgreSQL, MongoDB, Redis, LDAP, SCRAM) are replaced by ConnectMQ credentials issued in the console."),
    (r"^auth\.", "action", "EMQX 4 auth plugins are replaced by ConnectMQ credentials issued in the console."),
    (r"^authorization\.no_match$", "action", "Maps to the ConnectMQ ACL no-match policy."),
    (r"^acl_nomatch$", "action", "Maps to the ConnectMQ ACL no-match policy."),
    (r"(deny_action|acl_deny_action)$", "info", "ConnectMQ answers denied publish/subscribe with reason code 0x87 and keeps the connection (like deny_action = ignore)."),
    (r"^authorization\.sources", "action", "Authorization sources: file/built_in_database are converted by this script; HTTP/MySQL/PostgreSQL/MongoDB/Redis sources must be exported and converted manually."),
    (r"^acl_file$", "action", "Convert with --acl."),
    (r"^authorization\.cache", "info", "ConnectMQ refreshes ACL rules automatically; no cache setting."),
    (r"^(rule_engine|rules|bridges|connectors|actions|sources)\b", "blocker", "Rule engine / data integration does not exist on ConnectMQ: replace each rule/action with a backend subscriber (shared subscription on Basic/Scale), or keep EMQX bridging to ConnectMQ during the parallel run."),
    (r"^(retainer)", "check", "Retained storage is managed; plan caps the retained count. Copy retained messages with copy_retained.py."),
    (r"^delayed", "blocker", "Delayed publish ($delayed/<seconds>/topic) is not supported; schedule in the publisher or a backend."),
    (r"^rewrite", "action", "Topic rewrite is not supported; change topics in clients, or rewrite in the bridge/relay during the parallel run."),
    (r"^auto_subscribe", "action", "Auto-subscribe is not supported; clients must subscribe after connecting."),
    (r"^exhook", "blocker", "ExHook gRPC hooks are not supported; move the logic into a client-side service."),
    (r"^(webhook|web_hook|bridges\.webhook)", "blocker", "Webhooks are not supported; use a backend subscriber that calls the HTTP endpoint."),
    (r"^slow_subs", "info", "Use the ConnectMQ dashboard and sessions view for diagnostics."),
    (r"^topic_metrics", "info", "Use the ConnectMQ dashboard topic monitor."),
    (r"^(plugins|modules)", "check", "EMQX plugins/modules do not run on ConnectMQ; review each one."),
    (r"^(node|cluster|rpc|db)\.", "info", "Clustering is managed by ConnectMQ; not applicable."),
    (r"^(dashboard|api_key)", "info", "Replaced by the ConnectMQ console."),
    (r"^(flapping_detect|force_shutdown|force_gc|overload_protection|sysmon|limiter)", "info", "Protection limits are managed by ConnectMQ (connection rate, per-client message rate per plan)."),
    (r"^(log|prometheus|opentelemetry|statsd)", "info", "Use the ConnectMQ console for monitoring; EMQX exporters stop working."),
    (r"^sys_topics", "info", "$SYS topics are not provided; use the ConnectMQ dashboard."),
    (r"(^|\.)mountpoint$", "action", "Listener mountpoints prefix topics; add the prefix to the topic tree or strip it in the bridge."),
]


def report_conf(path: str) -> tuple[dict, dict]:
    findings = {k: [] for k in ("blocker", "action", "check", "info", "unknown")}
    found = {}
    reported_prefix = set()
    for key, value in flatten_conf(path):
        found[key] = value
        line = f"{key} = {value}"
        matched = False
        for rx, sev, text in CONF_RULES:
            if re.search(rx, key) or re.search(rx, line):
                if sev == "blocker" and "verify" in rx and not re.search(r"verify_peer|true", value):
                    sev, text = "info", "Client certificates not required on the source; nothing to migrate."
                if key.endswith("allow_anonymous") and value.strip('"').lower() == "false":
                    sev, text = "info", "Disabled on the source; nothing to migrate."
                group = rx + "|" + ".".join(key.split(".")[:2])
                if sev in ("blocker", "action") and group in reported_prefix and not key.endswith(("no_match", "acl_nomatch", "allow_anonymous")):
                    matched = True
                    break
                reported_prefix.add(group)
                shown = "***" if re.search(r"pass|secret|cookie|key$", key) else value
                findings[sev].append(f"`{key} = {shown}`: {text}")
                matched = True
                break
        if not matched and re.match(r"^(mqtt|zone|listener|listeners|authentication|authorization)", key):
            findings["unknown"].append(f"`{key} = {value}`: review manually.")
    return findings, found


# --------------------------------------------------------------------------------------------------------------

def read_clients(paths: list[str]) -> list[dict]:
    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        for c in data.get("data", data) if isinstance(data, dict) else data:
            rows.append({"client_id": c.get("clientid", ""), "username": c.get("username") or "",
                         "proto_ver": c.get("proto_ver", ""), "connected": c.get("connected", ""),
                         "ip_address": c.get("ip_address", ""), "subscriptions_cnt": c.get("subscriptions_cnt", ""),
                         "mqueue_len": c.get("mqueue_len", "")})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", help="emqx.conf (HOCON or EMQX 4 format)")
    ap.add_argument("--acl", help="acl.conf (Erlang terms)")
    ap.add_argument("--builtin", action="append", default=[], help="built_in_database rules JSON (repeatable)")
    ap.add_argument("--clients", action="append", default=[], help="/api/v5/clients JSON page (repeatable)")
    ap.add_argument("--client-map", help="CSV old_client_id,new_client_id")
    ap.add_argument("--user-map", help="CSV old_username,new_username")
    ap.add_argument("--no-match", choices=("allow", "deny"), help="Override the ConnectMQ no-match policy")
    ap.add_argument("--out", default="migration")
    args = ap.parse_args()
    if not (args.conf or args.acl or args.builtin or args.clients):
        ap.error("give --conf, --acl, --builtin or --clients")
    os.makedirs(args.out, exist_ok=True)

    client_map = load_map(args.client_map, "old_client_id", "new_client_id")
    user_map = {k: v for k, v in load_map(args.user_map, "old_username", "new_username").items() if v}

    findings, conf = ({k: [] for k in ("blocker", "action", "check", "info", "unknown")}, {})
    if args.conf:
        findings, conf = report_conf(args.conf)

    notes: list[str] = []
    rules: list[Rule] = []
    no_match = args.no_match
    conf_no_match = next((v.strip('"') for k, v in conf.items() if k in ("authorization.no_match", "acl_nomatch")), None)
    if args.acl:
        r, nm = acl_conf_to_rules(args.acl, notes)
        rules += r
        no_match = no_match or nm
    if args.builtin:
        rules += builtin_to_rules(args.builtin, notes)
    no_match = no_match or conf_no_match or "deny"
    if no_match == "allow" and rules:
        notes.append("No-match policy is 'allow' (EMQX default). Consider 'deny' on ConnectMQ with explicit allow "
                     "rules: with 'allow', any topic not covered by a deny rule is open to every client.")
    rules = apply_maps(rules, client_map, user_map)

    users = sorted({r.subject_value for r in rules if r.subject_type == "username" and "*" not in r.subject_value
                    and r.subject_value not in user_map.values()} | set())
    clients = read_clients(args.clients) if args.clients else []
    users = sorted(set(users) | {c["username"] for c in clients if c["username"]})

    problems = check(rules)
    if rules:
        for fmt, ext in (("markdown", "md"), ("csv", "csv"), ("json", "json")):
            with open(os.path.join(args.out, f"acl-rules.{ext}"), "w", encoding="utf-8", newline="") as f:
                f.write(render(rules, no_match, fmt, notes + problems))
    if users:
        with open(os.path.join(args.out, "users.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["old_username", "new_username", "credential_scope"])
            for u in users:
                w.writerow([u, user_map.get(u, ""), ""])
    if clients:
        with open(os.path.join(args.out, "clients.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(clients[0].keys()))
            w.writeheader()
            w.writerows(clients)
        v3 = sum(1 for c in clients if str(c["proto_ver"]) == "3")
        if v3:
            findings["action"].append(f"{v3} client(s) use MQTT 3.1 (proto_ver 3): upgrade to 3.1.1 or 5.")
        queued = [c for c in clients if isinstance(c["mqueue_len"], int) and c["mqueue_len"] > 0]
        if queued:
            findings["check"].append(f"{len(queued)} client(s) have queued messages: drain or accept loss at cutover.")

    lines = ["# EMQX -> ConnectMQ audit", ""]
    if args.conf:
        lines.append(f"Configuration: `{args.conf}` ({len(conf)} settings)")
    lines += [f"acl.conf: `{args.acl or '-'}`",
              f"built_in_database exports: {', '.join(f'`{p}`' for p in args.builtin) or '-'}",
              f"Clients: {len(clients)}", ""]
    titles = {"blocker": "Blockers (must be redesigned)", "action": "Actions (change during migration)",
              "check": "Check against the ConnectMQ plan", "info": "Information", "unknown": "Not recognised (review)"}
    for sev in ("blocker", "action", "check", "info", "unknown"):
        if findings[sev]:
            lines += [f"## {titles[sev]}", ""] + [f"- {x}" for x in findings[sev]] + [""]
    lines += ["## Access control", "",
              (f"{len(rules)} ConnectMQ rules generated -> `acl-rules.md` (no-match policy **{no_match}**)."
               if rules else "No ACL rules converted."), ""]
    if notes or problems:
        lines += ["### Notes", ""] + [f"- {n}" for n in notes + problems] + [""]
    lines += ["## Next steps", "",
              "1. Fill `users.csv` new_username after issuing ConnectMQ credentials, then re-run with --user-map.",
              "2. Build the client map: `python client_id_map.py clients.csv --issued-id <ID> -o client-map.csv`.",
              "3. Pull /api/v5/stats and /api/v5/metrics over a busy day to size the plan.",
              "4. List every rule/data integration and decide its replacement before scheduling the first wave.", ""]
    with open(os.path.join(args.out, "audit.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"wrote {args.out}/audit.md ({sum(len(v) for v in findings.values())} findings, "
          f"{len(findings['blocker'])} blockers), {len(rules)} ACL rules, {len(users)} users, {len(clients)} clients")
    return 0


if __name__ == "__main__":
    sys.exit(main())
