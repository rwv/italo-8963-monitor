#!/usr/bin/env python3
"""Monitor an Italo train and push updates to ntfy.sh.

Polls the public Italo "treno" status API and sends a notification whenever the
delay, projected arrival time, or platform changes for a chosen station.

Configuration via environment variables (all optional):
  TRAIN_NUMBER   train number to watch                 (default: 8963)
  STATION_CODE   station location code to track        (default: SMN = Firenze S.M.N.)
  NTFY_TOPIC     ntfy.sh topic to publish to           (default: italo-8963-monitor)
  POLL_INTERVAL  seconds between checks                 (default: 30)
  ALWAYS_NOTIFY  "1" to notify every poll, not just on change (default: 0)
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

TRAIN_NUMBER = os.environ.get("TRAIN_NUMBER", "8963")
STATION_CODE = os.environ.get("STATION_CODE", "SMN")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "italo-8963-monitor")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
ALWAYS_NOTIFY = os.environ.get("ALWAYS_NOTIFY", "0") == "1"

API_URL = (
    "https://italoinviaggio.italotreno.com/api/RicercaTrenoService?"
    + urllib.parse.urlencode({"TrainNumber": TRAIN_NUMBER})
)
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
TIMEOUT = 20


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_status():
    req = urllib.request.Request(API_URL, headers={"User-Agent": "italo-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def find_station(schedule, code):
    """Return the station dict matching `code`, searching stops then non-stops."""
    for key in ("StazioniFerme", "StazioniNonFerme"):
        for st in schedule.get(key, []) or []:
            if st.get("LocationCode") == code:
                return st
    return None


def parse_status(data):
    """Extract the fields we care about for the target station."""
    if data.get("IsEmpty", True):
        return None

    schedule = data.get("TrainSchedule") or {}
    disruption = schedule.get("Distruption") or {}
    leg = schedule.get("Leg") or {}
    station = find_station(schedule, STATION_CODE)

    delay = disruption.get("DelayAmount")

    # Prefer the Leg block when it targets our station (it carries the
    # delay-adjusted projected arrival); fall back to the station entry.
    if leg.get("ArrivalStation") == STATION_CODE:
        scheduled = leg.get("EstimatedArrivalTime")
        projected = leg.get("ActualArrivalTime")
        platform = leg.get("ActualArrivalPlatform")
    elif station:
        scheduled = station.get("EstimatedArrivalTime")
        projected = station.get("ActualArrivalTime")
        platform = station.get("ActualArrivalPlatform")
    else:
        scheduled = projected = platform = None

    station_name = (station or {}).get("LocationDescription") or leg.get(
        "ArrivalStationDescription"
    ) or STATION_CODE

    return {
        "delay": delay,
        "running_state": disruption.get("RunningState"),
        "scheduled": scheduled,
        "projected": projected,
        "platform": platform,
        "station_name": station_name,
        "origin": schedule.get("DepartureStationDescription"),
        "destination": schedule.get("ArrivalStationDescription"),
        "last_update": data.get("LastUpdate"),
    }


def delay_text(delay):
    if delay is None:
        return "ritardo n/d"
    if delay > 0:
        return f"+{delay} min in ritardo"
    if delay < 0:
        return f"{-delay} min in anticipo"
    return "in orario"


def build_message(s):
    platform = s["platform"] if s["platform"] not in (None, "") else "—"
    lines = [
        f"{delay_text(s['delay'])}",
        f"Arrivo {s['station_name']}: {s['projected'] or '?'} (orario {s['scheduled'] or '?'})",
        f"Binario: {platform}",
        f"Tratta: {s['origin']} → {s['destination']}",
        f"Aggiornato: {s['last_update']}",
    ]
    return "\n".join(lines)


def signature(s):
    """Fields whose change should trigger a new notification."""
    return (s["delay"], s["projected"], s["platform"], s["running_state"])


def send_ntfy(title, message, priority="default", tags=""):
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
    }
    req = urllib.request.Request(
        NTFY_URL, data=message.encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()


def main():
    log(
        f"Monitoring train {TRAIN_NUMBER} @ {STATION_CODE} -> {NTFY_URL} "
        f"every {POLL_INTERVAL}s (always_notify={ALWAYS_NOTIFY})"
    )
    last_sig = None
    notified_error = False

    while True:
        try:
            data = fetch_status()
            s = parse_status(data)
            notified_error = False

            if s is None:
                log("API returned empty (train not found / no data yet)")
                if last_sig != "EMPTY":
                    send_ntfy(
                        f"Italo {TRAIN_NUMBER}: nessun dato",
                        "Il treno non risulta al momento (orario non attivo?).",
                        tags="warning",
                    )
                    last_sig = "EMPTY"
            else:
                sig = signature(s)
                changed = sig != last_sig
                if changed or ALWAYS_NOTIFY:
                    has_platform = s["platform"] not in (None, "")
                    priority = "high" if has_platform else "default"
                    tags = "train" + (",checkered_flag" if has_platform else "")
                    title = f"Italo {TRAIN_NUMBER} · {delay_text(s['delay'])}"
                    send_ntfy(title, build_message(s), priority=priority, tags=tags)
                    log(f"Notified: {sig}")
                    last_sig = sig
                else:
                    log(f"No change: {sig}")

        except KeyboardInterrupt:
            log("Stopped.")
            return 0
        except Exception as e:  # network / parse errors: keep going
            log(f"Error: {e}")
            if not notified_error:
                try:
                    send_ntfy(
                        f"Italo {TRAIN_NUMBER}: errore monitor",
                        f"Errore nel recupero dati: {e}",
                        priority="low",
                        tags="warning",
                    )
                except Exception:
                    pass
                notified_error = True

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
