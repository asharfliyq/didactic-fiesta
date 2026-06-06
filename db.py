import sqlite3
import time
from pathlib import Path
from typing import Optional

from models import (
    TorrentEntry,
    ParsedRelease,
    SeederSnapshot,
    ShowTracker,
    EpisodeRecord,
    GroupPattern,
)
from config import DB_MAIN, DB_HISTORY, DB_PATTERNS


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_all():
    _init_main()
    _init_history()
    _init_patterns()


def _init_main():
    conn = _connect(DB_MAIN)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS torrents (
            torrent_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT,
            download_url TEXT,
            page_url TEXT,
            seeders INTEGER DEFAULT 0,
            leechers INTEGER DEFAULT 0,
            pub_timestamp REAL,
            first_seen REAL,
            feed_source TEXT,
            tracker TEXT DEFAULT 'tl',

            content_type TEXT,
            parsed_name TEXT,
            parsed_year INTEGER,
            parsed_season INTEGER,
            parsed_episode INTEGER,
            parsed_episode_end INTEGER,
            parsed_group TEXT,
            parsed_res TEXT,
            parsed_source TEXT,
            parsed_source_family TEXT,
            parsed_service_code TEXT,
            parsed_codec TEXT,
            parsed_audio TEXT,
            parsed_hdr TEXT,
            parsed_date_key TEXT,

            family_key TEXT,
            variant_key TEXT,
            exact_key TEXT,

            is_repack INTEGER DEFAULT 0,
            is_proper INTEGER DEFAULT 0,
            is_internal INTEGER DEFAULT 0,
            game_version TEXT,

            tmdb_id INTEGER,
            imdb_id TEXT,
            poster_url TEXT,

            notified INTEGER DEFAULT 0,
            matched_profile TEXT,

            size_bytes INTEGER DEFAULT 0,
            info_hash TEXT,
            uploader TEXT
        );

        CREATE TABLE IF NOT EXISTS seeder_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            torrent_id INTEGER,
            seeders INTEGER,
            leechers INTEGER,
            checked_at REAL,
            FOREIGN KEY (torrent_id) REFERENCES torrents(torrent_id)
        );

        CREATE TABLE IF NOT EXISTS show_tracker (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_name TEXT UNIQUE NOT NULL,
            tmdb_id INTEGER,
            latest_season INTEGER DEFAULT 0,
            latest_episode INTEGER DEFAULT 0,
            last_updated REAL,
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS show_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_name TEXT NOT NULL,
            season INTEGER,
            episode INTEGER,
            torrent_id INTEGER,
            group_name TEXT,
            resolution TEXT,
            found_at REAL,
            UNIQUE(show_name, season, episode, group_name)
        );

        CREATE INDEX IF NOT EXISTS idx_torrents_group ON torrents(parsed_group);
        CREATE INDEX IF NOT EXISTS idx_torrents_name ON torrents(parsed_name);
        CREATE INDEX IF NOT EXISTS idx_torrents_pub ON torrents(pub_timestamp);
        CREATE INDEX IF NOT EXISTS idx_torrents_tracker ON torrents(tracker);
        CREATE INDEX IF NOT EXISTS idx_torrents_family ON torrents(family_key);
        CREATE INDEX IF NOT EXISTS idx_torrents_variant ON torrents(variant_key);
        CREATE INDEX IF NOT EXISTS idx_torrents_exact ON torrents(exact_key);
        CREATE INDEX IF NOT EXISTS idx_snapshots_tid ON seeder_snapshots(torrent_id);
        CREATE INDEX IF NOT EXISTS idx_episodes_show ON show_episodes(show_name);
    """)
    conn.commit()
    conn.close()


def _init_history():
    conn = _connect(DB_HISTORY)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            torrent_id INTEGER,
            profile_name TEXT,
            chat_id TEXT,
            sent_at REAL,
            message_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            command TEXT,
            args TEXT,
            received_at REAL,
            processed INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_notif_tid ON notifications(torrent_id);
    """)
    conn.commit()
    conn.close()


def _init_patterns():
    conn = _connect(DB_PATTERNS)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS group_patterns (
            group_name TEXT PRIMARY KEY,
            peak_hour_start INTEGER,
            peak_hour_end INTEGER,
            peak_days TEXT,
            avg_per_day REAL,
            total_seen INTEGER,
            last_upload REAL,
            calculated_at REAL
        );

        CREATE TABLE IF NOT EXISTS upload_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT,
            category TEXT,
            content_type TEXT,
            pub_timestamp REAL,
            hour INTEGER,
            weekday INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_ulog_group ON upload_log(group_name);
        CREATE INDEX IF NOT EXISTS idx_ulog_ts ON upload_log(pub_timestamp);
    """)
    conn.commit()
    conn.close()


def torrent_exists(torrent_id: int) -> bool:
    conn = _connect(DB_MAIN)
    row = conn.execute("SELECT 1 FROM torrents WHERE torrent_id=?", (torrent_id,)).fetchone()
    conn.close()
    return row is not None


def insert_torrent(entry: TorrentEntry, parsed: ParsedRelease) -> bool:
    if torrent_exists(entry.torrent_id):
        return False

    conn = _connect(DB_MAIN)
    conn.execute("""
        INSERT INTO torrents (
            torrent_id, title, category, download_url, page_url,
            seeders, leechers, pub_timestamp, first_seen, feed_source,
            tracker, content_type, parsed_name, parsed_year, parsed_season,
            parsed_episode, parsed_episode_end, parsed_group, parsed_res,
            parsed_source, parsed_source_family, parsed_service_code,
            parsed_codec, parsed_audio, parsed_hdr, parsed_date_key,
            family_key, variant_key, exact_key,
            is_repack, is_proper, is_internal, game_version,
            size_bytes, info_hash, uploader, imdb_id, tmdb_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        entry.torrent_id, entry.title, entry.category,
        entry.download_url, entry.page_url,
        entry.seeders, entry.leechers,
        entry.pub_timestamp, time.time(), entry.feed_source,
        entry.tracker, parsed.content_type.value, parsed.clean_name, parsed.year,
        parsed.season, parsed.episode, parsed.episode_end,
        parsed.group, parsed.resolution,
        parsed.source, parsed.source_family, parsed.service_code,
        parsed.codec, parsed.audio, parsed.hdr, parsed.date_key,
        parsed.family_key, parsed.variant_key, parsed.exact_key,
        int(parsed.is_repack), int(parsed.is_proper),
        int(parsed.is_internal), parsed.game_version,
        entry.size_bytes, entry.info_hash, entry.uploader,
        entry.imdb_id, entry.tmdb_id
    ))
    conn.commit()
    conn.close()
    return True


def update_seeders(torrent_id: int, seeders: int, leechers: int):
    conn = _connect(DB_MAIN)
    conn.execute(
        "UPDATE torrents SET seeders=?, leechers=? WHERE torrent_id=?",
        (seeders, leechers, torrent_id)
    )
    conn.commit()
    conn.close()


def add_seeder_snapshot(torrent_id: int, seeders: int, leechers: int):
    conn = _connect(DB_MAIN)
    conn.execute(
        "INSERT INTO seeder_snapshots (torrent_id,seeders,leechers,checked_at) VALUES (?,?,?,?)",
        (torrent_id, seeders, leechers, time.time())
    )
    conn.execute("""
        DELETE FROM seeder_snapshots WHERE id IN (
            SELECT id FROM seeder_snapshots
            WHERE torrent_id=?
            ORDER BY checked_at DESC
            LIMIT -1 OFFSET 5
        )
    """, (torrent_id,))
    conn.commit()
    conn.close()


def get_seeder_history(torrent_id: int) -> list[SeederSnapshot]:
    conn = _connect(DB_MAIN)
    rows = conn.execute(
        "SELECT * FROM seeder_snapshots WHERE torrent_id=? ORDER BY checked_at ASC",
        (torrent_id,)
    ).fetchall()
    conn.close()
    return [
        SeederSnapshot(
            torrent_id=r["torrent_id"],
            seeders=r["seeders"],
            leechers=r["leechers"],
            checked_at=r["checked_at"],
        )
        for r in rows
    ]


def find_exact_dupes(name: str, group: str, resolution: str, codec: str, source: str, season, episode, exclude_id: int) -> list[dict]:
    if not name or not group:
        return []

    conn = _connect(DB_MAIN)
    query = """
        SELECT torrent_id, tracker, parsed_group, parsed_res, parsed_codec,
               parsed_source, parsed_audio, parsed_hdr, seeders, leechers,
               page_url, download_url, first_seen
        FROM torrents
        WHERE parsed_name = ? COLLATE NOCASE
          AND parsed_group = ? COLLATE NOCASE
          AND COALESCE(parsed_res, '') = COALESCE(?, '')
          AND COALESCE(parsed_source, '') = COALESCE(?, '') COLLATE NOCASE
          AND COALESCE(parsed_codec, '') = COALESCE(?, '') COLLATE NOCASE
          AND torrent_id != ?
    """
    params: list = [name, group, resolution or '', source or '', codec or '', exclude_id]

    if season is not None:
        query += " AND parsed_season=?"
        params.append(season)
    if episode is not None:
        query += " AND parsed_episode=?"
        params.append(episode)

    query += " ORDER BY first_seen ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_other_releases(name: str, resolution: str, season, episode, exclude_id: int) -> list[dict]:
    if not name:
        return []
    if season is None and episode is None:
        return []

    conn = _connect(DB_MAIN)
    query = """
        SELECT torrent_id, tracker, parsed_group, parsed_res, parsed_codec,
               parsed_source, parsed_audio, parsed_hdr, seeders, leechers,
               page_url, download_url, first_seen, notified
        FROM torrents
        WHERE parsed_name = ? COLLATE NOCASE
          AND COALESCE(parsed_res, '') = COALESCE(?, '')
          AND torrent_id != ?
    """
    params: list = [name, resolution or '', exclude_id]

    if season is not None:
        query += " AND parsed_season=?"
        params.append(season)
    if episode is not None:
        query += " AND parsed_episode=?"
        params.append(episode)

    query += " ORDER BY first_seen ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def was_exact_release_notified(name: str, group: str, resolution: str, source: str, codec: str, season, episode) -> list[dict]:
    if not name or not group:
        return []

    conn = _connect(DB_MAIN)
    query = """
        SELECT torrent_id, tracker, parsed_group
        FROM torrents
        WHERE parsed_name = ? COLLATE NOCASE
          AND parsed_group = ? COLLATE NOCASE
          AND COALESCE(parsed_res, '') = COALESCE(?, '')
          AND COALESCE(parsed_source, '') = COALESCE(?, '') COLLATE NOCASE
          AND COALESCE(parsed_codec, '') = COALESCE(?, '') COLLATE NOCASE
          AND notified = 1
    """
    params: list = [name, group, resolution or '', source or '', codec or '']

    if season is not None:
        query += " AND parsed_season=?"
        params.append(season)
    if episode is not None:
        query += " AND parsed_episode=?"
        params.append(episode)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_first_tracker(name: str, resolution: str, season, episode) -> str:
    if not name:
        return ""

    conn = _connect(DB_MAIN)
    query = """
        SELECT tracker FROM torrents
        WHERE parsed_name = ? COLLATE NOCASE
          AND COALESCE(parsed_res, '') = COALESCE(?, '')
    """
    params: list = [name, resolution or '']

    if season is not None:
        query += " AND parsed_season=?"
        params.append(season)
    if episode is not None:
        query += " AND parsed_episode=?"
        params.append(episode)

    query += " ORDER BY first_seen ASC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row["tracker"] if row else ""


def was_group_episode_notified(name: str, group: str, season, episode) -> bool:
    if not name or not group:
        return False
    if season is None and episode is None:
        return False

    conn = _connect(DB_MAIN)
    row = conn.execute("""
        SELECT 1 FROM torrents
        WHERE parsed_name = ? COLLATE NOCASE
          AND parsed_group = ? COLLATE NOCASE
          AND COALESCE(parsed_season, -1) = COALESCE(?, -1)
          AND COALESCE(parsed_episode, -1) = COALESCE(?, -1)
          AND notified = 1
        LIMIT 1
    """, (
        name, group,
        season if season is not None else -1,
        episode if episode is not None else -1,
    )).fetchone()
    conn.close()
    return row is not None


def get_notification_for_release(parsed_name: str, group: str, resolution: str, season, episode) -> list[dict]:
    hconn = _connect(DB_HISTORY)
    mconn = _connect(DB_MAIN)

    query = """
        SELECT torrent_id FROM torrents
        WHERE parsed_name = ? COLLATE NOCASE
          AND COALESCE(parsed_res, '') = COALESCE(?, '')
          AND notified = 1
    """
    params: list = [parsed_name, resolution or '']

    if group:
        query += " AND parsed_group = ? COLLATE NOCASE"
        params.append(group)

    if season is not None:
        query += " AND parsed_season=?"
        params.append(season)
    if episode is not None:
        query += " AND parsed_episode=?"
        params.append(episode)

    rows = mconn.execute(query, params).fetchall()
    mconn.close()

    results = []
    for row in rows:
        notifs = hconn.execute(
            "SELECT * FROM notifications WHERE torrent_id=? ORDER BY sent_at DESC",
            (row["torrent_id"],)
        ).fetchall()
        for n in notifs:
            results.append(dict(n))

    hconn.close()
    return results


def get_notifications_for_torrent_ids(torrent_ids: list[int]) -> list[dict]:
    if not torrent_ids:
        return []

    seen_ids = set()
    unique_ids = []
    for tid in torrent_ids:
        if not tid:
            continue
        tid = int(tid)
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        unique_ids.append(tid)

    if not unique_ids:
        return []

    placeholders = ",".join("?" for _ in unique_ids)

    conn = _connect(DB_HISTORY)
    rows = conn.execute(
        f"""
        SELECT torrent_id, chat_id, message_id, sent_at
        FROM notifications
        WHERE torrent_id IN ({placeholders})
          AND message_id IS NOT NULL
          AND message_id != 0
        ORDER BY sent_at DESC
        """,
        unique_ids,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_torrent_tmdb(torrent_id: int, tmdb_id: int, imdb_id: str, poster: str):
    conn = _connect(DB_MAIN)
    conn.execute(
        "UPDATE torrents SET tmdb_id=?, imdb_id=?, poster_url=? WHERE torrent_id=?",
        (tmdb_id, imdb_id, poster, torrent_id)
    )
    conn.commit()
    conn.close()


def mark_notified(torrent_id: int, profile: str):
    conn = _connect(DB_MAIN)
    conn.execute(
        "UPDATE torrents SET notified=1, matched_profile=? WHERE torrent_id=?",
        (profile, torrent_id)
    )
    conn.commit()
    conn.close()


def log_notification(torrent_id: int, profile: str, chat_id: str, message_id: int = 0):
    conn = _connect(DB_HISTORY)
    conn.execute(
        "INSERT INTO notifications (torrent_id, profile_name, chat_id, sent_at, message_id) VALUES (?,?,?,?,?)",
        (torrent_id, profile, chat_id, time.time(), message_id)
    )
    conn.commit()
    conn.close()


def get_recent_torrents(hours: int = 6) -> list[dict]:
    cutoff = time.time() - (hours * 3600)
    conn = _connect(DB_MAIN)
    rows = conn.execute(
        "SELECT * FROM torrents WHERE first_seen > ? ORDER BY pub_timestamp DESC",
        (cutoff,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    conn = _connect(DB_MAIN)
    total = conn.execute("SELECT COUNT(*) as c FROM torrents").fetchone()["c"]
    today = conn.execute(
        "SELECT COUNT(*) as c FROM torrents WHERE first_seen > ?",
        (time.time() - 86400,)
    ).fetchone()["c"]
    notified = conn.execute("SELECT COUNT(*) as c FROM torrents WHERE notified=1").fetchone()["c"]
    shows = conn.execute("SELECT COUNT(*) as c FROM show_tracker WHERE active=1").fetchone()["c"]
    conn.close()
    return {
        "total": total,
        "today": today,
        "notified": notified,
        "tracked_shows": shows,
    }


def get_show_tracker(show_name: str) -> Optional[ShowTracker]:
    conn = _connect(DB_MAIN)
    row = conn.execute(
        "SELECT * FROM show_tracker WHERE show_name=? COLLATE NOCASE",
        (show_name,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return ShowTracker(
        id=row["id"],
        show_name=row["show_name"],
        tmdb_id=row["tmdb_id"],
        latest_season=row["latest_season"],
        latest_episode=row["latest_episode"],
        active=bool(row["active"]),
    )


def upsert_show_tracker(name: str, tmdb_id: Optional[int] = None):
    conn = _connect(DB_MAIN)
    conn.execute("""
        INSERT INTO show_tracker (show_name, tmdb_id, last_updated, active)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(show_name) DO UPDATE SET
            tmdb_id=COALESCE(excluded.tmdb_id, tmdb_id),
            last_updated=excluded.last_updated,
            active=1
    """, (name, tmdb_id, time.time()))
    conn.commit()
    conn.close()


def add_episode(ep: EpisodeRecord):
    conn = _connect(DB_MAIN)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO show_episodes
            (show_name, season, episode, torrent_id, group_name, resolution, found_at)
            VALUES (?,?,?,?,?,?,?)
        """, (
            ep.show_name, ep.season, ep.episode, ep.torrent_id,
            ep.group, ep.resolution, time.time()
        ))
        conn.execute("""
            UPDATE show_tracker SET
                latest_season = MAX(latest_season, ?),
                latest_episode = CASE
                    WHEN ? > latest_season THEN ?
                    WHEN ? = latest_season AND ? > latest_episode THEN ?
                    ELSE latest_episode
                END,
                last_updated = ?
            WHERE show_name = ? COLLATE NOCASE
        """, (
            ep.season,
            ep.season, ep.episode,
            ep.season, ep.episode, ep.episode,
            time.time(), ep.show_name
        ))
        conn.commit()
    except Exception:
        pass
    conn.close()


def get_show_episodes(show_name: str, season: Optional[int] = None) -> list[EpisodeRecord]:
    conn = _connect(DB_MAIN)
    if season is not None:
        rows = conn.execute(
            "SELECT * FROM show_episodes WHERE show_name=? COLLATE NOCASE AND season=? ORDER BY episode",
            (show_name, season)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM show_episodes WHERE show_name=? COLLATE NOCASE ORDER BY season, episode",
            (show_name,)
        ).fetchall()
    conn.close()
    return [
        EpisodeRecord(
            show_name=r["show_name"],
            season=r["season"],
            episode=r["episode"],
            torrent_id=r["torrent_id"],
            group=r["group_name"],
            resolution=r["resolution"],
            found_at=r["found_at"],
        )
        for r in rows
    ]


def get_all_tracked_shows() -> list[ShowTracker]:
    conn = _connect(DB_MAIN)
    rows = conn.execute("SELECT * FROM show_tracker WHERE active=1 ORDER BY show_name").fetchall()
    conn.close()
    return [
        ShowTracker(
            id=r["id"],
            show_name=r["show_name"],
            tmdb_id=r["tmdb_id"],
            latest_season=r["latest_season"],
            latest_episode=r["latest_episode"],
            active=bool(r["active"]),
        )
        for r in rows
    ]


def season_pack_exists(name: str, season: int, resolution: str) -> bool:
    if not name or season is None:
        return False
    conn = _connect(DB_MAIN)
    row = conn.execute("""
        SELECT 1 FROM torrents
        WHERE parsed_name = ? COLLATE NOCASE
          AND parsed_season = ?
          AND COALESCE(parsed_res, '') = COALESCE(?, '')
          AND content_type = 'season_pack'
        LIMIT 1
    """, (name, season, resolution or '')).fetchone()
    conn.close()
    return row is not None


def get_older_season_packs(name: str, season: int, exclude_id: int = 0) -> list[dict]:
    """Get season packs for the same show with lower season numbers."""
    if not name or season is None:
        return []
    conn = _connect(DB_MAIN)
    rows = conn.execute("""
        SELECT torrent_id, parsed_season, parsed_res, first_seen
        FROM torrents
        WHERE parsed_name = ? COLLATE NOCASE
          AND parsed_season < ?
          AND content_type = 'season_pack'
          AND torrent_id != ?
        ORDER BY parsed_season DESC
    """, (name, season, exclude_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_older_season_packs(name: str, season: int, exclude_id: int = 0) -> int:
    """Delete season packs for the same show with lower season numbers."""
    if not name or season is None:
        return 0
    conn = _connect(DB_MAIN)
    cursor = conn.execute("""
        DELETE FROM torrents
        WHERE parsed_name = ? COLLATE NOCASE
          AND parsed_season < ?
          AND content_type = 'season_pack'
          AND torrent_id != ?
    """, (name, season, exclude_id))
    deleted_count = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted_count

def log_upload(group: str, category: str, content_type: str, pub_ts: float):
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(pub_ts, tz=timezone.utc)
    conn = _connect(DB_PATTERNS)
    conn.execute(
        "INSERT INTO upload_log (group_name, category, content_type, pub_timestamp, hour, weekday) VALUES (?,?,?,?,?,?)",
        (group, category, content_type, pub_ts, dt.hour, dt.weekday())
    )
    conn.commit()
    conn.close()

def latest_episode_for_show_season(name: str, season: int) -> int | None:
    if not name or season is None:
        return None

    conn = _connect(DB_MAIN)
    row = conn.execute("""
        SELECT MAX(parsed_episode) as max_ep
        FROM torrents
        WHERE parsed_name = ? COLLATE NOCASE
          AND parsed_season = ?
          AND parsed_episode IS NOT NULL
    """, (name, season)).fetchone()
    conn.close()

    if not row:
        return None
    return row["max_ep"]


def cleanup_old_data(days: int = 30):
    cutoff = time.time() - (days * 86400)

    conn = _connect(DB_MAIN)
    conn.execute("DELETE FROM seeder_snapshots WHERE checked_at < ?", (cutoff,))
    conn.commit()
    conn.close()

    conn = _connect(DB_PATTERNS)
    conn.execute("DELETE FROM upload_log WHERE pub_timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()