"""Standalone CSC eServe availability checker for GitHub Actions.

Read-only: checks the availability feed, alerts Discord on changes.
Secrets arrive via environment variables, never printed or logged.
"""
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://services.csc.gov.ph"
PHT = ZoneInfo("Asia/Manila")
STATE = Path("state.json")
HISTORY = Path("history.ndjson")

LOC_ID = os.environ.get("LOC_ID", "195")
FORM_ID = os.environ.get("FORM_ID", "206")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
COOKIES_RAW = os.environ.get("CSC_COOKIES_JSON", "")
MONTHS = int(os.environ.get("MONTHS_AHEAD", "3"))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)


def log(msg):
    print(msg, flush=True)


def load_cookies():
    if not COOKIES_RAW.strip():
        return {}
    try:
        data = json.loads(COOKIES_RAW)
        items = data.get("cookies", []) if isinstance(data, dict) else data
        return {
            c["name"]: c["value"]
            for c in items
            if "csc.gov.ph" in (c.get("domain") or "")
        }
    except Exception as e:
        log(f"cookie parse failed: {type(e).__name__}")
        return {}


def make_session():
    """curl_cffi impersonates Chrome's TLS fingerprint: best chance with Cloudflare."""
    cookies = load_cookies()
    try:
        from curl_cffi import requests as creq

        s = creq.Session(impersonate="chrome", timeout=30)
        for k, v in cookies.items():
            s.cookies.set(k, v, domain=".csc.gov.ph")
        s.headers.update(
            {
                "User-Agent": UA,
                "Referer": BASE + "/client",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }
        )
        return s, "curl_cffi"
    except Exception as e:
        log(f"curl_cffi unavailable ({type(e).__name__}); using httpx fallback")
        import httpx

        s = httpx.Client(
            headers={
                "User-Agent": UA,
                "Referer": BASE + "/client",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            cookies=cookies,
            timeout=30,
            follow_redirects=False,
        )
        return s, "httpx"


def month_ranges(months_ahead):
    now = datetime.now(PHT)
    first = date(now.year, now.month, 1)
    out = []
    for i in range(max(1, months_ahead)):
        y, m = first.year, first.month + i
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        start = date(y, m, 1)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = date(ny, nm, 1)
        out.append(
            (
                f"{start.isoformat()}T00:00:00+08:00",
                f"{end.isoformat()}T00:00:00+08:00",
                start.strftime("%B %Y"),
            )
        )
    return out


def is_challenge(text):
    low = (text or "").lower()
    return "just a moment" in low or "challenge-platform" in low or "cf-chl" in low


def discord_send(content):
    if not WEBHOOK:
        log("DISCORD_WEBHOOK_URL not set; cannot alert")
        return False
    import httpx

    try:
        r = httpx.post(
            WEBHOOK,
            json={"content": content[:1900], "username": "CSC Slot Monitor"},
            timeout=15,
        )
        return r.status_code in (200, 204)
    except Exception:
        return False


def fetch_schedule_times(sess, lib, date_iso):
    try:
        if lib == "curl_cffi":
            r = sess.get(
                BASE + "/client/getSchedule",
                params={"date": date_iso, "location": LOC_ID, "form": FORM_ID},
            )
        else:
            r = sess.get(
                BASE + "/client/getSchedule",
                params={"date": date_iso, "location": LOC_ID, "form": FORM_ID},
            )
        if r.status_code != 200 or is_challenge(r.text):
            return []
        data = json.loads(r.text)
        items = data if isinstance(data, list) else []
        if isinstance(data, dict):
            for key in ("slots", "data", "schedules", "list", "result"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
        times = []
        for it in items:
            if isinstance(it, dict):
                t = str(it.get("time_from") or it.get("time") or it.get("start") or "")
                if t:
                    times.append(t[:5])
        return times
    except Exception:
        return []


def classify(entries):
    per = {}
    for e in entries:
        try:
            day = str(e.get("start", ""))[:10]
            pct = float(e.get("percent", 101))
        except Exception:
            continue
        if day:
            per.setdefault(day, []).append(pct)
    out = {}
    for day, pcts in per.items():
        avg = sum(pcts) / max(1, len(pcts))
        out[day] = "available" if avg < 100 else "booked"
    return out


def main():
    if not WEBHOOK:
        log("FATAL: DISCORD_WEBHOOK_URL secret missing")
        discord_send(
            "CSC monitor setup incomplete: the DISCORD_WEBHOOK_URL secret is "
            "missing. Add it in repo Settings > Secrets and variables > Actions."
        )
        sys.exit(1)

    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except Exception:
            state = {}

    prev_avail = {d for d, v in (state.get("availability") or {}).items()
                  if v.get("status") == "available"}

    if not load_cookies():
        log("no session cookies; session needs renewal")
        if not state.get("session_flagged"):
            discord_send(
                "**CSC session expired** (or not installed yet).\n"
                "Fix: run the probe login on the PC, then update the "
                "CSC_COOKIES_JSON secret with the fresh cookies file."
            )
            state["session_flagged"] = True
            STATE.write_text(json.dumps(state))
        sys.exit(0)

    sess, lib = make_session()
    log(f"lib={lib} checking {MONTHS} month range(s) for loc={LOC_ID} form={FORM_ID}")

    new_state = {}
    status = "ok"
    detail = ""
    try:
        for start_iso, end_iso, label in month_ranges(MONTHS):
            today = date.today().isoformat()
            params = {
                "loc_id": LOC_ID,
                "form_id": FORM_ID,
                "start_date": today,
                "start": start_iso,
                "end": end_iso,
            }
            if lib == "curl_cffi":
                r = sess.get(BASE + "/client/getEvents", params=params)
            else:
                r = sess.get(BASE + "/client/getEvents", params=params)
            log(f"{label}: HTTP {r.status_code}")
            if r.status_code in (301, 302, 303, 307, 401, 403) and not is_challenge(r.text):
                if r.status_code in (301, 302, 303, 307):
                    status, detail = "session_expired", "feed redirected to login"
                    break
                status, detail = "blocked", f"HTTP {r.status_code} on feed"
                break
            if r.status_code != 200:
                status, detail = "blocked", f"HTTP {r.status_code} on feed"
                break
            if is_challenge(r.text):
                status, detail = "blocked", "Cloudflare challenge (cookies/IP mismatch)"
                break
            try:
                entries = json.loads(r.text)
            except Exception:
                status, detail = "blocked", f"non-JSON response: {r.text[:80]!r}"
                break
            if not isinstance(entries, list):
                status, detail = "blocked", f"unexpected payload: {r.text[:80]!r}"
                break
            for day, st in classify(entries).items():
                cur = new_state.get(day)
                if st == "available" or cur == "available":
                    new_state[day] = {"status": "available", "times": []}
                else:
                    new_state[day] = {"status": "booked", "times": []}
    except Exception as e:
        status, detail = "error", f"{type(e).__name__}: {e}"
        log(f"error: {detail}")

    now_txt = datetime.now(PHT).strftime("%Y-%m-%d %H:%M PHT")

    if status == "ok":
        state["session_flagged"] = False
        state["last_ok"] = now_txt
        avail = sorted(d for d, v in new_state.items() if v["status"] == "available")
        newly = [d for d in avail if d not in prev_avail]
        log(f"available: {len(avail)} {avail} | full: {len(new_state) - len(avail)}")

        if newly:
            budget = 3
            for d in newly:
                if budget <= 0:
                    break
                new_state[d]["times"] = fetch_schedule_times(sess, lib, d)
                budget -= 1
            lines = [
                "**SLOT ALERT - CSC eServe**",
                "Exam: **Career Service - Professional**",
                "Location: **SM City Sorsogon (Events Center)**",
                "",
                "**Newly available:**",
            ]
            for d in newly:
                t = new_state[d].get("times") or []
                lines.append(f"  {d}" + (f" at {', '.join(t[:12])}" if t else ""))
            lines += [
                "",
                f"Detected: {now_txt}",
                "Book NOW (you do the booking): https://services.csc.gov.ph/",
            ]
            ok = discord_send("\n".join(lines))
            log(f"discord alert sent: {ok}")
            if HISTORY.exists() is False:
                HISTORY.touch()
            with HISTORY.open("a", encoding="utf-8") as f:
                for d in newly:
                    f.write(json.dumps({
                        "ts": now_txt, "date": d,
                        "times": new_state[d].get("times") or [],
                    }) + "\n")

    elif status == "session_expired":
        if not state.get("session_flagged"):
            discord_send(
                "**CSC session expired.** Monitoring paused until you refresh "
                "the CSC_COOKIES_JSON secret (probe login on PC, copy new "
                "cookies file into the secret)."
            )
            state["session_flagged"] = True
        log(f"session expired: {detail}")

    else:
        log(f"status={status} detail={detail}")
        if not state.get("blocked_flagged") or state.get("blocked_date") != now_txt[:10]:
            discord_send(
                f"**Monitoring problem** ({status}): {detail}\n"
                "It will retry automatically. If this repeats all day, the "
                "session or cookies need attention."
            )
            state["blocked_flagged"] = True
            state["blocked_date"] = now_txt[:10]
        if status == "blocked":
            pass  # retry next run
    if status == "ok":
        state["blocked_flagged"] = False

    state["availability"] = new_state
    state["last_run"] = now_txt
    state["last_status"] = status
    STATE.write_text(json.dumps(state, indent=1))
    log("state saved")


if __name__ == "__main__":
    main()
