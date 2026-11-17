"""Live tools for the Garmin Connect Personal API, one per endpoint.

These call Garmin when Claude asks and return Garmin's JSON as it is, so the
reply can hold anything Garmin sends, coordinates and names included
(decided 2026-11-17). The one exception is get_activity_fit: it parses the
original FIT file and drops every GPS field.

The API key is exchanged for an access token valid for 24 hours. The token
is kept in memory and reused, because Garmin rate limits the token endpoint
along with everything else (HTTP 429).

Settings, from the environment:
    GARMIN_API_KEY   the Personal API key, or else
    GARMIN_KEY_FILE  a KEY=value file holding it (default ~/.config/garmin-llm/garmin.env)
"""

import datetime
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
DEFAULT_KEY_FILE = "~/.config/garmin-llm/garmin.env"
TOKEN_TTL_S = 23 * 3600  # Garmin's tokens last 24 hours
TIMEOUT_S = 60
# Replies larger than this are refused with a hint to narrow the request,
# so one call cannot flood the chat.
MAX_REPLY_BYTES = 200_000

_token = None
_token_time = 0.0
_token_lock = threading.Lock()


def read_key():
    if os.environ.get("GARMIN_API_KEY"):
        return os.environ["GARMIN_API_KEY"]
    path = Path(os.environ.get("GARMIN_KEY_FILE", DEFAULT_KEY_FILE)).expanduser()
    if path.exists():
        for line in path.read_text().splitlines():
            name, _, value = line.strip().partition("=")
            if name == "GARMIN_API_KEY":
                return value
    raise ToolError("the server has no Garmin API key (GARMIN_API_KEY)")


def http(method, path, headers, params=None):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:
            return reply.status, reply.headers.get("Content-Type", ""), reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()
    except urllib.error.URLError as exc:
        raise ToolError(f"could not reach Garmin: {exc.reason}") from None


def token(renew=False):
    global _token, _token_time
    with _token_lock:
        if renew or _token is None or time.time() - _token_time > TOKEN_TTL_S:
            status, _, body = http("POST", TOKEN_PATH, {"Authorization": f"ApiKey {read_key()}"})
            if status == 429:
                raise ToolError("Garmin is rate limiting this key (HTTP 429); try again in a few minutes")
            if status != 200:
                raise ToolError(f"Garmin refused the API key (HTTP {status}); it may have expired")
            _token, _token_time = body.decode().strip(), time.time()
        return _token


def garmin_get(path, params=None):
    """GET a resource. Returns (content type, body bytes); None body for 204."""
    for renew in (False, True):
        status, content_type, body = http("GET", path, {"Authorization": f"Bearer {token(renew)}"}, params)
        if status != 401:
            break
    if status == 204:
        return content_type, None
    if status == 429:
        raise ToolError("Garmin is rate limiting this key (HTTP 429); try again in a few minutes")
    if status >= 400:
        detail = body[:300].decode(errors="replace")
        raise ToolError(f"Garmin answered HTTP {status}: {detail}")
    return content_type, body


def get(path, params=None):
    """A JSON resource as Python data, or a note when there is no data."""
    _, body = garmin_get(path, params)
    if not body:
        return {"no_data": True, "note": "Garmin has no data for this request"}
    if len(body) > MAX_REPLY_BYTES:
        raise ToolError(
            f"the reply is {len(body) // 1000} kB, more than the {MAX_REPLY_BYTES // 1000} kB allowed; "
            "ask for a shorter date range, or use run_query on the daily_summaries table for long periods"
        )
    return json.loads(body)


# Argument checks --------------------------------------------------------------


def day(value, name="date"):
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a date like 2026-11-17, got {value!r}") from None


def date_range(start_date, end_date, max_days):
    start, end = day(start_date, "start_date"), day(end_date, "end_date")
    days = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days + 1
    if days < 1:
        raise ToolError("end_date is before start_date")
    if days > max_days:
        raise ToolError(f"at most {max_days} days per call, asked for {days}; split the range")
    return start, end


# Activities -------------------------------------------------------------------

SEARCH_FILTERS = (
    "favorite, excludeChildren, activitySubType, eventType, minElevation, maxElevation, "
    "upperLatitude, rightLongitude, lowerLatitude, leftLongitude, minElapsedDuration, "
    "maxElapsedDuration, minAvgPower, excludedActivitySubType, requiredField, "
    "lowerMaxDepth, upperMaxDepth, decoDive, minBottomTime, maxBottomTime"
)


def search_activities(
    start_date: str | None = None,
    end_date: str | None = None,
    activity_type: str | None = None,
    search: str | None = None,
    min_distance_m: float | None = None,
    max_distance_m: float | None = None,
    min_duration_s: float | None = None,
    max_duration_s: float | None = None,
    sort_by: str | None = None,
    sort_order: str = "desc",
    start: int = 0,
    limit: int = 20,
    extra_filters: dict[str, str] | None = None,
) -> list | dict:
    """Search activities live in Garmin Connect, newest first by default.

    Returns Garmin's activity summaries (one dict per activity, with
    activityId, name, type, distance in m, duration in s, speeds in m/s, HR,
    training effect, and more). Use the activityId with get_activity,
    get_activity_workouts or get_activity_fit. activity_type is a Garmin type
    key such as running, trail_running, cycling, strength_training. search
    matches the activity name. Pages: start is the offset, limit at most 50.
    For history and totals, run_query on the warehouse is faster and complete.
    Other Garmin filters can go in extra_filters by their API name: """
    params = {
        "startDate": day(start_date, "start_date") if start_date else None,
        "endDate": day(end_date, "end_date") if end_date else None,
        "activityType": activity_type,
        "search": search,
        "minDistance": min_distance_m,
        "maxDistance": max_distance_m,
        "minDuration": min_duration_s,
        "maxDuration": max_duration_s,
        "sortBy": sort_by,
        "sortOrder": sort_order,
        "start": max(0, start),
        "limit": max(1, min(limit, 50)),
        **(extra_filters or {}),
    }
    return get("/activitylist-service/activities/search/activities", params)


search_activities.__doc__ = search_activities.__doc__ + SEARCH_FILTERS


def get_activity(activity_id: int) -> dict:
    """One activity in full: summary, metadata, measurements and splits (laps)."""
    return get(f"/activity-service/activity/{int(activity_id)}")


def get_activity_workouts(activity_id: int) -> list | dict:
    """The planned workout(s) the activity followed, with steps and targets. Empty when none."""
    return get(f"/activity-service/activity/{int(activity_id)}/workouts")


def get_heart_rate_zones() -> list | dict:
    """The user's heart rate zone settings per sport: zone floors, max HR, resting HR, threshold HR."""
    return get("/biometric-service/heartRateZones/v2")


def get_user_settings() -> dict:
    """Profile settings: max HR, lactate threshold, VO2max, weight, height, units, sleep times and more."""
    return get("/userprofile-service/userprofile/settings")


def get_fitness_age(date: str) -> dict:
    """Fitness Age as of the date (YYYY-MM-DD), with the factors behind it."""
    return get(f"/fitnessage-service/fitnessage/v2/{day(date)}")


# Sleep, HRV, heart rate, SpO2 ---------------------------------------------------


def get_sleep(date: str) -> dict:
    """Sleep for one night in detail: stages, sleep score and its factors, sleep need, HR,
    respiration, SpO2 and stress during sleep. The date is the day the sleep ended."""
    return get("/sleep-service/sleep/dailySleep", {"date": day(date)})


def get_sleep_range(start_date: str, end_date: str) -> list | dict:
    """Nightly sleep summaries for up to 31 nights, dated by the day each sleep ended."""
    start, end = date_range(start_date, end_date, 31)
    return get("/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": start, "endDate": end})


def get_hrv(date: str) -> dict:
    """Overnight HRV for one night, with the 5-minute readings, baseline and status."""
    return get(f"/hrv-service/hrv/{day(date)}")


def get_hrv_range(start_date: str, end_date: str) -> dict:
    """Nightly HRV summaries (last night average, weekly average, baseline, status), up to 365 nights."""
    start, end = date_range(start_date, end_date, 365)
    return get(f"/hrv-service/hrv/daily/{start}/{end}")


def get_daily_heart_rate(date: str) -> dict:
    """Heart rate through one day: resting, min, max and readings about every 2 minutes."""
    return get("/wellness-service/wellness/dailyHeartRate", {"date": day(date)})


def get_spo2(date: str) -> dict:
    """SpO2 (blood oxygen) readings and averages for one day, with altitude acclimation."""
    return get(f"/wellness-service/wellness/daily/spo2acclimation/{day(date)}")


# Daily summaries, stress, Body Battery, steps ----------------------------------


def get_daily_summary(date: str) -> dict:
    """Everything Garmin sums up for one day: steps, calories, HR, stress, Body Battery,
    intensity minutes, respiration, SpO2, floors. Today's is partial."""
    return get("/usersummary-service/usersummary/daily", {"calendarDate": day(date)})


def get_daily_summaries(start_date: str, end_date: str) -> dict:
    """Daily summaries (v2) for a range. Up to 366 days is allowed, but replies over 200 kB
    are refused, so about 2 months fits. For longer periods use run_query on daily_summaries."""
    start, end = date_range(start_date, end_date, 366)
    return get("/usersummary-service/v2/dailysummaries", {"startDate": start, "endDate": end})


def get_body_battery(start_date: str, end_date: str, chunks: int | None = None) -> list | dict:
    """Daily Body Battery reports for up to 31 days: charged, drained, highs and lows,
    with what drained or charged it. chunks splits each day into that many intervals and
    adds the lowest and highest reading of each (24 = hourly)."""
    start, end = date_range(start_date, end_date, 31)
    return get("/wellness-service/wellness/bodyBattery/reports/daily",
               {"startDate": start, "endDate": end, "chunks": chunks})


def get_stress_range(start_date: str, end_date: str) -> list | dict:
    """Daily stress levels for up to 28 days."""
    start, end = date_range(start_date, end_date, 28)
    return get(f"/usersummary-service/stats/stress/daily/{start}/{end}")


def get_steps_range(start_date: str, end_date: str) -> list | dict:
    """Daily steps, step goal and distance for up to 28 days."""
    start, end = date_range(start_date, end_date, 28)
    return get(f"/usersummary-service/stats/steps/daily/{start}/{end}")


def get_hydration_range(start_date: str, end_date: str) -> list | dict:
    """Daily hydration (logged water, goal, sweat loss) for up to 31 days."""
    start, end = date_range(start_date, end_date, 31)
    return get(f"/usersummary-service/stats/hydration/daily/{start}/{end}")


def get_wellness_activities(start_date: str, end_date: str) -> list | dict:
    """Wellness activity summaries for a date range (inclusive), such as breathwork or
    meditation sessions logged outside regular workouts."""
    start, end = day(start_date, "start_date"), day(end_date, "end_date")
    return get(f"/wellnessactivity-service/activity/summary/range/{start}/{end}")


# Body, nutrition, cycle ----------------------------------------------------------


def get_weight_entries(start_date: str, end_date: str) -> list | dict:
    """Every weight and body composition entry in the range (weight in grams)."""
    start, end = day(start_date, "start_date"), day(end_date, "end_date")
    return get(f"/weight-service/weight/all/{start}/{end}")


def get_weight_summary(start_date: str, end_date: str, include_all: bool = False) -> dict:
    """Weight per day (latest entry), the range average, and the entries just before and after."""
    start, end = day(start_date, "start_date"), day(end_date, "end_date")
    return get(f"/weight-service/weight/range/{start}/{end}", {"includeAll": str(include_all).lower()})


def get_weight_goals(start_date: str, end_date: str) -> list | dict:
    """Weight goals in effect during the range."""
    start, end = day(start_date, "start_date"), day(end_date, "end_date")
    return get(f"/goal-service/goal/user/effective/weightgoal/{start}/{end}")


def get_nutrition(date: str) -> dict:
    """Food log for one day: goals, nutrients consumed and each logged meal."""
    return get(f"/nutrition-service/food/logs/{day(date)}",
               {"includeGoals": "true", "includeNutrition": "true", "includeMedia": "false"})


def get_menstrual_cycle_summary(date: str) -> dict:
    """Menstrual cycle summary as of the date (cycle day, phase, predictions)."""
    return get(f"/periodichealth-service/menstrualcycle/summary/{day(date)}")


def get_menstrual_cycle_day(date: str) -> dict:
    """Detailed menstrual cycle day view: logged symptoms, flow, mood, and the cycle summary."""
    return get(f"/periodichealth-service/menstrualcycle/dayview/{day(date)}")


# FIT file -------------------------------------------------------------------------

MAX_SAMPLES = 1500


def is_gps_field(name):
    return (
        "position" in name
        or name.endswith(("_lat", "_long"))
        or name.startswith(("nec_", "swc_"))
    )


def json_value(v):
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, tuple):
        return [json_value(x) for x in v]
    if isinstance(v, bytes):
        return None
    if isinstance(v, float):
        return round(v, 3)
    return v


def message_dict(frame, units):
    out = {}
    for field in frame.fields:
        name = field.name
        if name.startswith("unknown") or is_gps_field(name) or field.value is None:
            continue
        value = json_value(field.value)
        if value is None:
            continue
        out[name] = value
        if field.units:
            units[name] = field.units
    return out


def fit_bytes(body):
    """Garmin sends the original file inside a zip."""
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".fit")]
            if not names:
                raise ToolError("the activity download holds no FIT file")
            return z.read(names[0])
    return body


def get_activity_fit(activity_id: int, sample_interval_s: int = 15) -> dict:
    """The activity's original FIT file, parsed: session totals, every lap, and the
    second-by-second record sampled every sample_interval_s seconds (HR, speed in m/s,
    distance in m, altitude, cadence, power, running dynamics). GPS fields are removed.
    elapsed_s in samples counts from the first record. Units are listed in `units`."""
    import fitdecode  # imported here so the other tools work without it

    _, body = garmin_get(f"/download-service/files/activity/{int(activity_id)}")
    if not body:
        raise ToolError("Garmin has no file for this activity (manual activities have none)")
    units = {}
    sessions, laps, records = [], [], []
    with fitdecode.FitReader(io.BytesIO(fit_bytes(body))) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "session":
                sessions.append(message_dict(frame, units))
            elif frame.name == "lap":
                laps.append(message_dict(frame, units))
            elif frame.name == "record":
                records.append(message_dict(frame, units))

    records = [r for r in records if "timestamp" in r]
    interval = max(1, sample_interval_s)
    if records:
        t0 = datetime.datetime.fromisoformat(records[0]["timestamp"])
        duration = (datetime.datetime.fromisoformat(records[-1]["timestamp"]) - t0).total_seconds()
        # Keep the reply bounded on long activities.
        interval = max(interval, int(duration // MAX_SAMPLES) + 1)
    samples, next_t = [], 0.0
    for r in records:
        elapsed = (datetime.datetime.fromisoformat(r["timestamp"]) - t0).total_seconds()
        if elapsed >= next_t:
            samples.append({"elapsed_s": elapsed, **{k: v for k, v in r.items() if k != "timestamp"}})
            next_t = elapsed + interval
    columns = sorted({k for s in samples for k in s} - {"elapsed_s"})
    return {
        "activity_id": int(activity_id),
        "sessions": sessions,
        "laps": laps,
        "samples": {
            "interval_s": interval,
            "columns": ["elapsed_s"] + columns,
            "rows": [[s.get(c) for c in ["elapsed_s"] + columns] for s in samples],
        },
        "units": units,
        "note": "GPS fields removed",
    }


TOOLS = [
    search_activities, get_activity, get_activity_workouts, get_activity_fit,
    get_heart_rate_zones, get_user_settings, get_fitness_age,
    get_sleep, get_sleep_range, get_hrv, get_hrv_range, get_daily_heart_rate, get_spo2,
    get_daily_summary, get_daily_summaries, get_body_battery, get_stress_range, get_steps_range,
    get_hydration_range, get_wellness_activities,
    get_weight_entries, get_weight_summary, get_weight_goals, get_nutrition,
    get_menstrual_cycle_summary, get_menstrual_cycle_day,
]
