#!/usr/bin/env python3
"""Monitor an Italo train and push updates to ntfy.sh.

Polls the public Italo "treno" status API and sends a notification with the
delay, projected arrival time, and platform for a chosen station.

Configuration via environment variables (all optional):
  TRAIN_NUMBER        train number to watch                       (default: 8963)
  STATION_CODE        station location code to track              (default: SMN = Firenze S.M.N.)
  NTFY_TOPIC          ntfy.sh topic to publish to                 (default: italo-8963-monitor)
  POLL_INTERVAL       seconds between checks                      (default: 30)
  ALWAYS_NOTIFY       "1" to notify every poll, not just on change(default: 0)
  STOP_AFTER_SECONDS  stop after N seconds, 0 = no limit          (default: 0)
  STOP_ON_DEPARTURE   "1" to stop once the train departs STATION  (default: 0)
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
STOP_AFTER_SECONDS = int(os.environ.get("STOP_AFTER_SECONDS", "0"))
STOP_ON_DEPARTURE = os.environ.get("STOP_ON_DEPARTURE", "0") == "1"

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


def all_stations(schedule):
    out = []
    for key in ("StazioniFerme", "StazioniNonFerme"):
        out.extend(schedule.get(key, []) or [])
    return out


def find_station(schedule, code):
    for st in all_stations(schedule):
        if st.get("LocationCode") == code:
            return st
    return None


def real_passed(st):
    """Whether a station has a *genuine* (delay-adjusted) measurement.

    The API pre-fills not-yet-reached stops with their scheduled time as a fake
    "actual" (this is why the website wrongly shows them as already served). A
    real measurement only exists when the actual time differs from the
    scheduled one, so we detect passage by that difference rather than by
    presence of a value. Returns (arrived, departed).
    """
    aa, ea = st.get("ActualArrivalTime"), st.get("EstimatedArrivalTime")
    ad, ed = st.get("ActualDepartureTime"), st.get("EstimatedDepartureTime")
    arrived = bool(aa) and aa != ea
    departed = bool(ad) and ad != ed
    return arrived, departed


def progress(schedule, code):
    """Return ('enroute'|'arrived'|'departed', last_real_station_name)."""
    stations = all_stations(schedule)
    target = next((s for s in stations if s.get("LocationCode") == code), None)

    last_real = None
    last_real_num = -1
    later_arrived = False
    for st in stations:
        arr, dep = real_passed(st)
        if arr or dep:
            num = st.get("StationNumber", -1)
            if num > last_real_num:
                last_real_num = num
                last_real = st.get("LocationDescription")
            if target is not None and num > target.get("StationNumber", 10**9):
                later_arrived = True

    state = "enroute"
    if target is not None:
        t_arr, t_dep = real_passed(target)
        if t_dep or later_arrived:
            state = "departed"
        elif t_arr:
            state = "arrived"
    return state, last_real


def parse_status(data):
    if data.get("IsEmpty", True):
        return None

    schedule = data.get("TrainSchedule") or {}
    disruption = schedule.get("Distruption") or {}
    leg = schedule.get("Leg") or {}
    station = find_station(schedule, STATION_CODE)

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

    station_name = (
        (station or {}).get("LocationDescription")
        or leg.get("ArrivalStationDescription")
        or STATION_CODE
    )
    state, last_real = progress(schedule, STATION_CODE)

    return {
        "delay": disruption.get("DelayAmount"),
        "running_state": disruption.get("RunningState"),
        "scheduled": scheduled,
        "projected": projected,
        "platform": platform,
        "station_name": station_name,
        "origin": schedule.get("DepartureStationDescription"),
        "destination": schedule.get("ArrivalStationDescription"),
        "last_update": data.get("LastUpdate"),
        "state": state,
        "last_real": last_real,
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
    ]
    if s["last_real"]:
        lines.append(f"Ultima fermata reale: {s['last_real']}")
    lines += [
        f"Tratta: {s['origin']} → {s['destination']}",
        f"Aggiornato: {s['last_update']}",
    ]
    return "\n".join(lines)


def signature(s):
    return (s["delay"], s["projected"], s["platform"], s["running_state"], s["state"])


def send_ntfy(title, message, priority="default", tags=""):
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    req = urllib.request.Request(
        NTFY_URL, data=message.encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()


def main():
    log(
        f"Monitoring train {TRAIN_NUMBER} @ {STATION_CODE} -> {NTFY_URL} "
        f"every {POLL_INTERVAL}s (always_notify={ALWAYS_NOTIFY}, "
        f"stop_after={STOP_AFTER_SECONDS}s, stop_on_departure={STOP_ON_DEPARTURE})"
    )
    started = time.monotonic()
    last_sig = None
    announced_arrival = False
    notified_error = False

    while True:
        if STOP_AFTER_SECONDS and (time.monotonic() - started) >= STOP_AFTER_SECONDS:
            log("Time limit reached, stopping.")
            send_ntfy(
                f"Italo {TRAIN_NUMBER}: monitor terminato",
                f"Limite di tempo raggiunto ({STOP_AFTER_SECONDS // 60} min). Monitoraggio interrotto.",
                priority="low",
                tags="hourglass_done",
            )
            return 0

        try:
            s = parse_status(fetch_status())
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
                time.sleep(POLL_INTERVAL)
                continue

            has_platform = s["platform"] not in (None, "")
            sig = signature(s)

            # Dedicated, always-sent alert the moment the train reaches our stop.
            if s["state"] in ("arrived", "departed") and not announced_arrival:
                send_ntfy(
                    f"Italo {TRAIN_NUMBER}: treno a {s['station_name']}!",
                    build_message(s),
                    priority="urgent",
                    tags="bullettrain_side,checkered_flag",
                )
                announced_arrival = True
                last_sig = sig
                log(f"Arrival alert: {sig}")
            elif sig != last_sig or ALWAYS_NOTIFY:
                priority = "high" if has_platform else "default"
                tags = "train" + (",checkered_flag" if has_platform else "")
                send_ntfy(
                    f"Italo {TRAIN_NUMBER} · {delay_text(s['delay'])}",
                    build_message(s),
                    priority=priority,
                    tags=tags,
                )
                last_sig = sig
                log(f"Notified: {sig}")
            else:
                log(f"No change: {sig}")

            if STOP_ON_DEPARTURE and s["state"] == "departed":
                log("Train departed target station, stopping.")
                send_ntfy(
                    f"Italo {TRAIN_NUMBER}: partito da {s['station_name']}",
                    "Il treno è partito dalla tua stazione. Monitoraggio interrotto. Buon viaggio!",
                    priority="default",
                    tags="wave",
                )
                return 0

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
