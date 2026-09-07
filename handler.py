
import asyncio
import logging
import os
import re
from datetime import datetime

import bot
import json
import base64
import socketio
import requests
from telegram.ext import ContextTypes

config = bot.config
client = bot.client
openai = bot.openai
changeButton = bot.changeButton
groupId = config["bot"]["groupId"]
websiteId = config["crisp"]["website"]
payload = config["openai"]["payload"]
USER_LOOKUP_WARNED = set()
USER_LOOKUP_EMPTY_WARNED = False
DB_TIMEOUTS = {
    "connect_timeout": 3,
    "read_timeout": 5,
    "write_timeout": 5,
}

META_ALIASES = {
    "plan": ["Plan", "plan", "plan_name", "套餐", "使用套餐"],
    "expired_at": ["ExpiredAt", "expired_at", "ExpireAt", "expire_at", "ExpireTime", "ExpiredTime", "expired", "expire", "到期时间"],
    "remaining_traffic": ["RemainingTraffic", "remaining_traffic", "RemainTraffic", "remain_traffic", "transfer_remaining", "剩余流量"],
    "used_traffic": ["UsedTraffic", "used_traffic", "usedTraffic", "traffic_used", "已用流量"],
    "total_traffic": ["AllTraffic", "TotalTraffic", "transfer_enable", "transferEnable", "total_traffic", "总流量"],
    "upload": ["u", "upload", "UploadTraffic", "upload_traffic"],
    "download": ["d", "download", "DownloadTraffic", "download_traffic"],
    "last_used_at": ["LastUsedAt", "last_used_at", "last_use_at", "lastUseAt", "t", "上次使用时间", "最后使用时间"],
    "last_login_at": ["LastLoginAt", "last_login_at", "lastLoginAt", "上次登录时间"],
}

V2BOARD_ACTIVITY_TABLES = ("v2_stat_user", "stat_user", "v2_user_traffic_log", "user_traffic_log")
METRON_ACTIVITY_TABLES = ("user_traffic_log", "user_subscribe_log", "alive_ip")
ACTIVITY_TIME_COLUMNS = ("log_time", "request_time", "datetime", "record_at", "created_at", "updated_at", "date")
CRISP_API_BASE_URL = "https://api.crisp.chat/v1"
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)

def getKey(content: str):
    if len(config["autoreply"]) > 0:
        for x in config["autoreply"]:
            keyword = x.split("|")
            for key in keyword:
                if key in content:
                    return True, config["autoreply"][x]
    return False, None

def get_meta_value(data, aliases):
    if not isinstance(data, dict):
        return None
    lower_map = {str(key).lower(): value for key, value in data.items()}
    for key in aliases:
        if key in data:
            return data[key]
        value = lower_map.get(str(key).lower())
        if value is not None:
            return value
    return None

def get_config_value(source, key, default=None):
    value = source.get(key)
    if value not in (None, ""):
        return value

    env_name = source.get(f"{key}Env")
    if env_name:
        return os.getenv(str(env_name), default)

    return default

def get_first_config_value(source, keys, default=None):
    for key in keys:
        value = get_config_value(source, key)
        if value not in (None, ""):
            return value
    return default

def mask_email(email):
    if not email or "@" not in email:
        return email or ""
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        masked_name = name[:1] + "*"
    else:
        masked_name = name[:2] + "*" * min(len(name) - 2, 6)
    return f"{masked_name}@{domain}"

def normalize_email(value):
    if value in (None, ""):
        return ""
    match = EMAIL_PATTERN.search(str(value))
    return match.group(0).strip().lower() if match else str(value).strip().lower()

def extract_email_from_value(value):
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        match = EMAIL_PATTERN.search(value)
        return normalize_email(match.group(0)) if match else ""
    if isinstance(value, dict):
        for key in ("email", "mail", "content", "text", "nickname"):
            email = extract_email_from_value(value.get(key))
            if email:
                return email
        for item in value.values():
            email = extract_email_from_value(item)
            if email:
                return email
    if isinstance(value, (list, tuple)):
        for item in value:
            email = extract_email_from_value(item)
            if email:
                return email
    return ""

def extract_email_from_message(data):
    if not isinstance(data, dict):
        return ""
    for key in ("content", "user", "visitor", "profile"):
        email = extract_email_from_value(data.get(key))
        if email:
            return email
    return ""

def get_email_lookup_values(email):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return "", ""
    return normalized_email, f"%{normalized_email}%"

def quote_identifier(identifier):
    identifier = str(identifier or "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", identifier):
        raise ValueError(f"不安全的数据库标识符: {identifier}")
    return f"`{identifier}`"

def build_activity_tables(site_config, default_tables, table_prefix=""):
    configured_tables = site_config.get("activityTables")
    if configured_tables:
        return configured_tables

    tables = []
    for table in default_tables:
        candidates = [table]
        if table_prefix and not table.startswith(table_prefix):
            candidates.insert(0, f"{table_prefix}{table}")
        for candidate in candidates:
            if candidate not in tables:
                tables.append(candidate)
    return tables

def get_lookup_sites():
    global USER_LOOKUP_EMPTY_WARNED
    lookup_config = config.get("userLookup", {})
    if lookup_config.get("enabled", True) is False:
        return []

    sites = lookup_config.get("sites") or []
    if sites:
        return sites

    legacy_config = config.get("v2board", {})
    legacy_db = legacy_config.get("database", {})
    if legacy_db:
        return [{
            "name": legacy_config.get("name", "V2Board"),
            "type": "v2board",
            "tablePrefix": legacy_config.get("tablePrefix", "v2_"),
            **legacy_db,
        }]
    if not USER_LOOKUP_EMPTY_WARNED:
        logging.warning("未配置 userLookup.sites，无法按邮箱查询网站套餐/到期/流量信息")
        USER_LOOKUP_EMPTY_WARNED = True
    return []

def get_site_db_config(site_config):
    database = get_first_config_value(site_config, ("database", "db", "dbname"))
    user = get_first_config_value(site_config, ("user", "username"))
    password = get_first_config_value(site_config, ("password", "pass"), "")
    if not database or not user:
        logging.warning(
            "用户查询站点配置不完整 site=%s：缺少 database/db 或 user/username",
            site_config.get("name", site_config.get("host", "unknown")),
        )
        return None

    return {
        "host": get_first_config_value(site_config, ("host", "hostname"), "127.0.0.1"),
        "port": int(get_first_config_value(site_config, ("port",), 3306)),
        "user": user,
        "password": password,
        "database": database,
        "charset": get_first_config_value(site_config, ("charset",), "utf8mb4"),
    }

def table_exists(cursor, table):
    cursor.execute("SHOW TABLES LIKE %s", (table,))
    return cursor.fetchone() is not None

def get_table_columns(cursor, table):
    cursor.execute(f"SHOW COLUMNS FROM {quote_identifier(table)}")
    return {row["Field"]: row for row in cursor.fetchall()}

def first_existing_column(columns, candidates):
    return next((column for column in candidates if column in columns), None)

def query_last_activity(cursor, user_id, activity_tables):
    for activity_table in activity_tables:
        if not table_exists(cursor, activity_table):
            continue

        columns = get_table_columns(cursor, activity_table)
        user_column = first_existing_column(columns, ("user_id", "userid"))
        time_column = first_existing_column(columns, ACTIVITY_TIME_COLUMNS)
        if not user_column or not time_column:
            continue

        cursor.execute(
            f"""
            SELECT MAX({quote_identifier(time_column)}) AS last_used_at
            FROM {quote_identifier(activity_table)}
            WHERE {quote_identifier(user_column)} = %s
            """,
            (user_id,),
        )
        row = cursor.fetchone() or {}
        if row.get("last_used_at") not in (None, ""):
            return row.get("last_used_at")
    return None

def query_v2board_user(cursor, site_config, email):
    normalized_email, fuzzy_email = get_email_lookup_values(email)
    if not normalized_email:
        return {}

    table_prefix = site_config.get("tablePrefix", "v2_")
    user_table_raw = f"{table_prefix or ''}{site_config.get('userTable', 'user')}"
    plan_table_raw = f"{table_prefix or ''}{site_config.get('planTable', 'plan')}"
    user_table = quote_identifier(user_table_raw)
    columns = get_table_columns(cursor, user_table_raw)

    optional_columns = {
        "id": first_existing_column(columns, ("id",)),
        "email": first_existing_column(columns, ("email",)),
        "expired_at": first_existing_column(columns, ("expired_at", "expire_at")),
        "transfer_enable": first_existing_column(columns, ("transfer_enable",)),
        "upload": first_existing_column(columns, ("u", "upload")),
        "download": first_existing_column(columns, ("d", "download")),
        "last_used_at": first_existing_column(columns, ("t", "last_used_at", "last_login_at")),
        "plan_id": first_existing_column(columns, ("plan_id",)),
    }
    if not optional_columns["email"]:
        return {}

    select_parts = [
        f"u.{quote_identifier(column)} AS {quote_identifier(alias)}"
        for alias, column in optional_columns.items()
        if column and alias != "plan_id"
    ]
    join_sql = ""
    if optional_columns["plan_id"] and table_exists(cursor, plan_table_raw):
        plan_columns = get_table_columns(cursor, plan_table_raw)
        plan_id_column = first_existing_column(plan_columns, ("id",))
        plan_name_column = first_existing_column(plan_columns, ("name", "title"))
        if plan_id_column and plan_name_column:
            select_parts.append(f"p.{quote_identifier(plan_name_column)} AS `plan`")
            join_sql = f"LEFT JOIN {quote_identifier(plan_table_raw)} AS p ON p.{quote_identifier(plan_id_column)} = u.{quote_identifier(optional_columns['plan_id'])}"

    query_sql = f"""
        SELECT {", ".join(select_parts)}
        FROM {user_table} AS u
        {join_sql}
        WHERE LOWER(TRIM(u.{quote_identifier(optional_columns["email"])})) = %s
        LIMIT 1
        """
    cursor.execute(query_sql, (normalized_email,))
    row = cursor.fetchone() or {}
    if not row and fuzzy_email:
        cursor.execute(
            query_sql.replace(
                f"LOWER(TRIM(u.{quote_identifier(optional_columns['email'])})) = %s",
                f"u.{quote_identifier(optional_columns['email'])} LIKE %s",
            ),
            (fuzzy_email,),
        )
        row = cursor.fetchone() or {}
    if not row:
        logging.info(
            "用户查询未匹配 site=%s type=v2board email=%s",
            site_config.get("name", site_config.get("host", "unknown")),
            mask_email(email),
        )
        return {}
    logging.info(
        "用户查询命中 site=%s type=v2board email=%s",
        site_config.get("name", site_config.get("host", "unknown")),
        mask_email(row.get("email") or normalized_email),
    )

    activity_tables = build_activity_tables(site_config, V2BOARD_ACTIVITY_TABLES, table_prefix)
    last_used_at = query_last_activity(cursor, row.get("id"), activity_tables) if row.get("id") else None

    return {
        "site": site_config.get("name"),
        "plan": row.get("plan"),
        "expired_at": row.get("expired_at"),
        "total_traffic": row.get("transfer_enable"),
        "used_traffic": (row.get("upload") or 0) + (row.get("download") or 0),
        "last_used_at": last_used_at or row.get("last_used_at"),
    }

def query_metron_user(cursor, site_config, email):
    normalized_email, fuzzy_email = get_email_lookup_values(email)
    if not normalized_email:
        return {}

    user_table_raw = site_config.get("userTable", "user")
    user_table = quote_identifier(user_table_raw)
    columns = get_table_columns(cursor, user_table_raw)

    optional_columns = {
        "id": first_existing_column(columns, ("id",)),
        "email": first_existing_column(columns, ("email",)),
        "plan": first_existing_column(columns, ("plan", "class")),
        "expired_at": first_existing_column(columns, ("class_expire", "expire_in", "expired_at")),
        "transfer_enable": first_existing_column(columns, ("transfer_enable",)),
        "upload": first_existing_column(columns, ("u", "upload")),
        "download": first_existing_column(columns, ("d", "download")),
        "last_used_at": first_existing_column(columns, ("t", "last_check_in_time", "last_login_at")),
    }
    if not optional_columns["email"]:
        return {}

    select_parts = [
        f"{quote_identifier(column)} AS {quote_identifier(alias)}"
        for alias, column in optional_columns.items()
        if column
    ]
    query_sql = f"""
        SELECT {", ".join(select_parts)}
        FROM {user_table}
        WHERE LOWER(TRIM({quote_identifier(optional_columns["email"])})) = %s
        LIMIT 1
        """
    cursor.execute(query_sql, (normalized_email,))
    row = cursor.fetchone() or {}
    if not row and fuzzy_email:
        cursor.execute(
            query_sql.replace(
                f"LOWER(TRIM({quote_identifier(optional_columns['email'])})) = %s",
                f"{quote_identifier(optional_columns['email'])} LIKE %s",
            ),
            (fuzzy_email,),
        )
        row = cursor.fetchone() or {}
    if not row:
        logging.info(
            "用户查询未匹配 site=%s type=metron email=%s",
            site_config.get("name", site_config.get("host", "unknown")),
            mask_email(email),
        )
        return {}
    logging.info(
        "用户查询命中 site=%s type=metron email=%s",
        site_config.get("name", site_config.get("host", "unknown")),
        mask_email(row.get("email") or normalized_email),
    )

    last_used_at = query_last_activity(
        cursor,
        row.get("id"),
        build_activity_tables(site_config, METRON_ACTIVITY_TABLES),
    ) if row.get("id") else None

    return {
        "site": site_config.get("name"),
        "plan": row.get("plan"),
        "expired_at": row.get("expired_at"),
        "total_traffic": row.get("transfer_enable"),
        "used_traffic": (row.get("upload") or 0) + (row.get("download") or 0),
        "last_used_at": last_used_at or row.get("last_used_at"),
    }

def query_user_profile_for_site(email, site_config):
    site_name = site_config.get("name", site_config.get("host", "unknown"))
    db_type = str(site_config.get("type", "v2board")).lower()
    db_config = get_site_db_config(site_config)
    if not db_config:
        return {}

    try:
        import pymysql
        connection = pymysql.connect(
            **db_config,
            cursorclass=pymysql.cursors.DictCursor,
            **DB_TIMEOUTS,
        )
        with connection:
            with connection.cursor() as cursor:
                if db_type in ("metron", "mysql", "sspanel", "sspanel-uim"):
                    return query_metron_user(cursor, site_config, email)
                return query_v2board_user(cursor, site_config, email)
    except ImportError:
        if "pymysql" not in USER_LOOKUP_WARNED:
            logging.warning("未安装 PyMySQL，无法查询用户数据库；请执行 pip install -r requirements.txt")
            USER_LOOKUP_WARNED.add("pymysql")
    except Exception as error:
        logging.warning("查询用户数据库失败 site=%s type=%s: %s", site_name, db_type, error)
    return {}

def query_user_profiles_across_sites(email):
    if not email:
        logging.info("Crisp 会话没有邮箱，跳过网站套餐/到期/流量查询")
        return []

    profiles = []
    sites = get_lookup_sites()
    if not sites:
        return []

    for site_config in sites:
        profile = query_user_profile_for_site(email, site_config)
        if profile:
            profiles.append(profile)
    if not profiles:
        logging.info("所有已配置站点均未查到用户 email=%s", mask_email(email))
    return profiles

def get_crisp_conversation(session_id):
    try:
        if hasattr(client.website, "get_conversation"):
            return client.website.get_conversation(websiteId, session_id) or {}
    except Exception as error:
        logging.warning("通过 Crisp SDK 获取会话失败 session=%s: %s", session_id, error)

    crisp_config = config.get("crisp", {})
    try:
        response = requests.get(
            f"{CRISP_API_BASE_URL}/website/{websiteId}/conversation/{session_id}",
            auth=(crisp_config.get("id", ""), crisp_config.get("key", "")),
            headers={"X-Crisp-Tier": "plugin"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("data") or {}
    except Exception as error:
        logging.warning("通过 Crisp REST 获取会话失败 session=%s: %s", session_id, error)
    return {}

def find_first_value(source, keys):
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    for value in source.values():
        if isinstance(value, dict):
            nested_value = find_first_value(value, keys)
            if nested_value not in (None, ""):
                return nested_value
    return None

def first_mapping(source, keys):
    if not isinstance(source, dict):
        return {}
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            return value
    for value in source.values():
        if isinstance(value, dict):
            nested_value = first_mapping(value, keys)
            if nested_value:
                return nested_value
    return {}

def compact_join(values, separator=" / "):
    return separator.join(str(value) for value in values if value not in (None, ""))

def build_visitor_location(conversation, metas):
    sources = [conversation, metas]
    ip = next((find_first_value(source, ("ip", "ip_address", "remote_ip")) for source in sources if find_first_value(source, ("ip", "ip_address", "remote_ip"))), None)
    isp = next((find_first_value(source, ("isp", "as_name", "organization", "org")) for source in sources if find_first_value(source, ("isp", "as_name", "organization", "org"))), None)
    asn = next((find_first_value(source, ("asn", "as_number")) for source in sources if find_first_value(source, ("asn", "as_number"))), None)

    geolocation = {}
    for source in sources:
        geolocation = first_mapping(source, ("geolocation", "geo", "location"))
        if geolocation:
            break

    country = find_first_value(geolocation, ("country", "country_code", "country_name"))
    region = find_first_value(geolocation, ("region", "region_code", "region_name", "province"))
    city = find_first_value(geolocation, ("city", "city_name"))
    location = compact_join((country, region, city))

    if not ip and not location and not isp and not asn:
        return None

    details = []
    if location:
        details.append(location)
    if ip:
        details.append(str(ip))
    network = compact_join((isp, asn), " ")
    if network:
        details.append(network)
    return " | ".join(details)

def parse_traffic_bytes(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    match = re.fullmatch(r"([\d.]+)\s*([kmgtp]?i?b|bytes?)?", text, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    unit = unit.replace("bytes", "b").replace("byte", "b")
    multipliers = {
        "b": 1,
        "kb": 1024,
        "kib": 1024,
        "mb": 1024 ** 2,
        "mib": 1024 ** 2,
        "gb": 1024 ** 3,
        "gib": 1024 ** 3,
        "tb": 1024 ** 4,
        "tib": 1024 ** 4,
        "pb": 1024 ** 5,
        "pib": 1024 ** 5,
    }
    return number * multipliers.get(unit, 1)

def format_traffic(value):
    traffic_bytes = parse_traffic_bytes(value)
    if traffic_bytes is None:
        return str(value) if value not in (None, "") else None
    if traffic_bytes < 0:
        traffic_bytes = 0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_index = 0
    while traffic_bytes >= 1024 and unit_index < len(units) - 1:
        traffic_bytes /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(traffic_bytes)} {units[unit_index]}"
    return f"{traffic_bytes:.2f} {units[unit_index]}"

def format_time(value, zero_text="从未使用"):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        timestamp = int(float(value))
        if timestamp <= 0:
            return zero_text
        if timestamp > 10 ** 12:
            timestamp = timestamp // 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    return str(value)

def build_user_profile_from_crisp_data(data):
    total_traffic = get_meta_value(data, META_ALIASES["total_traffic"])
    used_traffic = get_meta_value(data, META_ALIASES["used_traffic"])
    upload = get_meta_value(data, META_ALIASES["upload"])
    download = get_meta_value(data, META_ALIASES["download"])

    if used_traffic is None and (upload is not None or download is not None):
        used_traffic = (parse_traffic_bytes(upload) or 0) + (parse_traffic_bytes(download) or 0)

    return {
        "plan": get_meta_value(data, META_ALIASES["plan"]),
        "expired_at": get_meta_value(data, META_ALIASES["expired_at"]),
        "remaining_traffic": get_meta_value(data, META_ALIASES["remaining_traffic"]),
        "used_traffic": used_traffic,
        "total_traffic": total_traffic,
        "last_used_at": get_meta_value(data, META_ALIASES["last_used_at"]),
        "last_login_at": get_meta_value(data, META_ALIASES["last_login_at"]),
    }

def merge_missing_profile_values(primary, fallback):
    merged = dict(primary)
    for key, value in fallback.items():
        if merged.get(key) in (None, "") and value not in (None, ""):
            merged[key] = value
    return merged

def get_remaining_traffic(profile):
    remaining_traffic = profile.get("remaining_traffic")
    if remaining_traffic not in (None, ""):
        return format_traffic(remaining_traffic)

    total_traffic = parse_traffic_bytes(profile.get("total_traffic"))
    used_traffic = parse_traffic_bytes(profile.get("used_traffic"))
    if total_traffic is None or used_traffic is None:
        return None
    return format_traffic(total_traffic - used_traffic)

def append_profile_lines(flow, profile, include_site=True):
    site = profile.get("site")
    plan = profile.get("plan")
    expired_at = format_time(profile.get("expired_at"), zero_text="未设置")
    remaining_traffic = get_remaining_traffic(profile)
    last_used_at = format_time(profile.get("last_used_at") or profile.get("last_login_at"), zero_text="从未使用")

    if include_site and site:
        flow.append(f"🌐<b>所属网站</b>：{site}")
    if plan not in (None, ""):
        flow.append(f"🪪<b>使用套餐</b>：{plan}")
    if expired_at:
        flow.append(f"⏰<b>到期时间</b>：{expired_at}")
    if remaining_traffic:
        flow.append(f"📊<b>剩余流量</b>：{remaining_traffic}")
    if last_used_at:
        flow.append(f"🕒<b>上次使用</b>：{last_used_at}")

def getMetas(sessionId, fallback_email=None):
    metas = client.website.get_conversation_metas(websiteId, sessionId)
    conversation = get_crisp_conversation(sessionId)
    visitor_location = build_visitor_location(conversation, metas)
    email = normalize_email(fallback_email) or normalize_email(metas.get("email")) or extract_email_from_value(conversation)
    data = metas.get("data") or {}
    crisp_profile = build_user_profile_from_crisp_data(data)
    db_profiles = query_user_profiles_across_sites(email)

    flow = ['📠<b>Crisp消息推送</b>','']
    if len(email) > 0:
        flow.append(f'📧<b>电子邮箱</b>：{email}')
    if visitor_location:
        flow.append(f'📍<b>客户位置</b>：{visitor_location}')

    if db_profiles:
        for index, db_profile in enumerate(db_profiles):
            if index > 0:
                flow.append("")
            append_profile_lines(flow, merge_missing_profile_values(db_profile, crisp_profile), include_site=True)
    else:
        append_profile_lines(flow, crisp_profile, include_site=False)

    if len(flow) > 2:
        return '\n'.join(flow)
    return '无额外信息'


async def createSession(data):
    bot = callbackContext.bot
    botData = callbackContext.bot_data
    sessionId = data["session_id"]
    session = botData.get(sessionId)
    message_email = extract_email_from_message(data)
    if session is not None and message_email:
        session["email"] = message_email

    metas = getMetas(sessionId, message_email or (session or {}).get("email"))
    if session is None:
        enableAI = False if openai is None else True
        topic = await bot.create_forum_topic(
            groupId,data["user"]["nickname"])
        msg = await bot.send_message(
            groupId,
            metas,
            message_thread_id=topic.message_thread_id,
            reply_markup=changeButton(sessionId,enableAI)
            )
        botData[sessionId] = {
            'topicId': topic.message_thread_id,
            'messageId': msg.message_id,
            'enableAI': enableAI,
            'email': message_email
        }
    else:
        try:
            await bot.edit_message_text(
                metas,
                groupId,
                session['messageId'],
                reply_markup=changeButton(sessionId, session.get('enableAI', False))
            )
        except Exception as error:
            print(error)

async def sendMessage(data):
    bot = callbackContext.bot
    botData = callbackContext.bot_data
    sessionId = data["session_id"]
    session = botData.get(sessionId)

    client.website.mark_messages_read_in_conversation(websiteId,sessionId,
        {"from": "user", "origin": "chat", "fingerprints": [data["fingerprint"]]}
    )

    if data["type"] == "text":
        flow = ['📠<b>消息推送</b>','']
        flow.append(f"🧾<b>消息内容</b>：{data['content']}")

        result, autoreply = getKey(data["content"])
        if result is True:
            flow.append("")
            flow.append(f"💡<b>自动回复</b>：{autoreply}")
        elif openai is not None and session["enableAI"] is True:
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": payload},
                    {"role": "user", "content": data["content"]}
                ]
            )
            autoreply = response.choices[0].message.content
            flow.append("")
            flow.append(f"💡<b>自动回复</b>：{autoreply}")
        
        if autoreply is not None:
            query = {
                "type": "text",
                "content": autoreply,
                "from": "operator",
                "origin": "chat",
                "user": {
                    "nickname": '智能客服',
                    "avatar": 'https://img.ixintu.com/download/jpg/20210125/8bff784c4e309db867d43785efde1daf_512_512.jpg'
                }
            }
            client.website.send_message_in_conversation(websiteId, sessionId, query)
        await bot.send_message(
            groupId,
            '\n'.join(flow),
            message_thread_id=session["topicId"]
        )
    elif data["type"] == "file" and str(data["content"]["type"]).count("image") > 0:
        await bot.send_photo(
            groupId,
            data["content"]["url"],
            message_thread_id=session["topicId"]
        )
    else:
        print("Unhandled Message Type : ", data["type"])

sio = socketio.AsyncClient(reconnection_attempts=5, logger=True)
# Def Event Handlers
@sio.on("connect")
async def connect():
    await sio.emit("authentication", {
        "tier": "plugin",
        "username": config["crisp"]["id"],
        "password": config["crisp"]["key"],
        "events": [
            "message:send",
            "session:set_data"
        ]})
@sio.on("unauthorized")
async def unauthorized(data):
    print('Unauthorized: ', data)
@sio.event
async def connect_error():
    print("The connection failed!")
@sio.event
async def disconnect():
    print("Disconnected from server.")
@sio.on("message:send")
async def messageForward(data):
    if data["website_id"] != websiteId:
        return
    await createSession(data)
    await sendMessage(data)

@sio.on("session:set_data")
async def sessionDataForward(data):
    if data.get("website_id") != websiteId:
        return

    sessionId = data.get("session_id")
    if not sessionId:
        return

    bot = callbackContext.bot
    botData = callbackContext.bot_data
    session = botData.get(sessionId)
    if session is None:
        return

    event_email = extract_email_from_message(data)
    if event_email:
        session["email"] = event_email

    try:
        metas = getMetas(sessionId, event_email or session.get("email"))
        await bot.edit_message_text(
            metas,
            groupId,
            session["messageId"],
            reply_markup=changeButton(sessionId, session.get("enableAI", False))
        )
    except Exception as error:
        logging.warning("刷新 Telegram 会话信息失败 session=%s: %s", sessionId, error)

# Meow!
def getCrispConnectEndpoints():
    url = "https://api.crisp.chat/v1/plugin/connect/endpoints"

    authtier = base64.b64encode(
        (config["crisp"]["id"] + ":" + config["crisp"]["key"]).encode("utf-8")
    ).decode("utf-8")
    payload = ""
    headers = {"X-Crisp-Tier": "plugin", "Authorization": "Basic " + authtier}
    response = requests.request("GET", url, headers=headers, data=payload, timeout=10)
    response.raise_for_status()
    endPoint = json.loads(response.text).get("data").get("socket").get("app")
    return endPoint

# Connecting to Crisp RTM(WSS) Server
async def exec(context: ContextTypes.DEFAULT_TYPE):
    global callbackContext
    callbackContext = context
    # await sendAllUnread()
    if sio.connected:
        return
    await sio.connect(
        getCrispConnectEndpoints(),
        transports="websocket",
        wait_timeout=10,
    )


async def shutdown(application):
    if sio.connected:
        try:
            await asyncio.wait_for(sio.disconnect(), timeout=5)
        except asyncio.TimeoutError:
            logging.warning("Crisp Socket.IO 连接未能在 5 秒内断开，继续停止服务")
        except Exception as error:
            logging.warning("停止 Crisp Socket.IO 连接时出错，继续停止服务: %s", error)
