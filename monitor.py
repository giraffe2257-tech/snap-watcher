#!/usr/bin/env python3
"""Eurostar Snap price monitor.

For each configured route, opens the Snap results page directly, reads the
lowest price for the target date, and if it is at/below the threshold, fires a
loud macOS alert (volume up + alarm sound + spoken message + notification) and,
on the first hit, opens the booking page in the browser.

Runs once per invocation; scheduled every 5 min by launchd. See install.sh.
"""
import asyncio, json, re, os, sys, subprocess, datetime, platform
import urllib.request, urllib.parse
from playwright.async_api import async_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(HERE, "state.json")
LOG = os.path.join(HERE, "monitor.log")
BASE = "https://snap.eurostar.com/uk-en/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_day_label(label, anchor):
    """'Friday 4 September' -> date(2026, 9, 4). Year taken from `anchor`."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)", label or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(2)[:3].lower())
    if not mon:
        return None
    year = anchor.year + (1 if mon < anchor.month else 0)
    try:
        return datetime.date(year, mon, int(m.group(1)))
    except ValueError:
        return None


def route_url(route, iso):
    return (f"{BASE}?adult=1&origin={route['origin']}"
            f"&destination={route['destination']}&outbound={iso}")


def route_span(route):
    """Routes take either a single `outbound` or an `outbound_from`/`_to` range."""
    if route.get("outbound_from"):
        start = datetime.date.fromisoformat(route["outbound_from"])
        end = datetime.date.fromisoformat(route.get("outbound_to") or route["outbound_from"])
    else:
        start = end = datetime.date.fromisoformat(route["outbound"])
    return start, end


async def extract_days(page):
    return await page.evaluate(r"""() => {
        const out=[];
        document.querySelectorAll('[data-testid="calendarDay"]').forEach(el=>{
            const txt=(el.innerText||'').replace(/\s+/g,' ').trim();
            const dm=txt.match(/[A-Za-z]+day\s+\d{1,2}\s+[A-Za-z]+/);
            const pm=txt.match(/£\s?(\d+(?:\.\d+)?)/);
            out.push({date: dm?dm[0]:txt.slice(0,30),
                      price: pm?parseFloat(pm[1]):null,
                      raw: pm?('£'+pm[1]):(txt.includes('-')?'-':'')});
        });
        return out;
    }""")


async def load_calendar(browser, route, anchor):
    """Open the Snap page anchored on one date. Returns (days, error, url).

    The page renders a strip of neighbouring dates with their prices, so a
    single load covers several days of a range.
    """
    url = route_url(route, anchor.isoformat())
    ctx = await browser.new_context(locale="en-GB", viewport={"width": 1280, "height": 900}, user_agent=UA)
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(1000)
        try:
            await page.get_by_role("button", name=re.compile("Accept all", re.I)).click(timeout=3000)
        except Exception:
            pass
        days = []
        for _ in range(18):
            days = await extract_days(page)
            if days and any(d["raw"] for d in days):
                break
            await page.wait_for_timeout(1000)
        return days, None, url
    except Exception as e:
        return [], str(e)[:200], url
    finally:
        await ctx.close()


async def check_route(browser, route):
    """Return one result dict per on-sale date in the route's span.

    Keeps re-anchoring the calendar at the first date it has not seen yet, so a
    month-long span costs a handful of page loads rather than one per day. A
    load that turns up no new in-range date means Snap is not selling that far
    ahead yet, so we stop there.
    """
    start, end = route_span(route)
    found, anchor = {}, start
    while anchor <= end:
        days, err, url = await load_calendar(browser, route, anchor)
        if err:
            return [{"date": None, "name": route["name"], "price": None,
                     "raw": None, "error": err, "url": url}]
        new = 0
        for d in days:
            dt = parse_day_label(d["date"], anchor)
            if dt and start <= dt <= end and dt.isoformat() not in found:
                found[dt.isoformat()] = d
                new += 1
        if not new:
            break
        while anchor <= end and anchor.isoformat() in found:
            anchor += datetime.timedelta(days=1)
    out = []
    for iso in sorted(found):
        d = found[iso]
        label = datetime.date.fromisoformat(iso).strftime("%a %-d %b")
        out.append({"date": iso, "name": f"{route['name']} {label}",
                    "price": d["price"], "raw": d["raw"] or "-",
                    "error": None, "url": route_url(route, iso)})
    return out


# ---------- WhatsApp push (CallMeBot) ----------
def notify_whatsapp(cfg, text):
    """Send `text` to the configured WhatsApp number via CallMeBot.

    Credentials come from the environment first (CALLMEBOT_PHONE / CALLMEBOT_APIKEY)
    so the key never has to live in config.json when this runs in CI.
    """
    wa = (cfg.get("notify") or {}).get("callmebot") or {}
    if not wa.get("enabled", False):
        return
    phone = os.environ.get("CALLMEBOT_PHONE") or wa.get("phone", "")
    apikey = os.environ.get("CALLMEBOT_APIKEY") or wa.get("apikey", "")
    if not phone or not apikey:
        log("  WhatsApp: skipped (phone/apikey not set)")
        return
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": apikey})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            body = r.read(300).decode("utf-8", "replace").strip()
        log(f"  WhatsApp: sent ({r.status}) {body[:120]}")
    except Exception as e:
        log(f"  WhatsApp: FAILED {str(e)[:200]}")


# ---------- macOS alert ----------
def sh(cmd):
    subprocess.run(cmd, shell=True, capture_output=True)


def mac_alert(cfg, route, price, url, first_hit):
    vol = cfg.get("volume", 80)
    sh(f"osascript -e 'set volume output volume {vol}'")
    title = "Eurostar Snap deal!"
    body = f"{route['name']}: £{price:g} (<= £{cfg['threshold_gbp']})"
    # persistent-ish notification with sound
    sh(f"osascript -e 'display notification {json.dumps(body)} with title "
       f"{json.dumps(title)} sound name \"Sosumi\"'")
    # loud alarm sound loop
    snd = cfg.get("alarm_sound", "/System/Library/Sounds/Sosumi.aiff")
    for _ in range(int(cfg.get("alarm_repeat", 6))):
        sh(f"afplay {json.dumps(snd)}")
    # spoken announcement
    spoken = route.get("speak", route["name"])
    sh(f"say {json.dumps(f'Snap ticket found. {spoken}. {int(round(price))} pounds. Book now.')}")
    sh(f"say {json.dumps(f'{int(round(price))} pounds. Book now.')}")
    # open the booking page only on the first hit (avoid tab spam)
    if first_hit and cfg.get("open_browser_on_first_hit", True):
        sh(f"open {json.dumps(url)}")


def digest():
    """Check every route once and send a single summary message.

    Doubles as a heartbeat: if this stops arriving, something in the chain
    (the schedule, the scraper, or CallMeBot) has broken silently.
    """
    cfg = load_json(CONFIG, None)
    if not cfg:
        log("ERROR: config.json missing/invalid"); return
    threshold = cfg["threshold_gbp"]
    lines = []

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for route in cfg["routes"]:
                results = await check_route(browser, route)
                if not results:
                    lines.append(f"{route['name']}: nothing on sale in range yet")
                    log(f"digest: {lines[-1]}")
                under = [r for r in results
                         if r["price"] is not None and r["price"] <= threshold]
                for res in results:
                    if res["error"]:
                        lines.append(f"{res['name']}: check failed")
                        log(f"{res['name']}: ERROR {res['error']}")
                if under:
                    cheap = min(under, key=lambda r: r["price"])
                    lines.append(f"{route['name']}: {len(under)} date(s) at or below "
                                 f"GBP {threshold}, cheapest {cheap['name']} "
                                 f"GBP {cheap['price']:g}")
                    log(f"digest: {lines[-1]}")
                elif results:
                    priced = [r for r in results if r["price"] is not None]
                    if priced:
                        cheap = min(priced, key=lambda r: r["price"])
                        lines.append(f"{route['name']}: nothing under GBP {threshold}, "
                                     f"cheapest {cheap['name']} GBP {cheap['price']:g}")
                    else:
                        lines.append(f"{route['name']}: on sale but no prices shown")
                    log(f"digest: {lines[-1]}")
            await browser.close()

    asyncio.run(run())
    today = datetime.date.today().strftime("%-d %b")
    notify_whatsapp(cfg, "\U0001f68a Snap watcher daily check - " + today + "\n"
                    + "\n".join(lines)
                    + f"\n\nAlerts fire at or below GBP {threshold}. Checking every 30 min.")


def alert(cfg, hits):
    """Fire every enabled channel once for all qualifying dates in this run.

    Batched on purpose: a month-long span can qualify on many dates at once, and
    one message per date would flood WhatsApp and trip CallMeBot's rate limit.
    """
    if not hits:
        return
    lines = [f"{h['name']}: GBP {h['price']:g}" for h in hits[:20]]
    if len(hits) > 20:
        lines.append(f"...and {len(hits) - 20} more dates")
    cheapest = min(hits, key=lambda h: h["price"])
    notify_whatsapp(cfg, (
        f"\U0001f3ab Eurostar Snap deal ({len(hits)} date"
        f"{'s' if len(hits) != 1 else ''} at or below GBP {cfg['threshold_gbp']})\n"
        + "\n".join(lines)
        + f"\n\nCheapest: {cheapest['name']} GBP {cheapest['price']:g}\n"
        f"Book: {cheapest['url']}"))
    on_mac = platform.system() == "Darwin"
    if on_mac and (cfg.get("notify") or {}).get("mac_alarm", True):
        mac_alert(cfg, cheapest, cheapest["price"], cheapest["url"],
                  any(h["first_hit"] for h in hits))


def main():
    cfg = load_json(CONFIG, None)
    if not cfg:
        log("ERROR: config.json missing/invalid"); return
    state = load_json(STATE, {})
    threshold = cfg["threshold_gbp"]

    # ---- frequency gating: dense every N min inside any dense window, else every M min ----
    now = datetime.datetime.now()
    windows = cfg.get("dense_windows")
    if windows is None:
        dw = cfg.get("dense_window")
        windows = [dw] if dw else []
    dense = False
    required = cfg.get("default_interval_min", 30)
    for w in windows:
        if w.get("start_hour", 6) <= now.hour <= w.get("end_hour", 8):
            dense = True
            required = w.get("interval_min", 5)
            break
    last = state.get("_last_check")
    since = 1e9
    if last:
        try:
            since = (now - datetime.datetime.fromisoformat(last)).total_seconds() / 60
        except Exception:
            since = 1e9
    # In CI the cron itself sets the cadence, so the local gate is bypassed.
    if since < required - 1 and os.environ.get("SNAP_FORCE_CHECK") != "1":
        print(f"[{now:%H:%M:%S}] skip: {since:.0f}min since last (<{required}min, "
              f"{'dense' if dense else 'normal'} window)")
        return
    state["_last_check"] = now.isoformat(timespec="seconds")
    realert_after = cfg.get("realert_after_min", 60)

    def should_alert(prev, price):
        """Re-notify on a new/cheaper price, or once `realert_after_min` has passed."""
        if not prev.get("alerted"):
            return True, True
        if prev.get("last_price") != price:
            return True, False
        try:
            gap = (now - datetime.datetime.fromisoformat(prev["alerted_at"])).total_seconds() / 60
        except Exception:
            return True, False
        return gap >= realert_after, False

    hits = []

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for route in cfg["routes"]:
                results = await check_route(browser, route)
                if not results:
                    log(f"{route['name']}: nothing on sale in range yet")
                for res in results:
                    if res["error"]:
                        log(f"{res['name']}: ERROR {res['error']}")
                        continue
                    key = f"{route['origin']}-{route['destination']}-{res['date']}"
                    price = res["price"]
                    log(f"{res['name']}: {res['raw']}"
                        + (f"  (threshold £{threshold})" if price is not None else ""))
                    prev = state.get(key, {})
                    if price is not None and price <= threshold:
                        fire, first_hit = should_alert(prev, price)
                        if fire:
                            log(f"  >>> DEAL £{price:g} <= £{threshold} — ALERT "
                                f"({'first' if first_hit else 'repeat'})")
                            hits.append({**res, "first_hit": first_hit})
                            state[key] = {"alerted": True, "last_price": price,
                                          "alerted_at": now.isoformat(timespec="seconds")}
                        else:
                            log(f"  >>> DEAL £{price:g} — already alerted, holding "
                                f"(re-alert after {realert_after}min)")
                            state[key] = prev
                    else:
                        state[key] = {"alerted": False, "last_price": price}
            await browser.close()

    asyncio.run(run())
    alert(cfg, hits)
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    if "--test-notify" in sys.argv:
        cfg = load_json(CONFIG, {}) or {}
        notify_whatsapp(cfg, "✅ Snap watcher test message. If you see this, "
                             "WhatsApp alerts are working.")
    elif "--digest" in sys.argv:
        digest()
    else:
        main()
