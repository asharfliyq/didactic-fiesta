import sys
import time
import signal
import logging
import re
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import db
import fetcher
import release_parser as parser
import matcher
import tracker
import velocity
import patterns
import notifier
import quiet
from bot import start_polling, stop_polling
from models import ProfileMode

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("main")

alive = True
empty_cycles = 0
last_pattern_calc = 0
last_forecast = 0
last_weekly = 0

STARTUP_LOOKBACK = config.STARTUP_LOOKBACK
FALLBACK_COUNT = 10
MAX_ITEMS_PER_FEED = 50
SEEN_STREAK_BREAK = 5


def _stop(sig, frame):
    global alive
    log.info("Shutdown signal received")
    alive = False


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def compute_interval(had_tier1: bool) -> int:
    global empty_cycles
    si = config.get_smart_interval()
    base = config.BASE_INTERVAL

    if had_tier1:
        empty_cycles = 0
        return si.get("tier1_burst", 20)

    now_h = datetime.now(timezone.utc).hour
    peak = si.get("peak_hours", [16, 23])
    dead = si.get("dead_hours", [2, 7])

    if peak[0] <= now_h < peak[1]:
        base = int(base * si.get("peak_multiplier", 0.5))
    elif dead[0] <= now_h < dead[1]:
        base = int(base * si.get("dead_multiplier", 2.0))

    cooldown_threshold = si.get("cooldown_after_empty", 3)
    if empty_cycles >= cooldown_threshold:
        base = int(base * si.get("cooldown_multiplier", 1.5))

    return max(10, min(base, 300))


def _normalize_tracker_title(entry):
    if entry.tracker == "ar":
        entry.title = re.sub(
            r'^(?:TvHD|TvSD|TvUHD|TvPackHD|MovieHD|MovieSD|Movie4K|GamesPC|AppsPC|MusicHD|MusicSD|EBooks|AudioBooks)\s+\d+\s+\d+\s+',
            '', entry.title
        ).strip()


def _normalize_family_by_tmdb(entry, parsed):
    if not parsed.clean_name:
        return

    if entry.tmdb_id:
        canonical = _get_canonical_name_by_tmdb(entry.tmdb_id)
        if canonical and canonical.lower() != parsed.clean_name.lower():
            parsed.clean_name = canonical
            parsed.family_key = ""
            parsed.variant_key = ""
            parsed.exact_key = ""
            parser._build_keys(parsed)
            return

    if entry.imdb_id:
        try:
            import tmdb as tmdb_mod
            data = tmdb_mod.lookup_by_imdb(entry.imdb_id)
            if data and data.get("title"):
                canonical = data["title"]
                if canonical.lower() != parsed.clean_name.lower():
                    parsed.clean_name = canonical
                    entry.tmdb_id = data.get("tmdb_id", 0)
                    parsed.family_key = ""
                    parsed.variant_key = ""
                    parsed.exact_key = ""
                    parser._build_keys(parsed)
                    return
        except Exception:
            pass

    try:
        import tmdb as tmdb_mod
        from models import ContentType

        is_tv = parsed.content_type in (
            ContentType.EPISODE, ContentType.SEASON_PACK,
            ContentType.ANIME_EP, ContentType.ANIME_BATCH,
            ContentType.COMPLETE,
        )

        if is_tv:
            data = tmdb_mod.search_tv(parsed.clean_name)
        else:
            data = tmdb_mod.search_movie(parsed.clean_name, parsed.year)

        if data and data.get("tmdb_id"):
            tmdb_id = data["tmdb_id"]
            canonical = _get_canonical_name_by_tmdb(tmdb_id)

            if canonical and canonical.lower() != parsed.clean_name.lower():
                parsed.clean_name = canonical
                entry.tmdb_id = tmdb_id
                parsed.family_key = ""
                parsed.variant_key = ""
                parsed.exact_key = ""
                parser._build_keys(parsed)
            elif not canonical:
                entry.tmdb_id = tmdb_id
    except Exception:
        pass

def check_weekly_summary():
    global last_weekly
    from datetime import datetime
    now = datetime.now()

    if now.weekday() != 6:
        return
    if now.hour != 10:
        return
    if time.time() - last_weekly < 82800:
        return

    last_weekly = time.time()

    stats = db.get_stats()
    tracked = db.get_all_tracked_shows()

    lines = ["📊 <b>Weekly Summary</b>\n"]

    lines.append(f"📦 Total: {stats['total']} torrents")
    lines.append(f"📅 This week: {stats['today']} (today)")
    lines.append(f"📨 Notified: {stats['notified']}")
    lines.append(f"📺 Tracking: {stats['tracked_shows']} shows")
    lines.append("")

    if tracked:
        lines.append("<b>Show Status:</b>")
        for s in tracked[:15]:
            if s.latest_episode:
                status = f"S{s.latest_season:02d}E{s.latest_episode:02d}"
            else:
                status = "waiting"
            lines.append(f"  • {s.show_name} — {status}")
        lines.append("")

    import sqlite3
    conn = sqlite3.connect(str(config.DB_MAIN))
    conn.row_factory = sqlite3.Row

    new_shows = conn.execute("""
        SELECT DISTINCT parsed_name FROM torrents
        WHERE content_type = 'episode'
          AND parsed_season = 1
          AND parsed_episode = 1
          AND first_seen > ?
        ORDER BY first_seen DESC
        LIMIT 10
    """, (time.time() - 604800,)).fetchall()

    if new_shows:
        lines.append("<b>New Shows This Week:</b>")
        for s in new_shows:
            lines.append(f"  🌟 {s['parsed_name']}")
        lines.append("")

    top_groups = conn.execute("""
        SELECT parsed_group, COUNT(*) as cnt
        FROM torrents
        WHERE first_seen > ? AND parsed_group IS NOT NULL AND parsed_group != ''
        GROUP BY parsed_group
        ORDER BY cnt DESC
        LIMIT 10
    """, (time.time() - 604800,)).fetchall()

    if top_groups:
        lines.append("<b>Top Groups This Week:</b>")
        for g in top_groups:
            lines.append(f"  {g['parsed_group']}: {g['cnt']} releases")
        lines.append("")

    by_tracker = conn.execute("""
        SELECT tracker, COUNT(*) as cnt
        FROM torrents
        WHERE first_seen > ?
        GROUP BY tracker
        ORDER BY cnt DESC
    """, (time.time() - 604800,)).fetchall()

    if by_tracker:
        lines.append("<b>By Tracker:</b>")
        for t in by_tracker:
            lines.append(f"  {t['tracker']}: {t['cnt']}")

    conn.close()

    notifier.send_raw("\n".join(lines))

def _get_canonical_name_by_tmdb(tmdb_id):
    if not tmdb_id:
        return ""
    try:
        import sqlite3
        conn = sqlite3.connect(str(config.DB_MAIN))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT parsed_name FROM torrents WHERE tmdb_id=? AND parsed_name IS NOT NULL LIMIT 1",
            (tmdb_id,)
        ).fetchone()
        conn.close()
        if row:
            return row["parsed_name"]
    except Exception:
        pass
    return ""


def process_new_entry(entry):
    _normalize_tracker_title(entry)

    parsed = parser.parse(entry.title, entry.category)
    entry.parsed = parsed

    if not parsed.group and entry.uploader:
        parsed.group = entry.uploader

    _normalize_family_by_tmdb(entry, parsed)

    if parsed.group and parsed.group.lower() in [g.lower() for g in config.get_internal_groups()]:
        parsed.is_internal = True

    db.insert_torrent(entry, parsed)

    if parsed.group:
        db.log_upload(parsed.group, entry.category, parsed.content_type.value, entry.pub_timestamp)

    velocity.track_seeders(entry)

    matches = matcher.match_entry(entry, parsed)
    if matches == "__OLDER_SKIPPED__":
        return "__OLDER_SKIPPED__"
    if not matches:
        return None

    ep_info = tracker.process_episode(entry, parsed)
    pack_info = tracker.process_season_pack(entry, parsed)

    return {
        "entry": entry,
        "parsed": parsed,
        "matches": matches,
        "ep_info": ep_info,
        "pack_info": pack_info,
    }


def send_aggregated_alerts(pending_alerts):
    families = {}

    for alert in pending_alerts:
        for match_result in alert["matches"]:
            fkey = match_result.family_key or alert["parsed"].family_key or str(alert["entry"].torrent_id)

            if fkey not in families:
                families[fkey] = {
                    "best_match": match_result,
                    "ep_info": alert["ep_info"],
                    "pack_info": alert.get("pack_info"),
                    "had_tier1": match_result.group_tier.value == "tier1",
                    "count": 1,
                }
            else:
                existing = families[fkey]
                existing["count"] += 1

                if match_result.group_tier.value == "tier1":
                    existing["had_tier1"] = True

                if match_result.profile_mode == ProfileMode.RACE and existing["best_match"].profile_mode != ProfileMode.RACE:
                    match_result.matched_keywords = list(dict.fromkeys(
                        match_result.matched_keywords + existing["best_match"].matched_keywords
                    ))
                    if existing["best_match"].profile_name not in match_result.profile_name:
                        match_result.profile_name = f"{match_result.profile_name} + {existing['best_match'].profile_name}"
                    match_result.dupe_entries = list(dict.fromkeys(
                        match_result.dupe_entries + existing["best_match"].dupe_entries
                    ))
                    existing["best_match"] = match_result
                else:
                    existing["best_match"].matched_keywords = list(dict.fromkeys(
                        existing["best_match"].matched_keywords + match_result.matched_keywords
                    ))
                    if match_result.profile_name not in existing["best_match"].profile_name:
                        existing["best_match"].profile_name = f"{existing['best_match'].profile_name} + {match_result.profile_name}"
                    existing["best_match"].dupe_entries = list(dict.fromkeys(
                        existing["best_match"].dupe_entries + match_result.dupe_entries
                    ))
                    existing["best_match"].is_dupe = existing["best_match"].is_dupe or match_result.is_dupe

                if not existing["ep_info"] and alert["ep_info"]:
                    existing["ep_info"] = alert["ep_info"]

                if not existing["pack_info"] and alert.get("pack_info"):
                    existing["pack_info"] = alert.get("pack_info")

    show_seasons = {}
    for fkey, fdata in families.items():
        p = fdata["best_match"].entry.parsed
        if not p:
            continue
        if p.season is None or p.episode is None:
            continue

        show_key = f"{(p.clean_name or '').lower()}|s{p.season:02d}|{(p.resolution or '').lower()}"

        if show_key not in show_seasons:
            show_seasons[show_key] = []
        show_seasons[show_key].append((fkey, p.episode, fdata))

    suppress_keys = set()
    episode_summaries = {}

    for show_key, episodes in show_seasons.items():
        if len(episodes) <= 1:
            continue

        episodes.sort(key=lambda x: x[1], reverse=True)

        latest_fkey = episodes[0][0]
        latest_ep = episodes[0][1]

        other_eps = sorted([ep[1] for ep in episodes[1:]])

        for fkey, ep_num, fdata in episodes[1:]:
            suppress_keys.add(fkey)
            db.mark_notified(fdata["best_match"].entry.torrent_id, fdata["best_match"].profile_name)

        episode_summaries[latest_fkey] = {
            "latest_ep": latest_ep,
            "other_eps": other_eps,
            "total": len(episodes),
        }

    had_tier1 = False
    sent_count = 0
    deleted_messages = set()

    for fkey, family_data in families.items():
        if fkey in suppress_keys:
            continue

        result = family_data["best_match"]
        ep_info = family_data["ep_info"]
        pack_info = family_data.get("pack_info")

        if pack_info and pack_info.get("deleted_notifications"):
            for notif in pack_info["deleted_notifications"]:
                chat_id = str(notif.get("chat_id", "")).strip()
                msg_id = int(notif.get("message_id", 0))
                if chat_id and msg_id:
                    msg_key = (chat_id, msg_id)
                    if msg_key in deleted_messages:
                        continue
                    notifier.delete_message(chat_id, msg_id)
                    deleted_messages.add(msg_key)

        if family_data["had_tier1"]:
           had_tier1 = True

        old_notifs = matcher._get_family_notifications(fkey)
        for notif in old_notifs:
           chat_id = str(notif.get("chat_id", "")).strip()
           msg_id = notif.get("message_id", 0)
           msg_key = (chat_id, msg_id)
           if chat_id and msg_id and msg_key not in deleted_messages:
               notifier.delete_message(chat_id, msg_id)
               deleted_messages.add(msg_key)

        p = result.entry.parsed
        if p and p.season is not None and p.episode is not None:
           _delete_previous_episode_alerts(p.clean_name, p.season, p.episode)

        for suppressed_fkey in suppress_keys:
           if suppressed_fkey.startswith(fkey.rsplit("|", 1)[0]):
               old_suppressed = matcher._get_family_notifications(suppressed_fkey)
               for notif in old_suppressed:
                   chat_id = str(notif.get("chat_id", "")).strip()
                   msg_id = notif.get("message_id", 0)
                   msg_key = (chat_id, msg_id)
                   if chat_id and msg_id and msg_key not in deleted_messages:
                       notifier.delete_message(chat_id, msg_id)
                       deleted_messages.add(msg_key)

        if fkey in episode_summaries:
           summary = episode_summaries[fkey]
           ep_list = ", ".join(f"E{e:02d}" for e in summary["other_eps"])
           result.matched_keywords.append(f"batch:{summary['total']} episodes (also {ep_list})")

        if quiet.is_quiet():
           quiet.queue_notification({
               "title": result.entry.title,
               "mode": result.profile_mode.value,
               "tier": result.group_tier.value,
               "profile": result.profile_name,
           })
           log.info(f"QUEUED: {result.entry.title[:80]}")
        else:
           notifier.send_match(result, ep_info, pack_info)
           sent_count += 1
           time.sleep(0.1)

    return had_tier1, sent_count

def _delete_previous_episode_alerts(show_name, season, current_episode):
    if not show_name or season is None or current_episode is None:
        return
    if current_episode <= 1:
        return

    import sqlite3

    try:
        mconn = sqlite3.connect(str(config.DB_MAIN))
        mconn.row_factory = sqlite3.Row
        hconn = sqlite3.connect(str(config.DB_HISTORY))
        hconn.row_factory = sqlite3.Row

        prev_torrents = mconn.execute("""
            SELECT torrent_id FROM torrents
            WHERE parsed_name = ? COLLATE NOCASE
              AND parsed_season = ?
              AND parsed_episode < ?
              AND notified = 1
        """, (show_name, season, current_episode)).fetchall()

        for t in prev_torrents:
            notifs = hconn.execute(
                "SELECT chat_id, message_id FROM notifications WHERE torrent_id=?",
                (t["torrent_id"],)
            ).fetchall()
            for n in notifs:
                chat_id = n["chat_id"]
                msg_id = n["message_id"]
                if chat_id and msg_id:
                    notifier.delete_message(str(chat_id), msg_id)

        mconn.close()
        hconn.close()
    except Exception:
        pass

def run_cycle(is_first: bool = False) -> bool:
    global empty_cycles
    feeds = config.get_feeds()
    total_new = 0
    total_skipped = 0
    total_seen = 0
    total_older_skipped = 0
    now = time.time()
    cutoff = now - STARTUP_LOOKBACK

    all_entries = []

    def fetch_one(feed_cfg):
        url = feed_cfg["url"]
        fname = feed_cfg.get("name", url[:40])
        tracker_type = feed_cfg.get("tracker", "tl")
        return fetcher.fetch_feed(url, fname, tracker_type)

    fetch_start = time.time()

    with ThreadPoolExecutor(max_workers=max(1, len(feeds))) as executor:
        future_map = {executor.submit(fetch_one, fc): fc for fc in feeds}
        for future in as_completed(future_map):
            try:
                entries = future.result()
                all_entries.extend(entries)
            except Exception as e:
                fc = future_map[future]
                log.error(f"Feed error [{fc.get('name', '?')}]: {e}")

    fetch_time = time.time() - fetch_start

    dedup_by_id = {}
    for e in all_entries:
        if e.torrent_id not in dedup_by_id:
            dedup_by_id[e.torrent_id] = e

    all_entries = sorted(
        dedup_by_id.values(),
        key=lambda x: x.pub_timestamp or 0,
        reverse=True,
    )

    pending_alerts = []
    process_start = time.time()

    if is_first:
        recent_entries = [e for e in all_entries if e.pub_timestamp and e.pub_timestamp >= cutoff]
        if not recent_entries and all_entries:
            recent_entries = all_entries[:FALLBACK_COUNT]

        skipped = [e for e in all_entries if e not in recent_entries]
        total_skipped = len(skipped)

        for entry in skipped:
            _normalize_tracker_title(entry)
            if not db.torrent_exists(entry.torrent_id):
                parsed = parser.parse(entry.title, entry.category)
                db.insert_torrent(entry, parsed)
                if parsed.group:
                    db.log_upload(parsed.group, entry.category, parsed.content_type.value, entry.pub_timestamp)

        for entry in recent_entries:
            if not alive:
                break
            if db.torrent_exists(entry.torrent_id):
                total_seen += 1
                continue
            result = process_new_entry(entry)
            total_new += 1
            if result == "__OLDER_SKIPPED__":
                total_older_skipped += 1
            elif result:
                pending_alerts.append(result)
    else:
        by_feed = {}
        for entry in all_entries:
            feed = entry.feed_source or "unknown"
            if feed not in by_feed:
                by_feed[feed] = []
            by_feed[feed].append(entry)

        for feed_name, entries in by_feed.items():
            seen_streak = 0
            processed = 0

            for entry in entries:
                if not alive:
                    break
                if processed >= MAX_ITEMS_PER_FEED:
                    break
                if seen_streak >= SEEN_STREAK_BREAK:
                    break

                processed += 1

                if db.torrent_exists(entry.torrent_id):
                    seen_streak += 1
                    total_seen += 1
                    continue

                seen_streak = 0
                result = process_new_entry(entry)
                total_new += 1
                if result == "__OLDER_SKIPPED__":
                    total_older_skipped += 1
                elif result:
                    pending_alerts.append(result)

    process_time = time.time() - process_start

    had_tier1 = False
    total_sent = 0
    send_start = time.time()

    if pending_alerts and alive:
        had_tier1, total_sent = send_aggregated_alerts(pending_alerts)

    send_time = time.time() - send_start

    if total_new == 0:
        empty_cycles += 1
    else:
        empty_cycles = 0

    families_count = len(set(
        a["parsed"].family_key for a in pending_alerts if a["parsed"].family_key
    )) if pending_alerts else 0

    if is_first:
        log.info(
            f"First — New:{total_new} Sent:{total_sent} Skip:{total_skipped} OlderSkipped:{total_older_skipped} "
            f"Fam:{families_count} "
            f"[fetch:{fetch_time:.1f}s proc:{process_time:.1f}s send:{send_time:.1f}s]"
        )
    else:
        log.info(
            f"New:{total_new} Sent:{total_sent} Seen:{total_seen} OlderSkipped:{total_older_skipped} "
            f"Fam:{families_count} Empty:{empty_cycles} "
            f"[fetch:{fetch_time:.1f}s proc:{process_time:.1f}s send:{send_time:.1f}s]"
        )

    return had_tier1


def check_quiet_transition():
    if not quiet.is_quiet():
        items = quiet.flush_queue()
        if items:
            summary = quiet.build_summary(items)
            if summary:
                notifier.send_raw(summary)


def check_patterns():
    global last_pattern_calc, last_forecast
    now = time.time()

    if patterns.should_recalculate():
        patterns.recalculate_patterns()
        last_pattern_calc = now

    if now - last_forecast > 21600:
        forecast = patterns.get_daily_forecast()
        if forecast and not quiet.is_quiet():
            notifier.send_raw(forecast)
        last_forecast = now


def main():
    errs = config.validate()
    if errs:
        for e in errs:
            log.error(f"Config: {e}")
        if any("BOT_TOKEN" in e or "CHAT_ID" in e for e in errs):
            sys.exit(1)

    config.DATA_DIR.mkdir(exist_ok=True)
    db.init_all()
    tracker.init_tracked_shows()

    feeds_count = len(config.get_feeds())
    profiles_count = len(config.get_profiles())
    chats_count = len(config.CHAT_IDS)

    log.info("=" * 50)
    log.info("Torrent Monitor started")
    log.info(f"Feeds: {feeds_count}")
    log.info(f"Profiles: {profiles_count}")
    log.info(f"Chat IDs: {chats_count}")
    log.info(f"Base interval: {config.BASE_INTERVAL}s")
    log.info(f"Max items/feed: {MAX_ITEMS_PER_FEED}")
    log.info(f"Seen streak break: {SEEN_STREAK_BREAK}")
    log.info("=" * 50)

    start_polling()

    notifier.send_raw(
        f"🟢 Torrent Monitor started\n"
        f"📡 {feeds_count} feeds | 🎯 {profiles_count} profiles | 💬 {chats_count} chats"
    )
    notifier.send_raw("For details /help")

    cycle = 0
    while alive:
        cycle += 1
        log.info(f"── Cycle #{cycle} ──")

        try:
            had_tier1 = run_cycle(is_first=(cycle == 1))
        except Exception as e:
            log.exception(f"Cycle error: {e}")
            had_tier1 = False

        check_quiet_transition()

        try:
            check_weekly_summary()
        except Exception:
            pass
            log.error(f"Pattern check error: {e}")

        if cycle % 100 == 0:
            try:
                db.cleanup_old_data(30)
            except Exception:
                pass

        interval = compute_interval(had_tier1)
        log.info(f"Next check in {interval}s")

        for _ in range(interval):
            if not alive:
                break
            time.sleep(1)

    stop_polling()
    notifier.send_raw("🔴 Torrent Monitor stopped")
    log.info("Stopped")


if __name__ == "__main__":
    main()