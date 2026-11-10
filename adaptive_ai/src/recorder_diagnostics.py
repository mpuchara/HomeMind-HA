"""Run five bounded, read-only Recorder probes from the HomeMind add-on."""
import argparse
from datetime import datetime, timedelta, timezone
import getpass
from http.client import HTTPException
import json
import os
import re
import sys
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MILIGHT = "light.milight_schody_gora"
START = "2026-11-05T05:53:43.619576+00:00"
END = "2026-11-05T06:16:13.619577+00:00"
FLAGS = ("minimal_response", "no_attributes", "significant_changes_only", "skip_initial_state")
SUPERVISOR = "http://supervisor/core/api"
CORE = "http://homeassistant:8123/api"
BODY_LIMIT = 8 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(opener, row, token):
    query = {"filter_entity_id": row["entity_id"], "end_time": row["end_time"]}
    query.update(dict.fromkeys(row["flags"], ""))
    base = SUPERVISOR if row["route"] == "supervisor" else CORE
    path = "history/period/" + quote(row["start_time"], safe="") + "?" + urlencode(query)
    request = Request(base + "/" + path, headers={"Authorization": "Bearer " + token})
    row["request_started_utc"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    started = monotonic()
    try:
        with opener.open(request, timeout=20) as response:
            row["http"] = response.status
            body = response.read(BODY_LIMIT + 1)
        if len(body) > BODY_LIMIT:
            row["error"] = "body_limit"
        elif row["http"] == 200:
            history = json.loads(body)
            if isinstance(history, list) and all(isinstance(series, list) for series in history):
                row["records"] = sum(map(len, history))
            else:
                row["error"] = "unexpected_history_shape"
    except HTTPError as exc:
        row["http"] = exc.code
        exc.close()
    except (OSError, HTTPException, ValueError) as exc:
        row["error"] = type(exc).__name__
    row["duration_ms"] = round((monotonic() - started) * 1000)
    return row


def run(other_entity, supervisor_token, core_token=None):
    now = datetime.now(timezone.utc)
    cases = (
        ("A", MILIGHT, (), START, END, "supervisor"),
        ("B", MILIGHT, FLAGS, START, END, "supervisor"),
        ("C", other_entity, FLAGS, START, END, "supervisor"),
        ("D", MILIGHT, FLAGS, (now - timedelta(hours=1)).isoformat(), now.isoformat(), "supervisor"),
        ("E", MILIGHT, FLAGS, START, END, "core"),
    )
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for name, entity, flags, start, end, route in cases:
        row = dict(test=name, entity_id=entity, flags=flags, start_time=start, end_time=end,
                   route=route, http=None, duration_ms=None)
        token = supervisor_token if route == "supervisor" else core_token
        if not token:
            row["skipped"] = "missing_core_token" if route == "core" else "missing_supervisor_token"
            yield row
        else:
            yield probe(opener, row, token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("other_entity", help="Different entity with confirmed Recorder history in the historical window")
    parser.add_argument("--direct-core", action="store_true", help="Prompt for a separate HA token to run probe E")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", args.other_entity) or args.other_entity == MILIGHT:
        parser.error("other_entity must be a different Home Assistant entity ID")
    core_token = None
    if args.direct_core:
        if not sys.stdin.isatty():
            parser.error("--direct-core requires an interactive terminal")
        core_token = getpass.getpass("Home Assistant token for direct Core probe (E): ").strip()
    for result in run(args.other_entity, os.environ.get("SUPERVISOR_TOKEN"), core_token):
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
