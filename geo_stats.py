"""访客来源地理统计：GeoLite2 查询 + SQLite 每日聚合。

设计要点
--------
- IP 来自 CDN 头 ``Ali-Cdn-Real-Ip``（web.py 负责提取），用 GeoLite2-City.mmdb 本地查询，
  不外发任何请求；
- 计数走独立 SQLite 表 + 原子 UPSERT（``INSERT ... ON CONFLICT DO UPDATE``），
  48 个 gunicorn worker 并发写也一致（与 star_prompt 同一思路，不用 Store 进程内缓存）；
- 数据按天保留（``geo_country_daily`` / ``geo_city_daily``），页面只展示最近 7 天；
- 中国境内城市坐标在入库时就用纯算法从 WGS84 转成 GCJ-02（高德坐标系），
  前端无需再调 ``AMap.convertFrom``（其单次限 40 对坐标）；
- 国家同时存 ISO alpha-2 / alpha-3，alpha-3 对应高德 DistrictLayer.World 的 SOC 字段。
"""

from __future__ import annotations

import gzip
import ipaddress
import logging
import math
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import storage

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
MMDB_PATH = ROOT / "data" / "GeoLite2-City.mmdb"
MMDB_GZ_PATH = ROOT / "data" / "GeoLite2-City.mmdb.gz"
CN_AREA_SQL_PATH = ROOT / "data" / "cn_area.sql"

BJ_TZ = timezone(timedelta(hours=8))

CN_PROVINCE_NAMES = {
    "AH": "安徽省", "BJ": "北京市", "CQ": "重庆市", "FJ": "福建省",
    "GD": "广东省", "GS": "甘肃省", "GX": "广西壮族自治区", "GZ": "贵州省",
    "HA": "河南省", "HB": "湖北省", "HE": "河北省", "HI": "海南省",
    "HK": "香港特别行政区", "HL": "黑龙江省", "HN": "湖南省", "JL": "吉林省",
    "JS": "江苏省", "JX": "江西省", "LN": "辽宁省", "MO": "澳门特别行政区",
    "NM": "内蒙古自治区", "NX": "宁夏回族自治区", "QH": "青海省", "SC": "四川省",
    "SD": "山东省", "SH": "上海市", "SN": "陕西省", "SX": "山西省",
    "TJ": "天津市", "TW": "中国台湾省", "XJ": "新疆维吾尔自治区",
    "XZ": "西藏自治区", "YN": "云南省", "ZJ": "浙江省",
}
CN_PROVINCE_CODES = {name: code for code, name in CN_PROVINCE_NAMES.items()}

_city_province_map: Optional[dict[str, tuple[str, str]]] = None
_city_province_lock = threading.Lock()


def _city_aliases(name: str, *, prefecture_level: bool) -> set[str]:
    aliases = {name.strip()}
    if prefecture_level:
        for suffix in ("自治州", "地区", "市", "盟"):
            if name.endswith(suffix) and len(name) > len(suffix):
                aliases.add(name[: -len(suffix)])
                break
    return {alias for alias in aliases if alias}


def _load_city_province_map() -> dict[str, tuple[str, str]]:
    """从 cn_area.sql 构建城市/区县到省份索引；重名跨省时不做猜测。"""
    global _city_province_map
    if _city_province_map is not None:
        return _city_province_map
    with _city_province_lock:
        if _city_province_map is not None:
            return _city_province_map
        try:
            text = CN_AREA_SQL_PATH.read_text(encoding="utf-8")
            rows = {
                int(area_id): (int(parent_id), name)
                for area_id, parent_id, name in re.findall(
                    r"\((\d+),\s*(\d+),\s*'([^']*)'\)", text
                )
            }
            province_ids = {area_id for area_id, (parent_id, _) in rows.items() if parent_id == 0}
            candidates: dict[str, set[tuple[str, str]]] = {}
            for area_id, (parent_id, name) in rows.items():
                if area_id in province_ids:
                    continue
                ancestor_id = parent_id
                while ancestor_id in rows and ancestor_id not in province_ids:
                    ancestor_id = rows[ancestor_id][0]
                if ancestor_id not in province_ids:
                    continue
                raw_province = rows[ancestor_id][1]
                if raw_province == "海外":
                    continue
                if raw_province in ("北京", "天津", "上海", "重庆"):
                    province_name = raw_province + "市"
                elif raw_province in ("台湾", "台湾省"):
                    province_name = "中国台湾省"
                else:
                    province_name = raw_province
                province_code = CN_PROVINCE_CODES.get(province_name, "")
                prefecture_level = parent_id in province_ids
                for alias in _city_aliases(name, prefecture_level=prefecture_level):
                    candidates.setdefault(alias, set()).add((province_code, province_name))
            _city_province_map = {
                city: next(iter(matches))
                for city, matches in candidates.items()
                if len(matches) == 1
            }
            log.info("已从 cn_area.sql 加载 %d 个城市/区县省份映射", len(_city_province_map))
        except Exception as e:
            log.warning("cn_area.sql 城市省份映射加载失败: %s", e)
            _city_province_map = {}
    return _city_province_map


def _province_from_city(city_name: Optional[str]) -> Optional[tuple[str, str]]:
    if not city_name:
        return None
    mapping = _load_city_province_map()
    for alias in _city_aliases(city_name, prefecture_level=True):
        result = mapping.get(alias)
        if result:
            return result
    return None

# ─────────────────────────────────────────────
# GeoLite2 读取（懒加载单例；.mmdb 缺失时自动从 .gz 解压）
# ─────────────────────────────────────────────

_reader = None
_reader_failed = False
_reader_lock = threading.Lock()


def _get_reader():
    global _reader, _reader_failed
    if _reader is not None or _reader_failed:
        return _reader
    with _reader_lock:
        if _reader is not None or _reader_failed:
            return _reader
        try:
            if not MMDB_PATH.exists() and MMDB_GZ_PATH.exists():
                with gzip.open(MMDB_GZ_PATH, "rb") as fi, open(MMDB_PATH, "wb") as fo:
                    shutil.copyfileobj(fi, fo)
                log.info(f"已从 {MMDB_GZ_PATH.name} 解压 GeoLite2 数据库")
            import geoip2.database

            _reader = geoip2.database.Reader(str(MMDB_PATH))
        except Exception as e:
            _reader_failed = True
            log.warning(f"GeoLite2 数据库不可用，访客来源统计停用: {e}")
    return _reader


def _lookup(ip: str) -> Optional[dict]:
    """查询 IP 归属地；私网/保留地址或查询失败返回 None。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_multicast or addr.is_link_local:
        return None
    reader = _get_reader()
    if reader is None:
        return None
    try:
        resp = reader.city(ip)
    except Exception:
        return None
    country = resp.country
    if not country or not country.iso_code:
        return None
    code = country.iso_code.upper()
    country_name = country.names.get("zh-CN") or country.name or code
    if code == "TW" or country_name in ("台湾", "台湾省"):
        country_name = "中国台湾省"
    subdivision = resp.subdivisions.most_specific
    province_code = subdivision.iso_code if subdivision else None
    province_name = None
    if subdivision:
        province_name = subdivision.names.get("zh-CN") or subdivision.name
    if code == "CN" and province_code:
        province_name = CN_PROVINCE_NAMES.get(province_code, province_name)
    if code == "TW":
        province_code = "TW"
        province_name = "中国台湾省"
    city_name = None
    if resp.city:
        city_name = resp.city.names.get("zh-CN") or resp.city.name
    if code == "CN" and not province_name:
        province = _province_from_city(city_name)
        if province:
            province_code, province_name = province
    lat = resp.location.latitude
    lng = resp.location.longitude
    return {
        "country_code": code,
        "country_code3": ISO2_TO_ISO3.get(code, ""),
        "country_name": country_name,
        "province_code": province_code,
        "province_name": province_name,
        "geoname_id": resp.city.geoname_id if resp.city else None,
        "city_name": city_name,
        "lat": lat,
        "lng": lng,
    }


# ─────────────────────────────────────────────
# WGS84 -> GCJ-02（高德坐标系），仅中国境内需要
# ─────────────────────────────────────────────

_GCJ_A = 6378245.0
_GCJ_EE = 0.00669342162296594323


def _out_of_china(lat: float, lng: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    if _out_of_china(lat, lng):
        return lat, lng
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = 1 - _GCJ_EE * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_GCJ_A * (1 - _GCJ_EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_GCJ_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lng + dlng


# ─────────────────────────────────────────────
# SQLite 存储（跨 worker 原子 UPSERT）
# ─────────────────────────────────────────────

_db_inited = False
_db_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS geo_country_daily (
  date TEXT NOT NULL,
  code TEXT NOT NULL,
  code3 TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  visitors INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS geo_city_daily (
  date TEXT NOT NULL,
  geoname_id INTEGER NOT NULL,
  country TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  lat REAL NOT NULL DEFAULT 0,
  lng REAL NOT NULL DEFAULT 0,
  visitors INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (date, geoname_id)
);
CREATE TABLE IF NOT EXISTS geo_region_daily (
  date TEXT NOT NULL,
  region_key TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'country',
  code TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  visitors INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (date, region_key)
);
CREATE TABLE IF NOT EXISTS geo_stats_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
"""


def _db(db_path: Path):
    global _db_inited
    conn = storage.shared_sqlite_conn(db_path)
    if not _db_inited:
        with _db_lock:
            if not _db_inited:
                with conn:
                    for stmt in _SCHEMA.strip().split(";"):
                        stmt = stmt.strip()
                        if stmt:
                            conn.execute(stmt)
                    seed_done = conn.execute(
                        "SELECT 1 FROM geo_stats_meta WHERE key = 'country_region_seed_v1'"
                    ).fetchone()
                    if not seed_done:
                        # 兼容已有统计：境外访问仍按国家，中国历史访问先归入
                        # “省份未知”，随后由城市反查迁移尽可能细分。
                        conn.execute(
                            "INSERT OR IGNORE INTO geo_region_daily "
                            "(date, region_key, kind, code, name, visitors) "
                            "SELECT date, "
                            "CASE WHEN code = 'CN' THEN 'CN-UNKNOWN' "
                            "WHEN code = 'TW' THEN 'CN-TW' ELSE 'COUNTRY-' || code END, "
                            "CASE WHEN code IN ('CN', 'TW') THEN 'province' ELSE 'country' END, "
                            "CASE WHEN code = 'CN' THEN '' WHEN code = 'TW' THEN 'TW' ELSE code END, "
                            "CASE WHEN code = 'CN' THEN '中国（省份未知）' "
                            "WHEN code = 'TW' THEN '中国台湾省' ELSE name END, visitors "
                            "FROM geo_country_daily"
                        )
                        conn.execute(
                            "INSERT INTO geo_stats_meta (key, value) "
                            "VALUES ('country_region_seed_v1', 'done')"
                        )
                    backfill_done = conn.execute(
                        "SELECT 1 FROM geo_stats_meta WHERE key = 'city_province_backfill_v1'"
                    ).fetchone()
                    if not backfill_done:
                        # 利用已有城市记录尽可能回填历史省份。这个带版本标记的迁移
                        # 也适用于省级表已经由较早版本创建、但尚未支持城市反查的部署。
                        province_counts: dict[tuple[str, str, str], int] = {}
                        for date, city_name, visitors in conn.execute(
                            "SELECT date, name, SUM(visitors) FROM geo_city_daily "
                            "WHERE country = '中国' GROUP BY date, geoname_id"
                        ).fetchall():
                            province = _province_from_city(city_name)
                            if not province:
                                continue
                            province_code, province_name = province
                            key = (date, province_code, province_name)
                            province_counts[key] = province_counts.get(key, 0) + int(visitors)
                        unknown_by_date = dict(
                            conn.execute(
                                "SELECT date, visitors FROM geo_region_daily "
                                "WHERE region_key = 'CN-UNKNOWN'"
                            ).fetchall()
                        )
                        for (date, province_code, province_name), visitors in province_counts.items():
                            existing_row = conn.execute(
                                "SELECT visitors FROM geo_region_daily "
                                "WHERE date = ? AND region_key = ?",
                                (date, f"CN-{province_code}"),
                            ).fetchone()
                            existing = int(existing_row[0]) if existing_row else 0
                            # 城市统计中可能已包含新版直接写入的省份访问，因此只补齐
                            # “城市可证明的数量”与已有省份数量之间的差额。
                            wanted_delta = max(0, int(visitors) - existing)
                            available_unknown = max(0, int(unknown_by_date.get(date, 0)))
                            attributed = min(wanted_delta, available_unknown)
                            if attributed <= 0:
                                continue
                            conn.execute(
                                "INSERT INTO geo_region_daily "
                                "(date, region_key, kind, code, name, visitors) "
                                "VALUES (?, ?, 'province', ?, ?, ?) "
                                "ON CONFLICT(date, region_key) DO UPDATE SET "
                                "visitors = visitors + excluded.visitors, name = excluded.name",
                                (date, f"CN-{province_code}", province_code, province_name, attributed),
                            )
                            unknown_by_date[date] = available_unknown - attributed
                        for date, unknown_visitors in unknown_by_date.items():
                            unknown = max(0, int(unknown_visitors))
                            if unknown:
                                conn.execute(
                                    "UPDATE geo_region_daily SET visitors = ? "
                                    "WHERE date = ? AND region_key = 'CN-UNKNOWN'",
                                    (unknown, date),
                                )
                            else:
                                conn.execute(
                                    "DELETE FROM geo_region_daily "
                                    "WHERE date = ? AND region_key = 'CN-UNKNOWN'",
                                    (date,),
                                )
                        conn.execute(
                            "INSERT INTO geo_stats_meta (key, value) "
                            "VALUES ('city_province_backfill_v1', 'done')"
                        )
                _db_inited = True
    return conn


def status() -> dict:
    """诊断信息：GeoLite2 读取器与数据库文件是否可用（不含敏感信息）。"""
    return {
        "reader_ok": _get_reader() is not None,
        "mmdb_exists": MMDB_PATH.exists(),
        "mmdb_gz_exists": MMDB_GZ_PATH.exists(),
    }


def record_visit(db_path: Path, ip: str, date_str: Optional[str] = None) -> str:
    """记录一次访问的来源地。返回 "ok" 或跳过/失败原因（用于日志诊断）。"""
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return "invalid-ip"
    if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_multicast or addr.is_link_local:
        return "private-ip"
    if _get_reader() is None:
        return "reader-unavailable"
    info = _lookup(ip)
    if info is None:
        return "not-found"
    date_str = date_str or datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    conn = _db(db_path)
    try:
        conn.execute(
            "INSERT INTO geo_country_daily (date, code, code3, name, visitors) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(date, code) DO UPDATE SET visitors = visitors + 1",
            (date_str, info["country_code"], info["country_code3"], info["country_name"]),
        )
        if info["country_code"] in ("CN", "TW"):
            province_code = info["province_code"] or "UNKNOWN"
            region_key = f"CN-{province_code}"
            region_code = "" if province_code == "UNKNOWN" else province_code
            region_name = info["province_name"] or "中国（省份未知）"
            region_kind = "province"
        else:
            region_key = f"COUNTRY-{info['country_code']}"
            region_code = info["country_code"]
            region_name = info["country_name"]
            region_kind = "country"
        conn.execute(
            "INSERT INTO geo_region_daily (date, region_key, kind, code, name, visitors) "
            "VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(date, region_key) DO UPDATE SET visitors = visitors + 1, name = excluded.name",
            (date_str, region_key, region_kind, region_code, region_name),
        )
        if info["geoname_id"] and info["city_name"] and info["lat"] is not None and info["lng"] is not None:
            lat, lng = info["lat"], info["lng"]
            if info["country_code"] == "CN":
                lat, lng = wgs84_to_gcj02(lat, lng)
            conn.execute(
                "INSERT INTO geo_city_daily (date, geoname_id, country, name, lat, lng, visitors) "
                "VALUES (?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(date, geoname_id) DO UPDATE SET visitors = visitors + 1",
                (date_str, info["geoname_id"], info["country_name"], info["city_name"], lat, lng),
            )
        return "ok"
    except Exception as e:
        log.warning(f"访客来源写入失败: {e}")
        return "db-error"


def record_unknown_visit(db_path: Path, date_str: Optional[str] = None) -> str:
    """记录未授权使用 IP 归属地信息的访客，不读取或保存其 IP。"""
    date_str = date_str or datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    conn = _db(db_path)
    try:
        conn.execute(
            "INSERT INTO geo_country_daily (date, code, code3, name, visitors) "
            "VALUES (?, 'UNKNOWN', '', '未知', 1) "
            "ON CONFLICT(date, code) DO UPDATE SET visitors = visitors + 1",
            (date_str,),
        )
        conn.execute(
            "INSERT INTO geo_region_daily (date, region_key, kind, code, name, visitors) "
            "VALUES (?, 'UNKNOWN', 'unknown', '', '未知', 1) "
            "ON CONFLICT(date, region_key) DO UPDATE SET visitors = visitors + 1, name = excluded.name",
            (date_str,),
        )
        return "ok"
    except Exception as e:
        log.warning(f"未知访客来源写入失败: {e}")
        return "db-error"


def get_stats(db_path: Path, days: int = 7) -> dict:
    """聚合最近 ``days`` 天（含今天，北京时间）的访客来源。"""
    today = datetime.now(BJ_TZ).date()
    since = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    until = today.strftime("%Y-%m-%d")
    conn = _db(db_path)

    country_totals: dict[str, dict] = {}
    for code, code3, name, visitors in conn.execute(
        "SELECT code, code3, name, SUM(visitors) FROM geo_country_daily "
        "WHERE date >= ? GROUP BY code ORDER BY SUM(visitors) DESC",
        (since,),
    ).fetchall():
        # 国家层级中台湾地区并入中国；省级层级单独显示“中国台湾省”。
        if code == "TW":
            code, code3, name = "CN", "CHN", "中国"
        item = country_totals.setdefault(
            code, {"code": code, "code3": code3, "name": name, "visitors": 0}
        )
        item["visitors"] += int(visitors)
    countries = sorted(country_totals.values(), key=lambda item: item["visitors"], reverse=True)

    total = sum(c["visitors"] for c in countries)
    for c in countries:
        c["share"] = round(c["visitors"] / total, 4) if total else 0.0

    regions = []
    for kind, code, name, visitors in conn.execute(
        "SELECT kind, code, name, SUM(visitors) FROM geo_region_daily "
        "WHERE date >= ? GROUP BY region_key ORDER BY SUM(visitors) DESC",
        (since,),
    ).fetchall():
        regions.append(
            {"kind": kind, "code": code, "name": name, "visitors": int(visitors)}
        )

    cities = []
    for country, name, lat, lng, visitors in conn.execute(
        "SELECT country, name, lat, lng, SUM(visitors) FROM geo_city_daily "
        "WHERE date >= ? GROUP BY geoname_id ORDER BY SUM(visitors) DESC LIMIT 300",
        (since,),
    ).fetchall():
        if country in ("台湾", "台湾省"):
            country = "中国台湾省"
        cities.append(
            {"country": country, "name": name, "lat": lat, "lng": lng, "visitors": int(visitors)}
        )

    daily_rows = conn.execute(
        "SELECT date, SUM(visitors) FROM geo_country_daily WHERE date >= ? GROUP BY date",
        (since,),
    ).fetchall()
    daily_map = {d: int(v) for d, v in daily_rows}
    daily = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        daily.append({"date": d, "visitors": daily_map.get(d, 0)})

    return {
        "days": days,
        "since": since,
        "until": until,
        "total": total,
        "daily": daily,
        "countries": countries,
        "regions": regions,
        "cities": cities,
    }


# ─────────────────────────────────────────────
# ISO 3166-1 alpha-2 -> alpha-3（高德 World 图层 SOC 用 alpha-3）
# ─────────────────────────────────────────────

ISO2_TO_ISO3 = {
    "AD": "AND", "AE": "ARE", "AF": "AFG", "AG": "ATG", "AI": "AIA", "AL": "ALB",
    "AM": "ARM", "AO": "AGO", "AQ": "ATA", "AR": "ARG", "AS": "ASM", "AT": "AUT",
    "AU": "AUS", "AW": "ABW", "AX": "ALA", "AZ": "AZE", "BA": "BIH", "BB": "BRB",
    "BD": "BGD", "BE": "BEL", "BF": "BFA", "BG": "BGR", "BH": "BHR", "BI": "BDI",
    "BJ": "BEN", "BL": "BLM", "BM": "BMU", "BN": "BRN", "BO": "BOL", "BQ": "BES",
    "BR": "BRA", "BS": "BHS", "BT": "BTN", "BV": "BVT", "BW": "BWA", "BY": "BLR",
    "BZ": "BLZ", "CA": "CAN", "CC": "CCK", "CD": "COD", "CF": "CAF", "CG": "COG",
    "CH": "CHE", "CI": "CIV", "CK": "COK", "CL": "CHL", "CM": "CMR", "CN": "CHN",
    "CO": "COL", "CR": "CRI", "CU": "CUB", "CV": "CPV", "CW": "CUW", "CX": "CXR",
    "CY": "CYP", "CZ": "CZE", "DE": "DEU", "DJ": "DJI", "DK": "DNK", "DM": "DMA",
    "DO": "DOM", "DZ": "DZA", "EC": "ECU", "EE": "EST", "EG": "EGY", "EH": "ESH",
    "ER": "ERI", "ES": "ESP", "ET": "ETH", "FI": "FIN", "FJ": "FJI", "FK": "FLK",
    "FM": "FSM", "FO": "FRO", "FR": "FRA", "GA": "GAB", "GB": "GBR", "GD": "GRD",
    "GE": "GEO", "GF": "GUF", "GG": "GGY", "GH": "GHA", "GI": "GIB", "GL": "GRL",
    "GM": "GMB", "GN": "GIN", "GP": "GLP", "GQ": "GNQ", "GR": "GRC", "GS": "SGS",
    "GT": "GTM", "GU": "GUM", "GW": "GNB", "GY": "GUY", "HK": "HKG", "HM": "HMD",
    "HN": "HND", "HR": "HRV", "HT": "HTI", "HU": "HUN", "ID": "IDN", "IE": "IRL",
    "IL": "ISR", "IM": "IMN", "IN": "IND", "IO": "IOT", "IQ": "IRQ", "IR": "IRN",
    "IS": "ISL", "IT": "ITA", "JE": "JEY", "JM": "JAM", "JO": "JOR", "JP": "JPN",
    "KE": "KEN", "KG": "KGZ", "KH": "KHM", "KI": "KIR", "KM": "COM", "KN": "KNA",
    "KP": "PRK", "KR": "KOR", "KW": "KWT", "KY": "CYM", "KZ": "KAZ", "LA": "LAO",
    "LB": "LBN", "LC": "LCA", "LI": "LIE", "LK": "LKA", "LR": "LBR", "LS": "LSO",
    "LT": "LTU", "LU": "LUX", "LV": "LVA", "LY": "LBY", "MA": "MAR", "MC": "MCO",
    "MD": "MDA", "ME": "MNE", "MF": "MAF", "MG": "MDG", "MH": "MHL", "MK": "MKD",
    "ML": "MLI", "MM": "MMR", "MN": "MNG", "MO": "MAC", "MP": "MNP", "MQ": "MTQ",
    "MR": "MRT", "MS": "MSR", "MT": "MLT", "MU": "MUS", "MV": "MDV", "MW": "MWI",
    "MX": "MEX", "MY": "MYS", "MZ": "MOZ", "NA": "NAM", "NC": "NCL", "NE": "NER",
    "NF": "NFK", "NG": "NGA", "NI": "NIC", "NL": "NLD", "NO": "NOR", "NP": "NPL",
    "NR": "NRU", "NU": "NIU", "NZ": "NZL", "OM": "OMN", "PA": "PAN", "PE": "PER",
    "PF": "PYF", "PG": "PNG", "PH": "PHL", "PK": "PAK", "PL": "POL", "PM": "SPM",
    "PN": "PCN", "PR": "PRI", "PS": "PSE", "PT": "PRT", "PW": "PLW", "PY": "PRY",
    "QA": "QAT", "RE": "REU", "RO": "ROU", "RS": "SRB", "RU": "RUS", "RW": "RWA",
    "SA": "SAU", "SB": "SLB", "SC": "SYC", "SD": "SDN", "SE": "SWE", "SG": "SGP",
    "SH": "SHN", "SI": "SVN", "SJ": "SJM", "SK": "SVK", "SL": "SLE", "SM": "SMR",
    "SN": "SEN", "SO": "SOM", "SR": "SUR", "SS": "SSD", "ST": "STP", "SV": "SLV",
    "SX": "SXM", "SY": "SYR", "SZ": "SWZ", "TC": "TCA", "TD": "TCD", "TF": "ATF",
    "TG": "TGO", "TH": "THA", "TJ": "TJK", "TK": "TKL", "TL": "TLS", "TM": "TKM",
    "TN": "TUN", "TO": "TON", "TR": "TUR", "TT": "TTO", "TV": "TUV", "TW": "TWN",
    "TZ": "TZA", "UA": "UKR", "UG": "UGA", "UM": "UMI", "US": "USA", "UY": "URY",
    "UZ": "UZB", "VA": "VAT", "VC": "VCT", "VE": "VEN", "VG": "VGB", "VI": "VIR",
    "VN": "VNM", "VU": "VUT", "WF": "WLF", "WS": "WSM", "YE": "YEM", "YT": "MYT",
    "ZA": "ZAF", "ZM": "ZMB", "ZW": "ZWE",
}
