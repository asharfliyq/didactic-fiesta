import re
import time
import logging
import sqlite3
from models import ParsedRelease, TorrentEntry, EpisodeRecord, ContentType
import db
import config

log = logging.getLogger("tracker")


def init_tracked_shows():
    import re
    watchlists = config.get_watchlists()
    for key, items in watchlists.items():
        if "shows" not in key.lower():
            continue
        for show in items:
            show = str(show).strip()
            if not show:
                continue
            clean = re.sub(r'\s+[Ss]\d+$', '', show).strip()
            existing = db.get_show_tracker(clean)
            if not existing:
                db.upsert_show_tracker(clean)
                log.info(f"Auto-tracking show: {clean}")


def process_episode(entry: TorrentEntry, parsed: ParsedRelease) -> dict | None:
    if parsed.content_type not in (ContentType.EPISODE, ContentType.ANIME_EP):
        return None
    if not parsed.clean_name or parsed.season is None or parsed.episode is None:
        return None

    tracked = db.get_all_tracked_shows()
    matched_show = None
    for show in tracked:
        if show.show_name.lower() in parsed.clean_name.lower():
            matched_show = show
            break

    if not matched_show:
        return None

    ep = EpisodeRecord(
        show_name=matched_show.show_name,
        season=parsed.season,
        episode=parsed.episode,
        torrent_id=entry.torrent_id,
        group=parsed.group,
        resolution=parsed.resolution,
    )
    db.add_episode(ep)

    missing = find_missing_episodes(matched_show.show_name, parsed.season)

    return {
        "show": matched_show.show_name,
        "season": parsed.season,
        "episode": parsed.episode,
        "prev_latest_s": matched_show.latest_season,
        "prev_latest_e": matched_show.latest_episode,
        "is_new_episode": (
            parsed.season > matched_show.latest_season or
            (parsed.season == matched_show.latest_season and
             parsed.episode > matched_show.latest_episode)
        ),
        "missing": missing,
    }


def process_season_pack(entry: TorrentEntry, parsed: ParsedRelease) -> dict | None:
    """Process season pack and delete older season packs for the same show."""
    if parsed.content_type != ContentType.SEASON_PACK:
        return None
    if not parsed.clean_name or parsed.season is None:
        return None

    tracked = db.get_all_tracked_shows()
    matched_show = None
    for show in tracked:
        if show.show_name.lower() in parsed.clean_name.lower():
            matched_show = show
            break

    if not matched_show:
        return None

    # Delete older season packs for this show
    older_packs = db.get_older_season_packs(matched_show.show_name, parsed.season, entry.torrent_id)
    older_pack_ids = [p.get("torrent_id") for p in older_packs if p.get("torrent_id")]
    older_notifications = db.get_notifications_for_torrent_ids(older_pack_ids)
    deleted_count = db.delete_older_season_packs(matched_show.show_name, parsed.season, entry.torrent_id)
    
    if deleted_count > 0:
        log.info(f"Deleted {deleted_count} older season pack(s) for {matched_show.show_name}")

    return {
        "show": matched_show.show_name,
        "season": parsed.season,
        "prev_latest_s": matched_show.latest_season,
        "had_previous": len(older_packs) > 0,
        "deleted_count": deleted_count,
        "deleted_notifications": older_notifications,
    }


def backfill_episodes(show_name: str) -> int:
    conn = sqlite3.connect(str(config.DB_MAIN))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT torrent_id, parsed_name, parsed_season, parsed_episode,
               parsed_group, parsed_res
        FROM torrents
        WHERE parsed_name LIKE ? AND parsed_season IS NOT NULL AND parsed_episode IS NOT NULL
        ORDER BY parsed_season, parsed_episode
    """, (f"%{show_name}%",)).fetchall()
    conn.close()

    count = 0
    for r in rows:
        ep = EpisodeRecord(
            show_name=show_name,
            season=r["parsed_season"],
            episode=r["parsed_episode"],
            torrent_id=r["torrent_id"],
            group=r["parsed_group"] or "",
            resolution=r["parsed_res"] or "",
        )
        db.add_episode(ep)
        count += 1

    if count:
        log.info(f"Backfilled {count} episodes for {show_name}")
    return count


def find_missing_episodes(show_name: str, season: int) -> list[int]:
    episodes = db.get_show_episodes(show_name, season)
    if not episodes:
        return []
    ep_numbers = sorted(set(e.episode for e in episodes))
    if not ep_numbers:
        return []
    full_range = range(ep_numbers[0], ep_numbers[-1] + 1)
    return [e for e in full_range if e not in ep_numbers]


def add_show(name: str, tmdb_id: int | None = None) -> str:
    import sqlite3
    conn = sqlite3.connect(str(config.DB_MAIN))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM show_tracker WHERE show_name=? COLLATE NOCASE", (name,)
    ).fetchone()

    if row:
        if row["active"]:
            conn.close()
            return f"Already tracking: {name}"
        else:
            conn.execute(
                "UPDATE show_tracker SET active=1, last_updated=? WHERE show_name=? COLLATE NOCASE",
                (time.time(), name)
            )
            conn.commit()
            conn.close()
            count = backfill_episodes(name)
            if count:
                return f"Re-activated: {name}\n📋 Backfilled {count} episodes"
            return f"Re-activated: {name}"

    conn.close()
    db.upsert_show_tracker(name, tmdb_id)
    count = backfill_episodes(name)
    if count:
        return f"Now tracking: {name}\n📋 Backfilled {count} episodes"
    return f"Now tracking: {name}"


def remove_show(name: str) -> str:
    existing = db.get_show_tracker(name)
    if not existing:
        return f"Not tracking: {name}"
    conn = sqlite3.connect(str(config.DB_MAIN))
    conn.execute("UPDATE show_tracker SET active=0 WHERE show_name=? COLLATE NOCASE", (name,))
    conn.commit()
    conn.close()
    return f"Stopped tracking: {name}"


def get_show_status(name: str) -> str:
    show = db.get_show_tracker(name)
    if not show:
        return f"Not tracking: {name}\nUse /track {name}"
    episodes = db.get_show_episodes(name)
    if not episodes:
        count = backfill_episodes(name)
        if count:
            episodes = db.get_show_episodes(name)
        if not episodes:
            return f"📺 {name}\nNo episodes found yet"

    seasons = {}
    for ep in episodes:
        seasons.setdefault(ep.season, []).append(ep)

    lines = [f"📺 <b>{name}</b>"]
    for s in sorted(seasons.keys()):
        eps = sorted(set(e.episode for e in seasons[s]))
        groups = list(set(e.group for e in seasons[s] if e.group))
        missing = find_missing_episodes(name, s)
        ep_str = ", ".join(f"E{e:02d}" for e in eps)
        line = f"  S{s:02d}: {ep_str}"
        if groups:
            line += f" [{', '.join(groups[:3])}]"
        if missing:
            miss_str = ", ".join(f"E{m:02d}" for m in missing)
            line += f"\n  ❌ Missing: {miss_str}"
        lines.append(line)
    return "\n".join(lines)