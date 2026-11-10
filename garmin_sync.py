#!/usr/bin/env python3
"""
Garmin Dashboard - sync backend (runs in GitHub Actions)
=======================================================

Logs in to Garmin Connect, fetches new data since the last run, and writes an
ENCRYPTED data file for the web page in site/. Nothing readable is ever
committed or published: the data, the Garmin login token and any pending
2-step-verification session are all encrypted with your DATA_PASSPHRASE
(AES-256-GCM, key from PBKDF2-SHA256).

Environment (set as GitHub secrets / workflow inputs):
  DATA_PASSPHRASE   required. Used to encrypt everything; you type it to open the page.
  GARMIN_EMAIL      Garmin login email      (only needed until a token is saved)
  GARMIN_PASSWORD   Garmin login password   (only needed until a token is saved)
  MFA_CODE          2-step verification code, when Garmin asked for one
  START_DATE        optional YYYY-MM-DD, first day to fetch (default: oldest activity)
  RESET             "true" to discard previously synced data and start again
  TIME_BUDGET_MIN   stop fetching after this many minutes (default 55); the
                    workflow then starts another run to continue
  PAGES_URL         public URL of the site (used to recover data if the cache expired)

Run locally for testing:
  DATA_PASSPHRASE=test GARMIN_EMAIL=... GARMIN_PASSWORD=... python garmin_sync.py
"""

import base64
import datetime as dt
import gzip
import io
import json
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"            # kept between runs by actions/cache (all files encrypted)
SITE_SRC = ROOT / "site"
SITE_OUT = ROOT / "site_out"      # uploaded to GitHub Pages
TOKEN_DIR = Path(os.path.expanduser("~/.garminconnect"))
os.environ.setdefault("GARMINTOKENS", str(TOKEN_DIR))

PBKDF2_ITER = 600_000
PAUSE = float(os.getenv("GARMIN_PAUSE", "0.5"))
REFRESH_RECENT_DAYS = 3
STORE_VERSION = 1

DAILY_ENDPOINTS = [            # (key, method, args) - one call each per day
    ("summary", "get_stats", lambda d: (d,)),
    ("sleep", "get_sleep_data", lambda d: (d,)),
    ("hrv", "get_hrv_data", lambda d: (d,)),
    ("training_readiness", "get_training_readiness", lambda d: (d,)),
    ("max_metrics", "get_max_metrics", lambda d: (d,)),
]

_secrets_to_redact = []


def redact(msg):
    msg = str(msg)
    for s in _secrets_to_redact:
        if s and len(s) >= 4:
            msg = msg.replace(s, "***")
    return msg


def log(msg):
    msg = redact(msg)
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}", flush=True)


def gh(cmd):
    """GitHub Actions workflow command (annotation / mask / output)."""
    if os.getenv("GITHUB_ACTIONS"):
        print(cmd, flush=True)


def set_output(name, value):
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def summary(md):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")


def short_err(e):
    text = f"{type(e).__name__}: {e}"
    text = re.sub(r"https?://\S+", "<url>", text)   # don't leak URLs (contain your Garmin user id)
    return text[:160]


# ---------------------------------------------------------------------------
# Encryption (must match decrypt() in site/index.html)
# ---------------------------------------------------------------------------
def _key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(passphrase.encode())


def encrypt(data: bytes, passphrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passphrase, salt, PBKDF2_ITER)).encrypt(iv, gzip.compress(data, 9), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "alg": "AES-256-GCM", "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITER,
            "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt(env: dict, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = base64.b64decode
    key = _key(passphrase, d(env["salt"]), int(env["iter"]))
    return gzip.decompress(AESGCM(key).decrypt(d(env["iv"]), d(env["ct"]), None))


def save_enc(path: Path, data: bytes, passphrase: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(encrypt(data, passphrase)))
    tmp.replace(path)


def load_enc(path: Path, passphrase: str):
    if not path.exists():
        return None
    return decrypt(json.loads(path.read_text()), passphrase)


# ---------------------------------------------------------------------------
# Token persistence (encrypted tarball of ~/.garminconnect)
# ---------------------------------------------------------------------------
def restore_tokens(passphrase):
    try:
        blob = load_enc(STATE / "tokens.enc", passphrase)
    except Exception:
        log("Saved Garmin token could not be decrypted (passphrase changed?) - ignoring it.")
        return False
    if not blob:
        return False
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for m in tf.getmembers():
            if m.isfile() and "/" not in m.name and ".." not in m.name:
                (TOKEN_DIR / m.name).write_bytes(tf.extractfile(m).read())
                os.chmod(TOKEN_DIR / m.name, 0o600)
    return any(TOKEN_DIR.iterdir())


def persist_tokens(api, passphrase):
    # The library saves tokens itself on login; after an MFA resume we also try
    # the known dump helpers, whichever exists in the installed version.
    for obj_name in ("", "client", "garth"):
        obj = getattr(api, obj_name, None) if obj_name else api
        for meth in ("dump", "save_tokens", "save"):
            fn = getattr(obj, meth, None) if obj is not None else None
            if callable(fn):
                try:
                    fn(str(TOKEN_DIR))
                except Exception:
                    pass
    if not TOKEN_DIR.exists() or not any(p.is_file() for p in TOKEN_DIR.iterdir()):
        log("Note: no Garmin token file found to save; you may be asked to log in again next run.")
        return
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in TOKEN_DIR.iterdir():
            if p.is_file():
                tf.add(p, arcname=p.name)
    save_enc(STATE / "tokens.enc", buf.getvalue(), passphrase)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
class NeedsMFA(Exception):
    pass


def connect(passphrase):
    from garminconnect import Garmin

    mfa_code = (os.getenv("MFA_CODE") or "").strip().replace(" ", "")
    if mfa_code:
        gh(f"::add-mask::{mfa_code}")
        try:
            state = load_enc(STATE / "mfa_pending.enc", passphrase)
        except Exception:
            state = None
        if not state:
            raise SystemExit("A verification code was entered, but there is no login waiting for one. "
                             "Run the workflow again WITHOUT a code first; Garmin will then send you a new code.")
        api = Garmin()
        try:
            api.resume_login(json.loads(state), mfa_code)
        except Exception as e:
            (STATE / "mfa_pending.enc").unlink(missing_ok=True)
            raise SystemExit("Garmin didn't accept that verification code (it may have expired). "
                             f"Start again without a code to get a new one. ({short_err(e)})")
        (STATE / "mfa_pending.enc").unlink(missing_ok=True)
        log("2-step verification accepted.")
        persist_tokens(api, passphrase)
        try:  # reload from the saved token so the client is fully initialised (profile etc.)
            fresh = Garmin()
            fresh.login(str(TOKEN_DIR))
            return fresh
        except Exception:
            return api

    if restore_tokens(passphrase):
        try:
            api = Garmin()
            api.login(str(TOKEN_DIR))
            log("Logged in with saved token.")
            return api
        except Exception as e:
            log(f"Saved token didn't work ({short_err(e)}); logging in with email/password.")

    email, password = os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        raise SystemExit("No saved login yet. Add GARMIN_EMAIL and GARMIN_PASSWORD as repository secrets "
                         "(Settings > Secrets and variables > Actions).")
    api = Garmin(email, password, return_on_mfa=True)
    result = api.login(str(TOKEN_DIR))
    if isinstance(result, (tuple, list)) and result and result[0] == "needs_mfa":
        save_enc(STATE / "mfa_pending.enc", json.dumps(result[1]).encode(), passphrase)
        raise NeedsMFA()
    log("Logged in with email/password.")
    persist_tokens(api, passphrase)
    return api


class Caller:
    def __init__(self, api):
        self.api, self.calls = api, 0

    def __call__(self, method, *args, **kw):
        fn = getattr(self.api, method, None)
        if fn is None:
            raise AttributeError(method)
        delay = 30
        for attempt in range(6):
            try:
                time.sleep(PAUSE)
                self.calls += 1
                return fn(*args, **kw)
            except Exception as e:
                t = f"{type(e).__name__} {e}"
                if "429" in t or "TooMany" in t or "Too Many" in t:
                    log(f"Garmin rate limit - waiting {delay}s")
                    time.sleep(delay)
                    delay = min(delay * 2, 600)
                    continue
                if any(s in t for s in ("Timeout", "timed out", "ConnectionError", "502", "503", "504")) and attempt < 2:
                    time.sleep(5)
                    continue
                raise
        raise RuntimeError("still rate limited after retries")


def is_permanent(e):
    t = str(e)
    return any(s in t for s in ("404", "400", "403", "Not Found", "not found", "No data", "NoneType"))


# ---------------------------------------------------------------------------
# Data shaping (compact rows for the web page)
# ---------------------------------------------------------------------------
def g(obj, *path):
    for p in path:
        if obj is None:
            return None
        obj = (obj[p] if isinstance(obj, list) and isinstance(p, int) and len(obj) > p
               else obj.get(p) if isinstance(obj, dict) else None)
    return obj


def first(*vals):
    for v in vals:
        if v not in (None, "", 0):
            return v
    return None


def r(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) else None


def daily_row(date, rec):
    s = rec.get("summary") or {}
    sl = g(rec, "sleep", "dailySleepDTO") or {}
    hrv = g(rec, "hrv", "hrvSummary") or {}
    tr = rec.get("training_readiness")
    tr = tr[0] if isinstance(tr, list) and tr else (tr if isinstance(tr, dict) else {})
    mm = rec.get("max_metrics")
    mm = mm[0] if isinstance(mm, list) and mm else (mm if isinstance(mm, dict) else {})
    h = lambda k: r(sl[k] / 3600) if sl.get(k) else None
    row = {
        "d": date,
        "steps": s.get("totalSteps"),
        "stepGoal": s.get("dailyStepGoal"),
        "km": r(s["totalDistanceMeters"] / 1000) if s.get("totalDistanceMeters") else None,
        "kcal": s.get("totalKilocalories"),
        "activeKcal": s.get("activeKilocalories"),
        "rhr": s.get("restingHeartRate"),
        "maxHr": s.get("maxHeartRate"),
        "stress": s.get("averageStressLevel") if (s.get("averageStressLevel") or -1) >= 0 else None,
        "bbHigh": s.get("bodyBatteryHighestValue"),
        "bbLow": s.get("bodyBatteryLowestValue"),
        "floors": round(s["floorsAscended"]) if s.get("floorsAscended") else None,
        "intensity": ((s.get("moderateIntensityMinutes") or 0) + 2 * (s.get("vigorousIntensityMinutes") or 0)) or None,
        "spo2": s.get("averageSpo2"),
        "resp": s.get("avgWakingRespirationValue"),
        "sleepH": h("sleepTimeSeconds"), "deepH": h("deepSleepSeconds"), "remH": h("remSleepSeconds"),
        "lightH": h("lightSleepSeconds"), "awakeH": h("awakeSleepSeconds"),
        "sleepScore": g(sl, "sleepScores", "overall", "value"),
        "hrv": hrv.get("lastNightAvg"),
        "readiness": tr.get("score"),
        "vo2": first(g(mm, "generic", "vo2MaxPreciseValue"), g(mm, "generic", "vo2MaxValue")),
    }
    return {k: v for k, v in row.items() if v is not None}


def activity_row(a):
    t = a.get("activityType") or {}
    zones = [round(a.get(f"hrTimeInZone_{i}") or 0) for i in range(1, 6)]
    row = {
        "id": a.get("activityId"), "name": a.get("activityName"), "type": t.get("typeKey"),
        "start": a.get("startTimeLocal"),
        "km": r(a["distance"] / 1000, 3) if a.get("distance") else None,
        "sec": round(a["duration"]) if a.get("duration") else None,
        "movSec": round(a["movingDuration"]) if a.get("movingDuration") else None,
        "kcal": a.get("calories"), "avgHr": a.get("averageHR"), "maxHr": a.get("maxHR"),
        "elev": round(a["elevationGain"]) if a.get("elevationGain") else None,
        "speed": a.get("averageSpeed"),
        "cadence": first(a.get("averageRunningCadenceInStepsPerMinute"), a.get("averageBikingCadenceInRevPerMinute")),
        "power": a.get("avgPower"), "aerobicTE": a.get("aerobicTrainingEffect"),
        "anaerobicTE": a.get("anaerobicTrainingEffect"), "vo2": a.get("vO2MaxValue"), "steps": a.get("steps"),
        "lat": r(a.get("startLatitude"), 4), "lon": r(a.get("startLongitude"), 4),
        "loc": a.get("locationName"), "desc": a.get("description"),
        # extra detail (present for most watch-recorded activities)
        "tl": r(a.get("activityTrainingLoad"), 1),
        "teLabel": a.get("trainingEffectLabel"),
        "maxSpeed": a.get("maxSpeed"),
        "stride": r(a.get("avgStrideLength"), 1),
        "gct": r(a.get("avgGroundContactTime"), 0),
        "vo": r(a.get("avgVerticalOscillation"), 1),
        "z": zones if any(zones) else None,
    }
    return {k: v for k, v in row.items() if v is not None}


# ---------------------------------------------------------------------------
# Store (the decrypted contents of data.enc.json)
# ---------------------------------------------------------------------------
def empty_store():
    return {"v": STORE_VERSION, "activities": [], "days": [], "weights": [], "devices": [], "meta": {}}


def load_store(passphrase):
    if os.getenv("RESET", "").lower() == "true":
        log("RESET requested - starting with an empty data set.")
        return empty_store()
    path = STATE / "data.enc.json"
    if not path.exists() and os.getenv("PAGES_URL"):
        # cache expired (no run for 7+ days): recover from the published site
        try:
            url = os.getenv("PAGES_URL").rstrip("/") + "/data.enc.json"
            with urllib.request.urlopen(url, timeout=30) as resp:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(resp.read())
            log("Recovered previous data from the published page.")
        except Exception:
            pass
    if not path.exists():
        return empty_store()
    try:
        store = json.loads(load_enc(path, passphrase))
    except Exception:
        raise SystemExit("Existing data could not be decrypted with DATA_PASSPHRASE. If you changed the "
                         "passphrase on purpose, run the workflow once with 'Start over' ticked.")
    for k, v in empty_store().items():
        store.setdefault(k, v)
    return store


# ---------------------------------------------------------------------------
# Sync steps
# ---------------------------------------------------------------------------
ACTIVITY_FIELDS_VERSION = 2   # bump when activity_row() gains fields, to refresh all activities once


def sync_activities(call, store):
    known = {a["id"]: a for a in store["activities"]}
    if store["meta"].get("activity_fields") != ACTIVITY_FIELDS_VERSION:
        store["meta"]["activities_complete"] = False   # page through everything once
    new, start = 0, 0
    while True:
        batch = call("get_activities", start, 100) or []
        if not batch:
            break
        fresh = [a for a in batch if a.get("activityId") not in known]
        for a in batch:
            row = activity_row(a)
            if row.get("id"):
                known[row["id"]] = row
        new += len(fresh)
        if not fresh and store["meta"].get("activities_complete"):
            break
        start += 100
    store["meta"]["activities_complete"] = True
    store["meta"]["activity_fields"] = ACTIVITY_FIELDS_VERSION
    store["activities"] = sorted(known.values(), key=lambda a: a.get("start") or "", reverse=True)
    log(f"Activities: {len(store['activities'])} total ({new} new).")


def sync_weights(call, store, start, end):
    s = start if not store["meta"].get("weights_complete") else max(start, end - dt.timedelta(days=90))
    rows = {w["d"]: w for w in store["weights"]}
    while s <= end:
        e = min(s + dt.timedelta(days=364), end)
        try:
            res = call("get_body_composition", s.isoformat(), e.isoformat()) or {}
            for w in res.get("dateWeightList") or []:
                if w.get("weight"):
                    d = w.get("calendarDate") or str(w.get("date", ""))[:10]
                    rows[d] = {k: v for k, v in {
                        "d": d, "kg": r(w["weight"] / 1000), "bmi": r(w.get("bmi"), 1), "fat": r(w.get("bodyFat"), 1),
                        "muscleKg": r(w["muscleMass"] / 1000) if w.get("muscleMass") else None,
                    }.items() if v is not None}
        except Exception as ex:
            log(f"Weight {s}..{e}: {short_err(ex)}")
            return
        s = e + dt.timedelta(days=1)
    store["weights"] = sorted(rows.values(), key=lambda w: w["d"])
    store["meta"]["weights_complete"] = True
    log(f"Weigh-ins: {len(store['weights'])}.")


def sync_days(call, store, start, end, deadline):
    days = {d["d"]: d for d in store["days"]}
    recent = end - dt.timedelta(days=REFRESH_RECENT_DAYS)
    todo = [end - dt.timedelta(days=i) for i in range((end - start).days + 1)]
    todo = [d for d in todo if d.isoformat() not in days or d >= recent]
    log(f"Daily health data: {len(todo)} days to fetch.")
    done = 0
    for d in todo:
        if time.time() > deadline:
            log(f"Time budget reached after {done} days; the next run continues from here.")
            store["days"] = sorted(days.values(), key=lambda x: x["d"])
            return False
        ds, rec, temp_fail = d.isoformat(), {}, False
        for key, method, argf in DAILY_ENDPOINTS:
            try:
                rec[key] = call(method, *argf(ds))
            except AttributeError:
                pass
            except Exception as e:
                if not is_permanent(e):
                    temp_fail = True
        if temp_fail and not rec.get("summary"):
            continue  # retry this day next run
        days[ds] = daily_row(ds, rec)
        done += 1
        if done % 50 == 0:
            log(f"  {done}/{len(todo)} days")
            store["days"] = sorted(days.values(), key=lambda x: x["d"])
    store["days"] = sorted(days.values(), key=lambda x: x["d"])
    return True


def sync_account(call, store):
    try:
        name = call("get_full_name")
        if isinstance(name, str):
            store["name"] = name
            _secrets_to_redact.append(name)
    except Exception:
        pass
    try:
        # Garmin personal records (fastest 1K/mile/5K/10K/half/marathon, longest run/ride, step records...)
        prs = call("get_personal_record") or []
        keep = ("typeId", "prTypeLabelKey", "value", "activityId", "activityName", "activityType",
                "prStartTimeLocalFormatted", "prStartTimeLocal", "actStartDateTimeLocalFormatted",
                "activityStartDateTimeLocal", "prStartTimeGmtFormatted", "poolLengthUnit")
        store["records"] = [{k: p.get(k) for k in keep if p.get(k) is not None} for p in prs if isinstance(p, dict)]
        log(f"Personal records: {len(store['records'])}.")
    except Exception as e:
        log(f"Personal records unavailable ({short_err(e)}).")
    try:
        devs = call("get_devices") or []
        store["devices"] = [{k: v for k, v in {
            "name": first(d.get("productDisplayName"), d.get("displayName"), d.get("deviceTypeName")),
            "fw": d.get("currentFirmwareVersion"),
        }.items() if v} for d in devs if isinstance(d, dict)]
    except Exception:
        pass


def build_site(store, passphrase):
    if SITE_OUT.exists():
        shutil.rmtree(SITE_OUT)
    shutil.copytree(SITE_SRC, SITE_OUT)
    data = json.dumps(store, separators=(",", ":"), default=str).encode()
    save_enc(SITE_OUT / "data.enc.json", data, passphrase)
    shutil.copy(SITE_OUT / "data.enc.json", STATE / "data.enc.json")
    log(f"Encrypted data written ({len(data) // 1024} KB before compression).")


def main():
    passphrase = os.getenv("DATA_PASSPHRASE")
    if not passphrase or len(passphrase) < 8:
        raise SystemExit("Set a DATA_PASSPHRASE repository secret (at least 8 characters, ideally a long phrase).")
    _secrets_to_redact.extend([os.getenv("GARMIN_EMAIL") or "", os.getenv("GARMIN_PASSWORD") or ""])
    budget = float(os.getenv("TIME_BUDGET_MIN", "55")) * 60
    deadline = time.time() + budget
    STATE.mkdir(exist_ok=True)
    set_output("deploy", "false")
    set_output("more", "false")

    store = load_store(passphrase)   # fails early with a clear message if the passphrase is wrong
    try:
        api = connect(passphrase)
    except NeedsMFA:
        msg = ("Garmin has sent you a verification code (email or text). Open Actions > Sync Garmin data > "
               "Run workflow again and paste the code into 'Garmin verification code' within a few minutes.")
        gh(f"::warning title=Verification code needed::{msg}")
        summary("## Verification code needed\n\n" + msg)
        log(msg)
        return

    call = Caller(api)
    end = dt.date.today()
    sync_account(call, store)
    sync_activities(call, store)

    start_env = os.getenv("START_DATE", "").strip()
    if start_env:
        start = dt.date.fromisoformat(start_env)
    elif store["meta"].get("start"):
        start = dt.date.fromisoformat(store["meta"]["start"])
    else:
        dates = [a["start"][:10] for a in store["activities"] if a.get("start")]
        start = dt.date.fromisoformat(min(dates)) if dates else end - dt.timedelta(days=730)
    if not store["meta"].get("start") or start.isoformat() < store["meta"]["start"]:
        store["meta"]["start"] = start.isoformat()
    log(f"History from {start}.")

    sync_weights(call, store, start, end)
    complete = sync_days(call, store, start, end, deadline)
    persist_tokens(api, passphrase)   # tokens may have been refreshed during the run

    store["generated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    store["meta"]["complete"] = complete
    build_site(store, passphrase)
    set_output("deploy", "true")
    set_output("more", "false" if complete else "true")
    status = "Up to date." if complete else "Still downloading history - another run has been started."
    summary(f"## Garmin sync\n\n{len(store['activities'])} activities, {len(store['days'])} days, "
            f"{len(store['weights'])} weigh-ins. {status}\n\n{call.calls} Garmin requests.")
    log(f"Done. {call.calls} requests. {status}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str):          # our own friendly messages
            e.code = redact(e.code)
            gh(f"::error title=Sync problem::{e.code}")
            summary("## Sync problem\n\n" + e.code)
            print(e.code, file=sys.stderr)
            sys.exit(1)
        raise
    except Exception as e:
        text = redact(short_err(e))
        if any(k in text for k in ("401", "403", "Unauthorized", "credentials", "password", "Authentication")):
            text = "Garmin rejected the login. Check GARMIN_EMAIL and GARMIN_PASSWORD. " + text
        gh(f"::error title=Sync problem::{text}")
        summary("## Sync problem\n\n" + text)
        raise
