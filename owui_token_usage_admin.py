#!/usr/bin/env python3
"""
Developed by Nathan I Keiter, Gettysburg College.

Purpose:
  Admin CLI for managing OpenWebUI usage tiers (native token-limit-* groups), tier
  limits, per-user overrides, and usage reports in PostgreSQL.

Usage:
  1) Database connection (either works):
       - With AWS Secrets Manager configured, the DSN is fetched automatically (via boto3).
         Set APP_ENV and AWS_SECRET_ID_<APP_ENV> (for example,
         AWS_SECRET_ID_PROD) to select a Secrets Manager secret. Alternatively,
         set AWS_SECRET_ID directly. AWS_SECRET_DSN_KEY selects the DSN key.
       - For local/manual runs, override by exporting:
           export USAGE_DB_DSN='postgresql://user:pass@host:5432/db'
  2) Run commands:
       ./owui_token_usage_admin.py list-limits
       ./owui_token_usage_admin.py list-members
       ./owui_token_usage_admin.py list-members --group token-limit-pro
       ./owui_token_usage_admin.py assign-user --email user@example.com --group token-limit-pro
       ./owui_token_usage_admin.py remove-user --email user@example.edu --group token-limit-spring-2026
       ./owui_token_usage_admin.py create-tier --group spring-2026 --daily-tokens 200000 --monthly-tokens 2000000
       ./owui_token_usage_admin.py add-users --group token-limit-spring-2026 --csv students.csv --dry-run
       ./owui_token_usage_admin.py add-users --group token-limit-spring-2026 --csv students.csv
       ./owui_token_usage_admin.py delete-tier --group token-limit-spring-2026
       ./owui_token_usage_admin.py import-tiers --csv tiers.csv --dry-run   # several tiers + members from one CSV
       ./owui_token_usage_admin.py import-tiers --csv tiers.csv
       (custom tiers are token-limit-* groups; users that do not exist are created with role "user"
        and link to their SSO sign-in by e-mail. Load the current usage filter into Open WebUI first
        so limits for custom tiers are enforced.)
       (a user has ONE default tier (enterprise/pro/standard/test) and may be in any number of custom
        tiers; custom tier limits are summed and replace the default tier's limits.)
       ./owui_token_usage_admin.py audit-tiers
       ./owui_token_usage_admin.py assign-default-tier --all --dry-run          # users with no default tier
       ./owui_token_usage_admin.py assign-default-tier --all --group token-limit-standard
       ./owui_token_usage_admin.py assign-default-tier --email a@example.edu --email b@example.edu
       ./owui_token_usage_admin.py list-members --details
       ./owui_token_usage_admin.py set-group-limit --group token-limit-standard --daily-tokens 500000 --monthly-tokens 5000000
       ./owui_token_usage_admin.py show-user --user-id OPENWEBUI_USER_UUID_HERE

Dependencies:
  - Python 3.9+
  - psycopg with binary extras:
      pip install "psycopg[binary]"
  - boto3 for AWS Secrets Manager access

Prerequisites:
  - Network access to the PostgreSQL instance used by the OpenWebUI usage tracking filter.
  - Database schema already initialized by the filter (usage_limits, user_limit_overrides,
    resolve_user_tier/get_user_usage_status functions, etc.) and its built-in tier groups created.
    Tier membership lives in the native Open WebUI group tables.
  - Either USAGE_DB_DSN is exported, or AWS_SECRET_ID (or APP_ENV and its
    AWS_SECRET_ID_<APP_ENV> mapping) is set and the AWS identity has permission
    to call secretsmanager:GetSecretValue on the target secret.
"""

import argparse
import calendar
import json
import math
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import psycopg
from botocore.exceptions import BotoCoreError, ClientError

USAGE_TIMEZONE_NAME = os.environ.get("USAGE_TIMEZONE", "UTC")
USAGE_TZ = ZoneInfo(USAGE_TIMEZONE_NAME)
DEFAULT_SECRET_DSN_KEY = "DATABASE_URL"

TIER_PREFIX = os.environ.get("USAGE_TIER_PREFIX", "token-limit-")
BUILTIN_TIERS = [
    tier.strip()
    for tier in os.environ.get(
        "USAGE_BUILTIN_TIERS",
        ",".join(
            f"{TIER_PREFIX}{tier}"
            for tier in ("enterprise", "pro", "standard", "test")
        ),
    ).split(",")
    if tier.strip()
]
DEFAULT_TIER = os.environ.get("USAGE_DEFAULT_TIER", f"{TIER_PREFIX}standard")
DEFAULT_DAILY = int(os.environ.get("USAGE_DEFAULT_DAILY_TOKENS", "500000"))
DEFAULT_MONTHLY = int(os.environ.get("USAGE_DEFAULT_MONTHLY_TOKENS", "5000000"))
if not TIER_PREFIX:
    raise ValueError("USAGE_TIER_PREFIX must not be empty")
if len(TIER_PREFIX) >= 50:
    raise ValueError("USAGE_TIER_PREFIX must be shorter than the 50-character group name limit")
if len(set(BUILTIN_TIERS)) != len(BUILTIN_TIERS) or not BUILTIN_TIERS:
    raise ValueError("USAGE_BUILTIN_TIERS must contain unique, non-empty group names")
if DEFAULT_TIER not in BUILTIN_TIERS:
    raise ValueError("USAGE_DEFAULT_TIER must be included in USAGE_BUILTIN_TIERS")
if any(not tier.startswith(TIER_PREFIX) for tier in BUILTIN_TIERS):
    raise ValueError("Every USAGE_BUILTIN_TIERS entry must start with USAGE_TIER_PREFIX")
if any(limit < -1 or limit > 10**12 for limit in (DEFAULT_DAILY, DEFAULT_MONTHLY)):
    raise ValueError("USAGE_DEFAULT_*_TOKENS must be -1 (unlimited) or between 0 and 10^12")


def resolve_secret_id():
    explicit = os.environ.get("AWS_SECRET_ID")
    if explicit:
        return explicit

    app_env = (os.environ.get("APP_ENV") or "").strip()
    if not app_env:
        raise RuntimeError(
            "Neither AWS_SECRET_ID nor APP_ENV is set. "
            "Set AWS_SECRET_ID directly, or set APP_ENV and "
            "AWS_SECRET_ID_<APP_ENV> (for example AWS_SECRET_ID_PROD)."
        )

    secret_variable = f"AWS_SECRET_ID_{app_env.upper()}"
    secret_id = os.environ.get(secret_variable)
    if not secret_id:
        raise RuntimeError(
            f"{secret_variable} is not set. Set it to the Secrets Manager secret "
            "for this environment, or set AWS_SECRET_ID directly."
        )
    return secret_id


def get_dsn_from_secrets_manager():
    secret_id = resolve_secret_id()
    dsn_key = os.environ.get("AWS_SECRET_DSN_KEY", DEFAULT_SECRET_DSN_KEY)

    client = boto3.client("secretsmanager")

    try:
        response = client.get_secret_value(SecretId=secret_id)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to fetch secret {secret_id!r} from Secrets Manager: {exc}") from exc

    secret_string = response.get("SecretString")
    if not secret_string:
        raise RuntimeError("Secrets Manager response did not contain SecretString")

    try:
        secret_data = json.loads(secret_string)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Secrets Manager SecretString is not valid JSON") from exc

    dsn = secret_data.get(dsn_key)
    if not dsn:
        raise RuntimeError(f"Secret {secret_id!r} has no key {dsn_key!r}")

    return dsn


def get_conn():
    dsn = os.environ.get("USAGE_DB_DSN")

    if not dsn:
        try:
            dsn = get_dsn_from_secrets_manager()
        except RuntimeError as exc:
            print(f"ERROR: Could not resolve database DSN: {exc}", file=sys.stderr)
            print(
                "Set USAGE_DB_DSN, e.g. postgresql://user:pass@host:5432/db, "
                "or ensure your AWS credentials can access the selected Secrets Manager secret.",
                file=sys.stderr,
            )
            sys.exit(2)

    return psycopg.connect(dsn)


def get_connection_resolution_status():
    if os.environ.get("USAGE_DB_DSN"):
        return "Database connection resolved using USAGE_DB_DSN."

    try:
        secret_id = resolve_secret_id()
        get_dsn_from_secrets_manager()
    except RuntimeError:
        app_env = (os.environ.get("APP_ENV") or "").strip()
        secret_variable = f"AWS_SECRET_ID_{app_env.upper()}" if app_env else None
        if app_env and secret_variable and os.environ.get(secret_variable) and not os.environ.get("AWS_SECRET_ID"):
            return (
                "Database connection could not be resolved. Tried APP_ENV, but "
                f"{os.environ[secret_variable]} secret is unreachable."
            )
        return "Database connection could not be resolved, please check configuration options."

    if os.environ.get("AWS_SECRET_ID"):
        return f"Database connection resolved using AWS_SECRET_ID to fetch {secret_id} secret."

    return f"Database connection resolved using APP_ENV to fetch {secret_id} secret."


def tokens_from_usd(usd, blended_usd_per_1m):
    if usd is None:
        return None
    if blended_usd_per_1m is None or blended_usd_per_1m <= 0:
        raise ValueError("--blended-usd-per-1m must be > 0 when using dollar limits")
    return int(math.floor((usd / blended_usd_per_1m) * 1_000_000))


def resolve_limits(args):
    daily_tokens = args.daily_tokens
    monthly_tokens = args.monthly_tokens

    if args.daily_usd is not None:
        daily_tokens = tokens_from_usd(args.daily_usd, args.blended_usd_per_1m)

    if args.monthly_usd is not None:
        monthly_tokens = tokens_from_usd(args.monthly_usd, args.blended_usd_per_1m)

    if daily_tokens is None and monthly_tokens is None:
        raise ValueError("Provide at least one of --daily-tokens, --monthly-tokens, --daily-usd, --monthly-usd")

    for field, value in (("daily", daily_tokens), ("monthly", monthly_tokens)):
        if value is not None and (value < -1 or value > MAX_TOKENS):
            raise ValueError(f"{field} limit must be -1 (unlimited) or a number up to {MAX_TOKENS:,}")
    description = getattr(args, "description", None)
    if description is not None and len(description) > MAX_TEXT_LEN:
        raise ValueError(f"--description must be {MAX_TEXT_LEN} characters or fewer")

    return daily_tokens, monthly_tokens


def audit(action, **details):
    """One JSON line on stderr per change, mirroring the console's AUDIT log entries."""
    admin = os.environ.get("USER") or os.environ.get("LOGNAME") or "admin-cli"
    print("AUDIT " + json.dumps({"admin": admin, "action": action, **details}), file=sys.stderr)


# Usage tiers are native Open WebUI groups. Built-in tiers must match the
# filter's tier configuration and are ordered by tie-break priority.
MAX_TOKENS = 10**12  # same bounds as the management console
MAX_TEXT_LEN = 500
MAX_SLUG_LEN = 50 - len(TIER_PREFIX)  # usage_limits.group_name is VARCHAR(50)
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
EMAIL_RE = re.compile(r"[^@\s,;<>\"']{1,64}@[^@\s,;<>\"']{1,255}\.[^@\s,;<>\"']{2,63}")
MAX_BULK_USERS = 1000
NEW_USER_ROLE = "user"
# bcrypt hash of a random value that was discarded: accounts created here cannot use password
# login and sign in with SSO, which links to the account by e-mail (same as the console).
UNUSABLE_PASSWORD_HASH = "$2b$12$SIz9JXp6toQgk76cEJDlB.wMwMOUciUf7EBakLxE1s0NRUQWwwOu."
STATUS_KEYS = (
    "group_name",
    "daily_limit",
    "monthly_limit",
    "tokens_today",
    "tokens_this_month",
    "is_over_daily",
    "is_over_monthly",
    "limit_source",
)


def format_tokens(n):
    """500000 -> '500K', 5000000 -> '5M', -1 -> 'unlimited'."""
    if n is None:
        return "?"
    if n == -1:
        return "unlimited"
    for size, unit in ((10**9, "B"), (10**6, "M"), (10**3, "K")):
        if n >= size:
            return f"{n / size:.2f}".rstrip("0").rstrip(".") + unit
    return str(n)


def default_description(daily, monthly):
    """Description used when none is given, e.g. '500K/day, 5M/month'."""
    return f"{format_tokens(daily)}/day, {format_tokens(monthly)}/month"


def parse_limit(value):
    """Lenient CSV limit parser: 500000, '500,000', '500K', '5M', '-1'. None when blank."""
    s = str(value or "").strip().lower().replace(",", "").replace("_", "")
    if not s:
        return None
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)([km]?)", s)
    if not m:
        raise ValueError(f"{str(value)[:30]!r} is not a number")
    n = float(m.group(1)) * {"": 1, "k": 10**3, "m": 10**6}[m.group(2)]
    if n != int(n) or n < -1 or n > MAX_TOKENS:
        raise ValueError(f"{str(value)[:30]!r} must be -1 (unlimited) or a whole number up to {MAX_TOKENS:,}")
    return int(n)


def tier_names(cur):
    """Built-in tiers (priority order) followed by custom tiers registered in usage_limits."""
    cur.execute(
        "SELECT group_name FROM usage_limits WHERE starts_with(group_name, %s) AND NOT group_name = ANY(%s) ORDER BY group_name",
        (TIER_PREFIX, BUILTIN_TIERS),
    )
    return BUILTIN_TIERS + [r[0] for r in cur.fetchall()]


def validate_tier(cur, name):
    names = tier_names(cur)
    if name not in names:
        raise ValueError(f"{name!r} is not a usage tier. Valid tiers: {', '.join(names)}")
    return names


def custom_tier_name(raw):
    """Normalize a tier slug or prefixed tier name to the configured prefix."""
    slug = re.sub(r"[\s_]+", "-", (raw or "").strip().lower()).removeprefix(TIER_PREFIX).strip("-")
    if not slug or len(slug) > MAX_SLUG_LEN or not SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"Tier name must be 1-{MAX_SLUG_LEN} characters: letters, numbers and single hyphens "
            f"(it is stored as {TIER_PREFIX}<name>)"
        )
    name = TIER_PREFIX + slug
    if name in BUILTIN_TIERS:
        raise ValueError(f"{name} is a built-in tier")
    return name


def tier_group_ids(cur, names):
    cur.execute('SELECT id, name FROM "group" WHERE name = ANY(%s)', (names,))
    return {name: gid for gid, name in cur.fetchall()}


def table_exists(cur, name):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    return cur.fetchone()[0]


def insert_group(cur, name, description, daily, monthly):
    """Create the native Open WebUI group behind a tier (same shape the filter creates)."""
    now = int(time.time())
    gid = str(uuid.uuid4())
    cur.execute(
        """INSERT INTO "group" (id, user_id, name, description, data, meta, permissions, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (gid, "", name, description or default_description(daily, monthly),
         json.dumps({"daily_token_limit": daily, "monthly_token_limit": monthly}),
         json.dumps({}), json.dumps({}), now, now),
    )
    return gid


def place_users(cur, user_ids, tier):
    """Add users to `tier` (idempotent). A built-in tier replaces the user's other built-in tier;
    a custom tier is added alongside whatever else the user already has."""
    names = tier_names(cur)
    ids = tier_group_ids(cur, names)
    if tier not in ids:
        if tier in BUILTIN_TIERS:
            raise ValueError(
                f"Native group {tier!r} does not exist yet. "
                "Load the usage tracking filter once (send a chat) so it auto-creates the tier groups."
            )
        ids[tier] = insert_group(cur, tier, None, DEFAULT_DAILY, DEFAULT_MONTHLY)
    target = ids[tier]
    now = int(time.time())
    cur.execute("SELECT user_id FROM group_member WHERE group_id = %s AND user_id = ANY(%s)", (target, user_ids))
    already = len(cur.fetchall())
    moved = 0
    if tier in BUILTIN_TIERS:
        defaults = [ids[t] for t in BUILTIN_TIERS if t in ids]
        cur.execute(
            """DELETE FROM group_member WHERE user_id = ANY(%s) AND group_id = ANY(%s) AND group_id <> %s
               RETURNING user_id""",
            (user_ids, defaults, target),
        )
        moved = len({r[0] for r in cur.fetchall()})
    # Fresh created_at makes a built-in target the newest default membership, so it wins resolve_user_tier().
    cur.executemany(
        """INSERT INTO group_member (id, group_id, user_id, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (group_id, user_id) DO UPDATE SET created_at = EXCLUDED.created_at,
                                                          updated_at = EXCLUDED.updated_at""",
        [(str(uuid.uuid4()), target, uid, now, now) for uid in user_ids],
    )
    cur.execute('UPDATE "group" SET updated_at = %s WHERE id = ANY(%s)', (now, list(ids.values())))
    return {"already_in_tier": already, "moved_from_other_tiers": moved}


def table_columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s",
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


def insert_rows(cur, table, columns, rows):
    cols = list(columns)
    cur.executemany(
        f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join(["%s"] * len(cols))})',
        [[row[c] for c in cols] for row in rows],
    )


def create_users(cur, entries):
    """Pre-provision Open WebUI accounts for (email, name) pairs and return their new ids.

    Only columns that exist in this Open WebUI version are written. The account is linked to the
    person's SSO login by e-mail the first time they sign in.
    """
    now = int(time.time())
    user_cols = table_columns(cur, "user")
    auth_cols = table_columns(cur, "auth")
    users, auths, ids = [], [], []
    for email, name in entries:
        uid = str(uuid.uuid4())
        ids.append(uid)
        users.append({
            "id": uid, "email": email, "name": name, "role": NEW_USER_ROLE,
            "profile_image_url": "/user.png", "last_active_at": now, "created_at": now, "updated_at": now,
        })
        auths.append({
            "id": uid, "email": email, "password": UNUSABLE_PASSWORD_HASH, "active": True,
            "session_stamp": str(uuid.uuid4()),
        })
    insert_rows(cur, "user", [c for c in users[0] if c in user_cols], users)
    if auth_cols:
        insert_rows(cur, "auth", [c for c in auths[0] if c in auth_cols], auths)
    return ids


def status_dict(row):
    return dict(zip(STATUS_KEYS, row))


def resolve_user_id(cur, args):
    """Accept --user-id (UUID) or --email and return the Open WebUI user id."""
    if getattr(args, "email", None):
        cur.execute('SELECT id FROM "user" WHERE LOWER(email) = LOWER(%s)', (args.email,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"No Open WebUI user with email {args.email!r}")
        return row[0]
    if not getattr(args, "user_id", None):
        raise ValueError("Provide --user-id or --email")
    cur.execute('SELECT id FROM "user" WHERE id = %s', (args.user_id,))
    if not cur.fetchone():
        raise ValueError(f"No Open WebUI user with id {args.user_id!r}")
    return args.user_id


def set_group_limit(args):
    daily_tokens, monthly_tokens = resolve_limits(args)

    with get_conn() as conn:
        with conn.cursor() as cur:
            validate_tier(cur, args.group)
            cur.execute(
                """
                INSERT INTO usage_limits (
                  group_name,
                  daily_token_limit,
                  monthly_token_limit,
                  description,
                  updated_at
                )
                VALUES (
                  %(group_name)s,
                  COALESCE(%(daily)s, %(default_daily)s),
                  COALESCE(%(monthly)s, %(default_monthly)s),
                  %(description)s,
                  NOW()
                )
                ON CONFLICT (group_name) DO UPDATE SET
                  daily_token_limit = COALESCE(%(daily)s, usage_limits.daily_token_limit),
                  monthly_token_limit = COALESCE(%(monthly)s, usage_limits.monthly_token_limit),
                  description = COALESCE(%(description)s, usage_limits.description),
                  updated_at = NOW()
                """,
                {
                    "group_name": args.group,
                    "daily": daily_tokens,
                    "monthly": monthly_tokens,
                    "default_daily": DEFAULT_DAILY,
                    "default_monthly": DEFAULT_MONTHLY,
                    "description": args.description,
                },
            )

    audit("set_tier_limits", tier=args.group, daily=daily_tokens, monthly=monthly_tokens)
    print(f"Updated tier {args.group}: daily={daily_tokens}, monthly={monthly_tokens}")


def set_all_limits(args):
    """Update every tier, built-in and custom (the same limits are applied to all of them)."""
    daily_tokens, monthly_tokens = resolve_limits(args)

    set_parts = []
    params = {}

    if daily_tokens is not None:
        set_parts.append("daily_token_limit = %(daily)s")
        params["daily"] = daily_tokens

    if monthly_tokens is not None:
        set_parts.append("monthly_token_limit = %(monthly)s")
        params["monthly"] = monthly_tokens

    set_parts.append("updated_at = NOW()")

    with get_conn() as conn:
        with conn.cursor() as cur:
            tiers = tier_names(cur)
            params["tiers"] = tiers
            cur.execute(
                f"UPDATE usage_limits SET {', '.join(set_parts)} WHERE group_name = ANY(%(tiers)s)", params
            )
            updated = cur.rowcount

    audit("set_all_limits", tiers=tiers, daily=daily_tokens, monthly=monthly_tokens)
    print(f"Updated {updated} tier(s): daily={daily_tokens}, monthly={monthly_tokens}")
    custom = [t for t in tiers if t not in BUILTIN_TIERS]
    if custom:
        print(f"Note: this included custom tier(s): {', '.join(custom)}")


def remove_user(args):
    """Remove a user from one tier group (native membership). Their other tiers are untouched."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            validate_tier(cur, args.group)
            user_id = resolve_user_id(cur, args)
            cur.execute(
                'DELETE FROM group_member WHERE user_id = %s AND group_id IN (SELECT id FROM "group" WHERE name = %s)',
                (user_id, args.group),
            )
            removed = cur.rowcount
            cur.execute("SELECT * FROM get_user_usage_status(%s)", (user_id,))
            status = status_dict(cur.fetchone())
    audit("remove_from_tier", user_id=user_id, tier=args.group, removed=removed)
    print(f"Removed user {user_id} from {args.group}." if removed else f"User {user_id} was not in {args.group}.")
    print(f"Effective status now: {status}")


def assign_user(args):
    """Add a user to a tier using native Open WebUI group membership.

    A built-in tier replaces the user's other built-in tier (one default tier per user).
    A custom tier is added alongside their other tiers; several custom tiers' limits add up.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            validate_tier(cur, args.group)
            user_id = resolve_user_id(cur, args)
            result = place_users(cur, [user_id], args.group)
            cur.execute("SELECT * FROM get_user_usage_status(%s)", (user_id,))
            status = status_dict(cur.fetchone())

    audit("assign_tier", user_id=user_id, tier=args.group, removed_from=result["moved_from_other_tiers"])
    print(f"Assigned user {user_id} to {args.group}" + (" (replaced their previous default tier)." if result["moved_from_other_tiers"] else "."))
    print(f"Effective status now: {status}")


def create_tier(args):
    """Create a custom tier: a usage_limits row plus its native Open WebUI group."""
    name = custom_tier_name(args.group)
    daily = args.daily_tokens
    monthly = args.monthly_tokens
    if args.daily_usd is not None:
        daily = tokens_from_usd(args.daily_usd, args.blended_usd_per_1m)
    if args.monthly_usd is not None:
        monthly = tokens_from_usd(args.monthly_usd, args.blended_usd_per_1m)
    daily = DEFAULT_DAILY if daily is None else daily
    monthly = DEFAULT_MONTHLY if monthly is None else monthly
    for field, value in (("daily", daily), ("monthly", monthly)):
        if value < -1 or value > MAX_TOKENS:
            raise ValueError(f"{field} limit must be -1 (unlimited) or a number up to {MAX_TOKENS:,}")
    if args.description is not None and len(args.description) > MAX_TEXT_LEN:
        raise ValueError(f"--description must be {MAX_TEXT_LEN} characters or fewer")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM usage_limits WHERE LOWER(group_name) = %s", (name,))
            if cur.fetchone():
                raise ValueError(f"Tier {name} already exists")
            cur.execute('SELECT 1 FROM "group" WHERE LOWER(name) = %s', (name,))
            if cur.fetchone():
                raise ValueError(f"An Open WebUI group named {name} already exists; choose another name")
            insert_group(cur, name, args.description, daily, monthly)
            cur.execute(
                """INSERT INTO usage_limits (group_name, daily_token_limit, monthly_token_limit, description, updated_at)
                   VALUES (%s, %s, %s, %s, NOW())""",
                (name, daily, monthly, args.description or default_description(daily, monthly)),
            )

    audit("create_tier", tier=name, daily=daily, monthly=monthly)
    print(f"Created tier {name}: daily={daily}, monthly={monthly}")
    print(f"Add members with: add-users --group {name} --email <email> | --csv FILE")


def delete_tier(args):
    """Delete a custom tier. Members with no other tier return to the Standard tier."""
    tier = args.group
    if tier in BUILTIN_TIERS:
        raise ValueError("Built-in tiers cannot be deleted")

    with get_conn() as conn:
        with conn.cursor() as cur:
            if tier not in tier_names(cur):
                raise ValueError(f"{tier!r} is not a custom usage tier")
            cur.execute('SELECT id FROM "group" WHERE name = %s', (tier,))
            row = cur.fetchone()
            members = 0
            if row:
                cur.execute("SELECT COUNT(*) FROM group_member WHERE group_id = %s", (row[0],))
                members = cur.fetchone()[0]
            if not args.yes:
                print(
                    f"This deletes tier {tier} and its Open WebUI group. Its {members} member(s) who are in "
                    f"no other tier will be returned to {DEFAULT_TIER}."
                )
                if input("Type the tier name to confirm: ").strip() != tier:
                    raise ValueError("Cancelled")
            cur.execute('SELECT id FROM "group" WHERE name = %s', (DEFAULT_TIER,))
            standard = cur.fetchone()
            if not standard:
                raise ValueError(
                    f"Native group {DEFAULT_TIER!r} does not exist yet; load the usage tracking filter once to create it"
                )
            returned = 0
            if row:
                gid = row[0]
                # Members with no other tier return to Standard; anyone in another tier keeps it.
                cur.execute(
                    """SELECT gm.user_id FROM group_member gm
                       WHERE gm.group_id = %s AND NOT EXISTS (
                           SELECT 1 FROM group_member o JOIN "group" og ON og.id = o.group_id
                           WHERE o.user_id = gm.user_id AND og.id <> %s AND og.name = ANY(%s))""",
                    (gid, gid, tier_names(cur)),
                )
                orphans = [r[0] for r in cur.fetchall()]
                now = int(time.time())
                cur.executemany(
                    """INSERT INTO group_member (id, group_id, user_id, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (group_id, user_id) DO UPDATE SET created_at = EXCLUDED.created_at,
                                                                      updated_at = EXCLUDED.updated_at""",
                    [(str(uuid.uuid4()), standard[0], uid, now, now) for uid in orphans],
                )
                returned = len(orphans)
                if table_exists(cur, "access_grant"):
                    cur.execute("DELETE FROM access_grant WHERE principal_type = 'group' AND principal_id = %s", (gid,))
                cur.execute("DELETE FROM group_member WHERE group_id = %s", (gid,))
                cur.execute('DELETE FROM "group" WHERE id = %s', (gid,))
            if table_exists(cur, "user_groups"):
                cur.execute("DELETE FROM user_groups WHERE group_name = %s", (tier,))
            cur.execute("DELETE FROM usage_limits WHERE group_name = %s", (tier,))

    audit("delete_tier", tier=tier, members=members, returned_to_standard=returned)
    print(f"Deleted tier {tier}: {members} member(s), {returned} returned to {DEFAULT_TIER}.")


def read_csv_rows(path):
    """Read a CSV in any layout: the delimiter (comma, semicolon, tab, pipe) is sniffed from the first
    lines. Returns (rows, start_lines) where rows are lists of stripped cells."""
    import csv

    with open(path, newline="", encoding="utf-8-sig") as fh:
        text = fh.read()
    sample = re.sub(r'"[^"]*"', "", "\n".join(text.splitlines()[:5]))
    delim = max([",", ";", "\t", "|"], key=lambda d: sample.count(d))
    rows, starts, prev = [], [], 0
    reader = csv.reader(text.splitlines(keepends=True), delimiter=delim)
    # csv.reader over line pieces keeps quoted newlines inside one record
    for r in reader:
        cells = [c.strip() for c in r]
        if any(cells):
            rows.append(cells)
            starts.append(prev + 1)
        prev = reader.line_num
    return rows, starts


def read_csv_people(path):
    """Read (email, name) pairs from a CSV of any layout.

    The email column is detected automatically (the column with the most valid addresses; a header
    containing "mail" breaks ties) and all other columns are ignored, except a "name" column.
    """
    rows, _ = read_csv_rows(path)

    def find(cell):
        m = re.search(r"[^\s<>,;\"']+@[^\s<>,;\"']+\.[^\s<>,;\"']+", cell or "")
        return m.group(0).lower() if m and EMAIL_RE.fullmatch(m.group(0)) else ""

    width = max((len(r) for r in rows), default=0)
    email_col, best = -1, 0
    for c in range(width):
        n = sum(1 for r in rows if c < len(r) and find(r[c]))
        header_hit = bool(re.search(r"e-?mail", rows[0][c], re.I)) if c < len(rows[0]) else False
        current_hit = email_col >= 0 and email_col < len(rows[0]) and bool(re.search(r"e-?mail", rows[0][email_col], re.I))
        if n > best or (n == best and n > 0 and header_hit and not current_hit):
            email_col, best = c, n
    if email_col < 0:
        if rows:
            raise ValueError(f"No column with email addresses was found in {path}")
        return [], []
    has_header = "@" not in (rows[0][email_col] if email_col < len(rows[0]) else "")
    name_col = -1
    if has_header:
        header = [c.lower() for c in rows[0]]
        name_col = next((i for i, c in enumerate(header) if c in ("name", "full name", "display name", "fullname")), -1)
        rows = rows[1:]
    people, invalid = [], []
    for r in rows:
        email = find(r[email_col]) if email_col < len(r) else ""
        if email:
            people.append((email, r[name_col] if 0 <= name_col < len(r) else ""))
        else:
            invalid.append((r[email_col] if email_col < len(r) else "") or " ".join(r))
    return people, invalid


IMPORT_COLUMNS = {
    "group": ("groupname", "group", "tier", "tiername"),
    "email": ("email", "emailaddress", "mail"),
    "daily": ("dailytokenlimit", "dailylimit", "daily"),
    "monthly": ("monthlytokenlimit", "monthlylimit", "monthly"),
    "description": ("description", "desc"),
}
MAX_IMPORT_ROWS = 10000
MAX_IMPORT_GROUPS = 100


def read_import_rows(path):
    """Parse a multi-group CSV: group-name,email,daily-token-limit,monthly-token-limit,description."""
    all_rows, starts = read_csv_rows(path)
    if not all_rows:
        raise ValueError("The file is empty")
    norm = lambda c: re.sub(r"[^a-z0-9]", "", c.lower())
    idx = {k: next((i for i, c in enumerate(all_rows[0]) if norm(c) in names), -1) for k, names in IMPORT_COLUMNS.items()}
    # With no recognisable column names the first row is data and the columns are positional.
    has_header = any(i >= 0 for i in idx.values())
    if not has_header:
        if len(all_rows[0]) < 2 or not EMAIL_RE.fullmatch(all_rows[0][1]):
            raise ValueError(
                "No header row was found, and the second column of the first row is not an email address. "
                "Use the column order group-name,email,daily-token-limit,monthly-token-limit,description"
            )
        idx = {k: i for i, k in enumerate(IMPORT_COLUMNS)}
    missing = [("group-name" if k == "group" else k) for k in ("group", "email") if idx[k] < 0]
    if missing:
        raise ValueError(
            f"The header row must include the columns group-name and email (missing: {', '.join(missing)}). "
            f"Found: {', '.join(all_rows[0])}"
        )
    get = lambda r, k: r[idx[k]] if 0 <= idx[k] < len(r) else ""
    rows = [
        {"line": starts[i], "group": get(r, "group"), "email": get(r, "email"), "daily": get(r, "daily"),
         "monthly": get(r, "monthly"), "description": get(r, "description")}
        for i, r in enumerate(all_rows) if i >= (1 if has_header else 0)
    ]
    if not rows:
        raise ValueError("The file has a header but no data rows")
    return rows


def plan_import(cur, rows):
    """Validate multi-group CSV rows and describe what importing them would do. Writes nothing.

    Rows are grouped by group name wherever they appear in the file. Any error blocks the whole import.
    """
    if len(rows) > MAX_IMPORT_ROWS:
        raise ValueError(f"At most {MAX_IMPORT_ROWS} rows can be imported at once")
    cur.execute("SELECT LOWER(group_name) FROM usage_limits")
    taken = {r[0] for r in cur.fetchall()}
    cur.execute('SELECT LOWER(name) FROM "group"')
    taken |= {r[0] for r in cur.fetchall()}

    groups, order, global_errors = {}, [], []
    for r in rows:
        raw = (r.get("group") or "").strip()
        if not raw:
            global_errors.append(f"Line {r['line']}: group-name is empty")
            continue
        try:
            key, name_error = custom_tier_name(raw), None
        except ValueError as e:
            key, name_error = "invalid:" + raw.lower(), str(e)
        if key not in groups:
            groups[key] = {"name": key if name_error is None else raw, "input_name": raw, "rows": [],
                           "errors": [name_error] if name_error else [], "warnings": []}
            order.append(key)
        groups[key]["rows"].append(r)
    if len(order) > MAX_IMPORT_GROUPS:
        raise ValueError(f"At most {MAX_IMPORT_GROUPS} groups can be imported at once")

    all_emails = set()
    for key in order:
        g = groups[key]
        emails, dupes, desc = [], 0, None
        best = {"daily": None, "monthly": None}
        bad = {"email": [], "daily": [], "monthly": []}
        for r in g["rows"]:
            email = (r.get("email") or "").strip().lower()
            if not email:
                bad["email"].append(f"Line {r['line']}: email is empty")
            elif len(email) > 254 or not EMAIL_RE.fullmatch(email):
                bad["email"].append(f"Line {r['line']}: invalid email address {email[:60]!r}")
            elif email in emails:
                dupes += 1
            else:
                emails.append(email)
            for field in ("daily", "monthly"):
                try:
                    v = parse_limit(r.get(field))
                except ValueError as e:
                    bad[field].append(f"Line {r['line']}: {field}-token-limit {e}")
                    continue
                cur_best = best[field]
                # -1 means unlimited, so it counts as the greatest value
                if v is not None and (cur_best is None or (v == -1 and cur_best != -1) or (cur_best != -1 and v > cur_best)):
                    best[field] = v
            d = (r.get("description") or "").strip()
            if desc is None and d:
                desc = d
        g["errors"] += bad["email"][:10] + bad["daily"][:10] + bad["monthly"][:10]
        extra = sum(max(len(v) - 10, 0) for v in bad.values())
        if extra:
            g["errors"].append(f"{extra} more row error(s) not shown")
        for field in ("daily", "monthly"):
            if best[field] is None and not bad[field]:
                g["errors"].append(f"{field}-token-limit is required on at least one row of the group")
        if desc is not None and len(desc) > MAX_TEXT_LEN:
            g["errors"].append(f"description is longer than {MAX_TEXT_LEN} characters")
            desc = None
        if not emails and not bad["email"]:
            g["errors"].append("The group has no members")
        if dupes:
            g["warnings"].append(f"{dupes} duplicate row(s) for the same email ignored")
        if not key.startswith("invalid:") and g["name"].lower() in taken:
            g["errors"].append(f"A group named {g['name']} already exists in the system")
        g.update(daily=best["daily"], monthly=best["monthly"], emails=emails, description_defaulted=desc is None,
                 description=desc or (default_description(best["daily"], best["monthly"])
                                      if best["daily"] is not None and best["monthly"] is not None else None))
        all_emails.update(emails)

    existing = {}
    if all_emails:
        cur.execute('SELECT LOWER(email), id FROM "user" WHERE LOWER(email) = ANY(%s)', (sorted(all_emails),))
        existing = dict(cur.fetchall())
    for key in order:
        g = groups[key]
        g["status"] = "error" if g["errors"] else "create"
        g["new_users"] = sum(1 for e in g["emails"] if e not in existing)
    return groups, order, existing, global_errors


def print_import_plan(groups, order, existing, global_errors):
    for e in global_errors[:20]:
        print(f"  ERROR  {e}")
    for key in order:
        g = groups[key]
        label = "ERROR " if g["status"] == "error" else "CREATE"
        shown = f"{g['name']}" + (f' (from "{g["input_name"]}")' if g["input_name"] != g["name"] else "")
        lim = f"{format_tokens(g['daily'])}/day, {format_tokens(g['monthly'])}/month" if g["daily"] is not None and g["monthly"] is not None else "limits missing"
        print(f"  {label} {shown}: {lim}, {len(g['emails'])} member(s) ({g['new_users']} new)"
              f", description: {g['description']!r}{' (default)' if g['description_defaulted'] else ''}")
        for m in g["errors"]:
            print(f"           error: {m}")
        for m in g["warnings"]:
            print(f"           warning: {m}")


def import_tiers(args):
    """Create several custom tiers (limits + members) from one CSV; shows a summary first."""
    rows = read_import_rows(args.csv)  # parsed in memory only; the file is never copied anywhere
    with get_conn() as conn:
        with conn.cursor() as cur:
            groups, order, existing, global_errors = plan_import(cur, rows)
            ok = bool(order) and not global_errors and all(groups[k]["status"] == "create" for k in order)
            print(f"Import summary for {args.csv}: {len(order)} group(s), {len(rows)} row(s)")
            print_import_plan(groups, order, existing, global_errors)
            if not ok:
                conn.rollback()
                raise ValueError("The file has errors; nothing was created. Fix the CSV and run it again.")
            if args.dry_run:
                print("DRY RUN: nothing was changed.")
                conn.rollback()
                return
            if not args.yes and input("Create these groups? Type yes to confirm: ").strip().lower() != "yes":
                conn.rollback()
                raise ValueError("Cancelled")
            fresh = sorted({e for k in order for e in groups[k]["emails"]} - set(existing))
            created = dict(zip(fresh, create_users(cur, [(e, e.split("@")[0]) for e in fresh]))) if fresh else {}
            ids = {**existing, **created}
            for key in order:
                g = groups[key]
                insert_group(cur, key, g["description"], g["daily"], g["monthly"])
                cur.execute(
                    """INSERT INTO usage_limits (group_name, daily_token_limit, monthly_token_limit, description, updated_at)
                       VALUES (%s, %s, %s, %s, NOW())""",
                    (key, g["daily"], g["monthly"], g["description"]),
                )
                place_users(cur, [ids[e] for e in g["emails"]], key)
    audit("import_tiers", groups=order, users=sum(len(groups[k]["emails"]) for k in order), created_users=len(created))
    print(f"Created {len(order)} group(s); {len(created)} new user(s) created.")


def add_users(args):
    """Bulk-add users (by e-mail, id or CSV) to one tier, creating any that do not exist yet."""
    people = {}
    for email in args.email or []:
        people.setdefault(email.strip().lower(), "")
    invalid = []
    if args.csv:
        csv_people, invalid = read_csv_people(args.csv)
        for email, name in csv_people:
            people.setdefault(email, name)
    bad = [e for e in people if len(e) > 254 or not EMAIL_RE.fullmatch(e)]
    if bad:
        raise ValueError(f"Invalid email address: {bad[0][:80]!r}")
    ids = list(dict.fromkeys(args.user_id or []))
    if not people and not ids:
        raise ValueError("Provide --email, --user-id and/or --csv")
    if len(people) + len(ids) > MAX_BULK_USERS:
        raise ValueError(f"At most {MAX_BULK_USERS} users can be added at once")
    if invalid:
        print(f"Skipped {len(invalid)} invalid CSV row(s): {', '.join(i[:40] for i in invalid[:5])}"
              + (" ..." if len(invalid) > 5 else ""))

    with get_conn() as conn:
        with conn.cursor() as cur:
            validate_tier(cur, args.group)
            if ids:
                cur.execute('SELECT id FROM "user" WHERE id = ANY(%s)', (ids,))
                if len(cur.fetchall()) != len(ids):
                    raise ValueError("One or more --user-id values were not found")
            existing = {}
            if people:
                cur.execute('SELECT LOWER(email), id FROM "user" WHERE LOWER(email) = ANY(%s)', (list(people),))
                existing = dict(cur.fetchall())
            missing = [(e, (n or e.split("@")[0]).strip()) for e, n in people.items() if e not in existing]
            if args.dry_run:
                print(f"DRY RUN for {args.group}: {len(existing) + len(ids)} existing user(s) would be added/moved, "
                      f"{len(missing)} new user(s) would be created:")
                for email, _ in missing:
                    print(f"    + {email}")
                conn.rollback()
                return
            created = create_users(cur, missing) if missing else []
            user_ids = list(dict.fromkeys(ids + list(existing.values()) + created))
            result = place_users(cur, user_ids, args.group)

    audit("add_users", tier=args.group, users=len(user_ids), created=[e for e, _ in missing][:50], **result)
    print(
        f"Added {len(user_ids)} user(s) to {args.group}: {len(created)} created, "
        f"{result['moved_from_other_tiers']} switched default tier, {result['already_in_tier']} already in tier."
    )


def set_user_limit(args):
    """Per-user limit override. Stored in user_limit_overrides; tier membership is untouched."""
    daily_tokens, monthly_tokens = resolve_limits(args)

    with get_conn() as conn:
        with conn.cursor() as cur:
            user_id = resolve_user_id(cur, args)
            cur.execute(
                """
                INSERT INTO user_limit_overrides (
                  user_id, daily_token_limit, monthly_token_limit, notes, updated_by, updated_at
                )
                VALUES (%(user_id)s, %(daily)s, %(monthly)s, %(notes)s, %(by)s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                  daily_token_limit = COALESCE(%(daily)s, user_limit_overrides.daily_token_limit),
                  monthly_token_limit = COALESCE(%(monthly)s, user_limit_overrides.monthly_token_limit),
                  notes = COALESCE(%(notes)s, user_limit_overrides.notes),
                  updated_by = %(by)s,
                  updated_at = NOW()
                """,
                {
                    "user_id": user_id,
                    "daily": daily_tokens,
                    "monthly": monthly_tokens,
                    "notes": args.description,
                    "by": args.assigned_by,
                },
            )

    audit("set_override", user_id=user_id, daily=daily_tokens, monthly=monthly_tokens)
    print(
        f"Set individual limit override for {user_id}: daily={daily_tokens}, monthly={monthly_tokens} "
        "(NULL = inherit the tier limit)"
    )


def clear_user_limit(args):
    with get_conn() as conn:
        with conn.cursor() as cur:
            user_id = resolve_user_id(cur, args)
            cur.execute("DELETE FROM user_limit_overrides WHERE user_id = %s", (user_id,))
            removed = cur.rowcount
    if removed:
        audit("clear_override", user_id=user_id)
    print(f"Removed override for {user_id}." if removed else f"No override existed for {user_id}.")


def show_user(args):
    with get_conn() as conn:
        with conn.cursor() as cur:
            user_id = resolve_user_id(cur, args)
            cur.execute("SELECT * FROM get_user_usage_status(%s)", (user_id,))
            row = cur.fetchone()
            cur.execute(
                """
                SELECT g.name, gm.created_at
                FROM group_member gm JOIN "group" g ON g.id = gm.group_id
                WHERE gm.user_id = %s AND g.name = ANY(%s)
                ORDER BY gm.created_at DESC
                """,
                (user_id, tier_names(cur)),
            )
            memberships = cur.fetchall()

    if not row:
        print("No status returned")
        return

    print(status_dict(row))
    print(
        "Tier group memberships (newest default tier wins; custom tiers add up): "
        + (", ".join(f"{n} (added {datetime.fromtimestamp(ts).isoformat() if ts else '?'})" for n, ts in memberships) or "none (default tier applies)")
    )
    if len([n for n, _ in memberships if n in BUILTIN_TIERS]) > 1:
        print(f"WARNING: user is in several default tier groups. Only one default tier counts; custom tiers add up. Use assign-user/remove-user to tidy.")


def list_limits(args):
    with get_conn() as conn:
        with conn.cursor() as cur:
            tiers = tier_names(cur)
            cur.execute(
                """
                SELECT group_name, daily_token_limit, monthly_token_limit, description, updated_at
                FROM usage_limits
                WHERE group_name = ANY(%s)
                ORDER BY array_position(%s::text[], group_name::text) NULLS LAST, group_name
                """,
                (tiers, tiers),
            )
            rows = cur.fetchall()
            cur.execute(
                """
                SELECT o.user_id, COALESCE(u.email, u.name, o.user_id), o.daily_token_limit,
                       o.monthly_token_limit, o.notes
                FROM user_limit_overrides o LEFT JOIN "user" u ON u.id = o.user_id
                ORDER BY 2
                """
            )
            overrides = cur.fetchall()

    for row in rows:
        print({
            "group_name": row[0],
            "daily_token_limit": row[1],
            "monthly_token_limit": row[2],
            "description": row[3] or default_description(row[1], row[2]),
            "updated_at": row[4].isoformat() if row[4] else None,
        })
    if overrides:
        print("\nPer-user overrides (NULL = inherits tier limit):")
        for uid, label, daily, monthly, notes in overrides:
            print({"user": label, "user_id": uid, "daily": daily, "monthly": monthly, "notes": notes})


def fetch_all_user_statuses(cur):
    """(display_name, status dict) for every Open WebUI user, straight from the filter's SQL function."""
    cur.execute('SELECT id, COALESCE(name, email, id) FROM "user"')
    results = []
    for u_id, display_name in cur.fetchall():
        cur.execute("SELECT * FROM get_user_usage_status(%s)", (u_id,))
        row = cur.fetchone()
        if row:
            results.append((u_id, display_name, status_dict(row)))
    return results


def list_members(args):
    with get_conn() as conn:
        with conn.cursor() as cur:
            all_tiers = tier_names(cur)
            tiers = [args.group] if args.group else all_tiers
            if args.group:
                validate_tier(cur, args.group)

            cur.execute(
                """
                SELECT group_name, daily_token_limit, monthly_token_limit
                FROM usage_limits
                WHERE group_name = ANY(%s)
                """,
                (tiers,),
            )
            group_rows = cur.fetchall()
            if args.group and not group_rows:
                raise ValueError(f"Tier {args.group!r} not found in usage_limits")

            users_by_group = {}
            for _, display_name, details in fetch_all_user_statuses(cur):
                # group_name lists every tier the user is in, so they appear under each one.
                for tier_name in details["group_name"].split(", "):
                    users_by_group.setdefault(tier_name, []).append(
                        {"display_name": display_name, "details": details}
                    )

            groups = [
                {
                    "group_name": row[0],
                    "daily_token_limit": row[1],
                    "monthly_token_limit": row[2],
                }
                for row in group_rows
            ]
            groups.sort(key=lambda g: all_tiers.index(g["group_name"]))

            for i, group in enumerate(groups):
                if i > 0:
                    print()
                daily_limit_text = "unlimited" if group["daily_token_limit"] < 0 else f"{group['daily_token_limit']:,}"
                monthly_limit_text = "unlimited" if group["monthly_token_limit"] < 0 else f"{group['monthly_token_limit']:,}"
                print(f"{group['group_name']} (daily: {daily_limit_text}, monthly: {monthly_limit_text})")

                members = users_by_group.get(group["group_name"], [])
                members.sort(key=lambda m: m["display_name"].casefold())

                for member in members:
                    if args.details:
                        print(f"    {member['display_name']}: {member['details']}")
                    else:
                        suffix = " [override]" if member["details"]["limit_source"] == "override" else ""
                        suffix += " [default tier]" if member["details"]["limit_source"] == "default" else ""
                        print(f"    {member['display_name']}{suffix}")


def audit_tiers(args):
    """Report users in more than one built-in (default) tier group or in no tier group at all (default tier applies)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, COALESCE(u.email, u.name, u.id),
                       ARRAY_AGG(g.name ORDER BY gm.created_at DESC)
                FROM "user" u
                LEFT JOIN group_member gm ON gm.user_id = u.id
                LEFT JOIN "group" g ON g.id = gm.group_id AND g.name = ANY(%s)
                GROUP BY u.id, 2
                """,
                (tier_names(cur),),
            )
            rows = cur.fetchall()

    default_of = lambda groups: [g for g in groups if g in BUILTIN_TIERS]
    multi = [(label, default_of(groups)) for _, label, groups in rows if len(default_of(groups)) > 1]
    none = [label for _, label, groups in rows if not default_of(groups)]

    print(f"Users in more than one DEFAULT tier group ({len(multi)}) - newest membership wins:")
    for label, groups in sorted(multi):
        print(f"    {label}: {' > '.join(groups)}")
    print(f"\nUsers in no default tier group ({len(none)}) - default tier {DEFAULT_TIER} applies:")
    for label in sorted(none):
        print(f"    {label}")
    if multi:
        print("\nFix with: assign-user --email <email> --group <tier>")


def assign_default_tier(args):
    """Bulk-assign users to a default (built-in) tier. Select users with --all (everyone in no default
    tier group), --email (repeatable) and/or --email-file (one e-mail per line)."""
    tier = args.group or DEFAULT_TIER
    if tier not in BUILTIN_TIERS:
        raise ValueError(f"{tier!r} is not a default tier. Choose one of: {', '.join(BUILTIN_TIERS)}")
    emails = list(args.email or [])
    if args.email_file:
        emails += [ln.strip() for ln in Path(args.email_file).read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]
    if not args.all and not emails:
        raise ValueError("No users selected: use --all, --email, or --email-file")

    with get_conn() as conn:
        with conn.cursor() as cur:
            validate_tier(cur, tier)
            selected = {}
            if args.all:
                cur.execute(
                    """SELECT u.id, COALESCE(u.email, u.name, u.id) FROM "user" u
                       WHERE NOT EXISTS (
                           SELECT 1 FROM group_member gm JOIN "group" g ON g.id = gm.group_id
                           WHERE gm.user_id = u.id AND g.name = ANY(%s))""",
                    (BUILTIN_TIERS,),
                )
                selected.update(cur.fetchall())
            unknown = []
            for email in emails:
                cur.execute('SELECT id, email FROM "user" WHERE LOWER(email) = LOWER(%s)', (email,))
                row = cur.fetchone()
                if row:
                    selected[row[0]] = row[1]
                else:
                    unknown.append(email)

            print(f"{len(selected)} user(s) selected for {tier}.")
            for email in unknown:
                print(f"    not found, skipped: {email}")
            if not selected:
                raise ValueError("No users selected: nothing matched")
            if args.dry_run:
                for label in sorted(selected.values()):
                    print(f"    would assign: {label}")
                print("Dry run only; nothing changed.")
                return
            if not args.yes and input(f"Assign them to {tier}? Any other default tier is replaced. [y/N] ").strip().lower() != "y":
                raise ValueError("Cancelled")
            result = place_users(cur, list(selected), tier)

    audit("bulk_assign_default_tier", tier=tier, users=len(selected), **result)
    print(f"Assigned {len(selected)} user(s) to {tier} ({result['already_in_tier']} were already in it).")


# ANSI color codes used to highlight quota usage percentages in usage_report.
ANSI_RED = "\033[31m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_GREEN = "\033[32m"
ANSI_RESET = "\033[0m"


def usage_percent(used, limit):
    """Return percent of quota consumed, or None when the quota is unlimited (-1)."""
    if limit is None or limit < 0:
        return None
    if limit == 0:
        return 0.0
    return (used / limit) * 100


def color_for_percent(percent):
    if percent is None:
        return ANSI_GREEN
    if percent > 90:
        return ANSI_RED
    if percent > 75:
        return ANSI_YELLOW
    if percent > 50:
        return ANSI_BLUE
    return ANSI_GREEN


def format_percent(percent, width=0, is_sort_driver=False):
    text = "unlimited" if percent is None else f"{percent:.1f}%"
    # Mark whichever of daily/monthly percent determined this row's sort position.
    if is_sort_driver:
        text += "*"
    color = color_for_percent(percent)
    padded = f"{text:<{width}}" if width else text
    return f"{color}{padded}{ANSI_RESET}"


# Sortable percent: unlimited quotas count as 0% since they can never be "over".
def sortable_percent(percent):
    return percent if percent is not None else 0.0


def parse_date_token(token):
    """Parse a YYYY, YYYY-MM, or YYYY-MM-DD string into (granularity, start_date, end_date)."""
    parts = token.split("-")
    if len(parts) == 3:
        try:
            day = datetime.strptime(token, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid day value {token!r}; expected YYYY-MM-DD") from exc
        return "day", day, day

    if len(parts) == 2:
        try:
            year, month = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ValueError(f"Invalid month value {token!r}; expected YYYY-MM") from exc
        if not 1 <= month <= 12:
            raise ValueError(f"Invalid month value {token!r}: month must be between 01 and 12")
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        return "month", start, end

    if len(parts) == 1:
        try:
            year = int(parts[0])
        except ValueError as exc:
            raise ValueError(f"Invalid year value {token!r}; expected YYYY") from exc
        return "year", date(year, 1, 1), date(year, 12, 31)

    raise ValueError(f"Unrecognized date value {token!r}; expected YYYY, YYYY-MM, or YYYY-MM-DD")


def resolve_report_range(args):
    """Return (granularity, start_date, end_date, is_default) for the requested reporting period."""
    if args.start or args.end:
        if not (args.start and args.end):
            raise ValueError("--start and --end must be provided together")
        start_granularity, start_date, _ = parse_date_token(args.start)
        end_granularity, _, end_date = parse_date_token(args.end)
        if start_granularity != end_granularity:
            raise ValueError(
                f"--start ({args.start}) and --end ({args.end}) must be the same kind of value "
                "(both a day, both a month, or both a year)"
            )
        if end_date < start_date:
            raise ValueError("--end must not be before --start")
        return start_granularity, start_date, end_date, False

    if args.date:
        granularity, start_date, end_date = parse_date_token(args.date)
        return granularity, start_date, end_date, False

    today = datetime.now(USAGE_TZ).date()
    return "day", today, today, True


def enumerate_days(start, end):
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def enumerate_months(start, end):
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return months


def generated_at_label():
    return datetime.now(USAGE_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def build_report_title(granularity, start, end, is_default, mode):
    if is_default:
        title = f"Current Usage Report for timeframe today, {start.isoformat()}, for month {start.strftime('%Y-%m')}."
    elif mode == "single_day":
        title = f"Daily Usage Report for timeframe {start.isoformat()}, for month {start.strftime('%Y-%m')}."
    elif mode == "daily_range":
        title = f"Daily Usage Report for timeframe {start.isoformat()} through {end.isoformat()}."
    elif mode == "annual":
        if start.year == end.year:
            title = f"Annual Usage Report for timeframe {start.year}."
        else:
            title = f"Annual Usage Report for timeframe {start.year} through {end.year}."
    else:
        months = enumerate_months(start, end)
        if len(months) == 1:
            year, month = months[0]
            title = f"Monthly Usage Report for timeframe {year:04d}-{month:02d}."
        else:
            (y1, m1), (y2, m2) = months[0], months[-1]
            title = f"Monthly Usage Report for timeframe {y1:04d}-{m1:02d} through {y2:04d}-{m2:02d}."

    return f"{title} Generated {generated_at_label()}"


def fetch_user_quotas(cur):
    """Current effective quota for every OpenWebUI user. Quotas are not versioned historically."""
    cur.execute('SELECT id, COALESCE(name, email, id) FROM "user"')
    quotas = {}
    for u_id, display_name in cur.fetchall():
        cur.execute("SELECT * FROM get_user_usage_status(%s)", (u_id,))
        row = cur.fetchone()
        quotas[u_id] = {
            "display_name": display_name,
            "daily_limit": row[1] if row else -1,
            "monthly_limit": row[2] if row else -1,
        }
    return quotas


def fetch_usage_for_day(cur, day):
    cur.execute(
        "SELECT user_id, SUM(total_tokens) FROM usage_records WHERE (recorded_at AT TIME ZONE %s)::date = %s GROUP BY user_id",
        (USAGE_TIMEZONE_NAME, day),
    )
    return dict(cur.fetchall())


def fetch_usage_for_month(cur, year, month):
    month_start = date(year, month, 1)
    month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    cur.execute(
        "SELECT user_id, SUM(total_tokens) FROM usage_records WHERE (recorded_at AT TIME ZONE %s)::date >= %s AND (recorded_at AT TIME ZONE %s)::date < %s GROUP BY user_id",
        (USAGE_TIMEZONE_NAME, month_start, USAGE_TIMEZONE_NAME, month_end),
    )
    return dict(cur.fetchall())


def build_block_entries(quotas, month_usage, day_usage=None):
    entries = []
    for user_id, info in quotas.items():
        monthly_used = month_usage.get(user_id, 0)
        entry = {
            "display_name": info["display_name"],
            "monthly_limit": info["monthly_limit"],
            "tokens_month": monthly_used,
            "monthly_pct": usage_percent(monthly_used, info["monthly_limit"]),
        }
        if day_usage is not None:
            daily_used = day_usage.get(user_id, 0)
            entry.update({
                "daily_limit": info["daily_limit"],
                "tokens_today": daily_used,
                "daily_pct": usage_percent(daily_used, info["daily_limit"]),
            })
        entries.append(entry)
    return entries


def filter_and_sort_entries(entries, has_daily, args):
    if not args.show_all_users:
        if has_daily:
            entries = [e for e in entries if e["tokens_today"] > 0 or e["tokens_month"] > 0]
        else:
            entries = [e for e in entries if e["tokens_month"] > 0]

    threshold = args.percentage
    if threshold is not None:
        if args.quota == "daily":
            entries = [e for e in entries if has_daily and sortable_percent(e["daily_pct"]) > threshold]
        elif args.quota == "monthly":
            entries = [e for e in entries if sortable_percent(e["monthly_pct"]) > threshold]
        elif has_daily:
            entries = [
                e for e in entries
                if sortable_percent(e["daily_pct"]) > threshold or sortable_percent(e["monthly_pct"]) > threshold
            ]
        else:
            entries = [e for e in entries if sortable_percent(e["monthly_pct"]) > threshold]

    if has_daily:
        for e in entries:
            e["max_pct"] = max(sortable_percent(e["daily_pct"]), sortable_percent(e["monthly_pct"]))
            e["sum_pct"] = sortable_percent(e["daily_pct"]) + sortable_percent(e["monthly_pct"])
        entries.sort(key=lambda e: (-e["max_pct"], -e["sum_pct"], e["display_name"].casefold()))
    else:
        entries.sort(key=lambda e: (-sortable_percent(e["monthly_pct"]), e["display_name"].casefold()))

    return entries


def print_block(entries, has_daily, show_sort_column, day_label, month_label):
    # Date labels are aligned to sit above the Daily/Monthly Usage columns they describe.
    if has_daily:
        label_line = (" " * 31 + day_label).ljust(71) + month_label
    else:
        label_line = " " * 31 + month_label
    print(label_line)

    header = f"{'Display Name':<30}"
    if has_daily:
        header += f" {'Daily Usage/Quota':<28} {'Daily %':<10}"
    header += f" {'Monthly Usage/Quota':<30} {'Monthly %':<10}"
    if show_sort_column:
        header += f" {'Sort %':<10}"
    print(header)
    print("-" * len(header))

    for e in entries:
        monthly_limit_text = "unlimited" if e["monthly_limit"] < 0 else f"{e['monthly_limit']:,}"
        monthly_usage_text = f"{e['tokens_month']:,} / {monthly_limit_text}"

        if has_daily:
            daily_limit_text = "unlimited" if e["daily_limit"] < 0 else f"{e['daily_limit']:,}"
            daily_usage_text = f"{e['tokens_today']:,} / {daily_limit_text}"
            # Sort is driven by whichever percent is higher; break ties by marking daily.
            daily_is_driver = sortable_percent(e["daily_pct"]) >= sortable_percent(e["monthly_pct"])
            line = (
                f"{e['display_name']:<30} "
                f"{daily_usage_text:<28} "
                f"{format_percent(e['daily_pct'], 9, daily_is_driver)} "
                f"{monthly_usage_text:<30} "
                f"{format_percent(e['monthly_pct'], 9, not daily_is_driver)}"
            )
            if show_sort_column:
                line += f" {format_percent(e['max_pct'], 9)}"
        else:
            line = f"{e['display_name']:<30} {monthly_usage_text:<30} {format_percent(e['monthly_pct'], 9)}"

        print(line)

    # The block's primary timeframe is the day for daily blocks, the month otherwise.
    total_tokens = sum(e["tokens_today"] if has_daily else e["tokens_month"] for e in entries)
    footer = f"\n{len(entries)} user(s) shown."
    if show_sort_column:
        footer += " (* marks the percentage used for sorting)"
    footer += f"\n{total_tokens:,} tokens used."
    print(footer)


def usage_report(args):
    granularity, start, end, is_default = resolve_report_range(args)
    total_days = (end - start).days + 1

    if total_days == 1:
        mode = "single_day"
        blocks = [{"day": start, "month": (start.year, start.month)}]
    elif args.show_daily:
        mode = "daily_range"
        blocks = [{"day": d, "month": (d.year, d.month)} for d in enumerate_days(start, end)]
    else:
        mode = "annual" if granularity == "year" else "monthly_range"
        blocks = [{"month": ym} for ym in enumerate_months(start, end)]

    print(build_report_title(granularity, start, end, is_default, mode))
    print()

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Per-user quotas aren't needed for --summary, which only reports timeframe totals.
            quotas = {} if args.summary else fetch_user_quotas(cur)

            month_cache = {}

            def month_usage(year, month):
                if (year, month) not in month_cache:
                    month_cache[(year, month)] = fetch_usage_for_month(cur, year, month)
                return month_cache[(year, month)]

            blocks_printed = 0
            grand_total = 0
            summary_unit = "day"
            for block in blocks:
                has_daily = "day" in block
                summary_unit = "day" if has_daily else "month"
                year, month = block["month"]
                monthly_usage = month_usage(year, month)
                month_label = f"{year:04d}-{month:02d}"

                if has_daily:
                    daily_usage = fetch_usage_for_day(cur, block["day"])
                    day_label = block["day"].isoformat()
                else:
                    daily_usage = None
                    day_label = None

                if args.summary:
                    block_total = sum((daily_usage if has_daily else monthly_usage).values())
                    if not args.show_all_dates and block_total == 0:
                        continue
                    print(f"{day_label or month_label:<12} {block_total:,} tokens used")
                    grand_total += block_total
                    blocks_printed += 1
                    continue

                entries = build_block_entries(quotas, monthly_usage, daily_usage)

                # Skip a block once all its own filters (percentage/quota/show-all-users) are
                # applied and nothing is left to display, rather than judging on raw totals.
                entries = filter_and_sort_entries(entries, has_daily, args)
                if not args.show_all_dates and not entries:
                    continue

                show_sort_column = has_daily and args.quota is None

                if blocks_printed > 0:
                    print()
                print_block(entries, has_daily, show_sort_column, day_label, month_label)
                blocks_printed += 1

            if args.summary:
                average_total = grand_total / blocks_printed if blocks_printed else 0
                print()
                print(f"Total tokens used across {blocks_printed} {summary_unit}s: {grand_total:,}")
                print(f"Average tokens used per {summary_unit} : {average_total:,.0f}")


def add_limit_args(parser):
    parser.add_argument("--daily-tokens", type=int, help="Daily token limit (absolute count)")
    parser.add_argument("--monthly-tokens", type=int, help="Monthly token limit (absolute count)")
    parser.add_argument("--daily-usd", type=float, help="Daily limit expressed in USD (requires --blended-usd-per-1m)")
    parser.add_argument("--monthly-usd", type=float, help="Monthly limit expressed in USD (requires --blended-usd-per-1m)")
    parser.add_argument(
        "--blended-usd-per-1m",
        type=float,
        help="Blended USD cost per 1M tokens. Required when using --daily-usd or --monthly-usd",
    )
    parser.add_argument("--description", help="Optional free-text description stored on the usage_limits row")


def add_user_args(parser):
    parser.add_argument("--user-id", help="OpenWebUI user UUID")
    parser.add_argument("--email", help="OpenWebUI user email (alternative to --user-id)")


DEPENDENCIES_HELP = """
Dependencies:
  Required (must be installed for any command to run):
    - Python 3.9+
    - psycopg[binary]   pip install "psycopg[binary]"
    - boto3             pip install boto3   (only needed when USAGE_DB_DSN is not set)

Database connection resolution order:
  1. USAGE_DB_DSN         Explicit override, e.g. postgresql://user:pass@host:5432/db
  2. AWS_SECRET_ID        Explicit Secrets Manager secret name override
  3. APP_ENV              Environment label; maps to AWS_SECRET_ID_<APP_ENV>
  Optional:
    AWS_SECRET_DSN_KEY    Key inside the secret JSON holding the DSN (default: DATABASE_URL)
    AWS_SECRET_ID_<APP_ENV>  Secret name used when AWS_SECRET_ID is unset
    USAGE_TIMEZONE        IANA timezone; must match the tracking plugin (default: UTC)
    USAGE_TIER_PREFIX     Tier group prefix (default: token-limit-)
    USAGE_BUILTIN_TIERS   Comma-separated built-in group names, in tie-break order
    USAGE_DEFAULT_TIER    Built-in group used as the default (default: token-limit-standard)
    USAGE_DEFAULT_DAILY_TOKENS    Fallback daily quota (default: 500000)
    USAGE_DEFAULT_MONTHLY_TOKENS  Fallback monthly quota (default: 5000000)

Commands (see "<command> --help" for command-specific options):
  list-limits       List tier token limits and any per-user overrides
  list-members      List tiers and their member users (native group membership)
  set-group-limit   Update the daily/monthly token limit of a tier
  set-all-limits    Bulk-update the daily/monthly token limit for every tier (custom tiers included)
  assign-user       Add a user to a tier (a built-in tier replaces their default tier; custom tiers add up)
  remove-user       Remove a user from one tier (other tiers are kept)
  create-tier       Create a custom tier (<configured-prefix><name> group + limits)
  delete-tier       Delete a custom tier; members return to the configured default tier
  add-users         Bulk-add users to a tier by e-mail/id/CSV; creates users that do not exist
  set-user-limit    Set a per-user limit override (tier membership unchanged)
  clear-user-limit  Remove a per-user limit override
  show-user         Show a user's tier, limits, usage, and tier-group memberships
  audit-tiers       Find users in several tier groups or none
  assign-default-tier  Bulk-assign users (e.g. all with no default tier) to a default tier
  usage-report      Show a color-coded report of daily/monthly usage vs. quota for all users
"""


def main():
    help_requested = any(argument in {"-h", "--help"} for argument in sys.argv[1:])
    epilog = DEPENDENCIES_HELP
    if help_requested:
        epilog += f"\nConnection status:\n  {get_connection_resolution_status()}\n"

    parser = argparse.ArgumentParser(
        description="Admin CLI for OpenWebUI usage tracking filter",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-limits", help="List tier token limits and per-user overrides")
    p.set_defaults(func=list_limits)

    p = sub.add_parser("list-members", help="List tiers and their member users")
    p.add_argument("--group", help="Filter to one tier (default: all tiers)")
    p.add_argument(
        "--details",
        action="store_true",
        help="Include detailed usage information for each member (same details as show-user)",
    )
    p.set_defaults(func=list_members)

    p = sub.add_parser("set-group-limit", help="Update the token limit of a tier (built-in or custom)")
    p.add_argument("--group", required=True, help=f"Tier to update (e.g. {TIER_PREFIX}pro)")
    add_limit_args(p)
    p.set_defaults(func=set_group_limit)

    p = sub.add_parser(
        "set-all-limits",
        help="Bulk-update the token limit for every tier, including custom tiers",
    )
    add_limit_args(p)
    p.set_defaults(func=set_all_limits)

    p = sub.add_parser(
        "assign-user",
        help="Add a user to a tier (built-in replaces the default tier; custom is added to their others)",
    )
    add_user_args(p)
    p.add_argument("--group", required=True, help="Tier to assign the user to")
    p.set_defaults(func=assign_user)

    p = sub.add_parser("remove-user", help="Remove a user from one tier (their other tiers are kept)")
    add_user_args(p)
    p.add_argument("--group", required=True, help="Tier to remove the user from")
    p.set_defaults(func=remove_user)

    p = sub.add_parser(
        "create-tier",
        help=(
            f"Create a custom tier ({TIER_PREFIX}<name> group + limits). "
            f"Defaults: {DEFAULT_DAILY:,} daily / {DEFAULT_MONTHLY:,} monthly"
        ),
    )
    p.add_argument("--group", required=True, help=f"Tier name; stored as {TIER_PREFIX}<name> (letters, numbers, hyphens)")
    add_limit_args(p)
    p.set_defaults(func=create_tier)

    p = sub.add_parser(
        "delete-tier",
        help=f"Delete a custom tier; members in no other tier return to {DEFAULT_TIER}",
    )
    p.add_argument("--group", required=True, help="Custom tier to delete (built-in tiers cannot be deleted)")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    p.set_defaults(func=delete_tier)

    p = sub.add_parser(
        "add-users",
        help="Bulk-add users to a tier (by --email, --user-id and/or --csv); missing users are created",
    )
    p.add_argument("--group", required=True, help="Tier to add the users to (built-in or custom)")
    p.add_argument("--email", action="append", help="User e-mail; repeat for several")
    p.add_argument("--user-id", action="append", help="Open WebUI user UUID; repeat for several")
    p.add_argument(
        "--csv",
        help="CSV file in any layout, header row optional; the column with email addresses is detected automatically, other columns are ignored",
    )
    p.add_argument("--dry-run", action="store_true", help="Show what would happen without changing anything")
    p.set_defaults(func=add_users)

    p = sub.add_parser(
        "import-tiers",
        help="Create several custom tiers and their members from one CSV "
             "(group-name,email,daily-token-limit,monthly-token-limit,description)",
    )
    p.add_argument("--csv", required=True, help="CSV, header row optional (then columns are group-name,email,daily,monthly,description); rows are grouped by group-name")
    p.add_argument("--dry-run", action="store_true", help="Show the summary only; change nothing")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    p.set_defaults(func=import_tiers)

    p = sub.add_parser(
        "set-user-limit",
        help="Set a per-user limit override (tier membership unchanged)",
    )
    add_user_args(p)
    p.add_argument("--assigned-by", default="admin-cli", help="Value stored in updated_by (default: admin-cli)")
    add_limit_args(p)
    p.set_defaults(func=set_user_limit)

    p = sub.add_parser("clear-user-limit", help="Remove a per-user limit override")
    add_user_args(p)
    p.set_defaults(func=clear_user_limit)

    p = sub.add_parser("show-user", help="Show a user's tier, limits, usage, and tier memberships")
    add_user_args(p)
    p.set_defaults(func=show_user)

    p = sub.add_parser("audit-tiers", help="Find users in several tier groups or in none")
    p.set_defaults(func=audit_tiers)

    p = sub.add_parser(
        "assign-default-tier",
        help="Bulk-assign users to a default tier (replaces any other default tier they have)",
    )
    p.add_argument("--group", help=f"Default tier to assign (default: {DEFAULT_TIER})")
    p.add_argument("--all", action="store_true", help="Select every user who is in no default tier group")
    p.add_argument("--email", action="append", help="Select a user by e-mail (repeatable)")
    p.add_argument("--email-file", help="Select users listed in a text file, one e-mail per line")
    p.add_argument("--dry-run", action="store_true", help="Show who would be assigned without changing anything")
    p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    p.set_defaults(func=assign_default_tier)

    p = sub.add_parser(
        "usage-report",
        help="Show a color-coded report of daily/monthly usage vs. quota for all users",
    )
    p.add_argument(
        "--percentage",
        type=float,
        help="Only include users whose usage percentage exceeds this threshold",
    )
    p.add_argument(
        "--quota",
        choices=["daily", "monthly"],
        help="Restrict --percentage filtering to this quota type (default: either daily or monthly)",
    )
    p.add_argument(
        "--date",
        help="Report period: YYYY-MM-DD (day), YYYY-MM (month), or YYYY (year). Default: today/this month",
    )
    p.add_argument(
        "--start",
        help="Start of a date range; must be the same kind of value (day/month/year) as --end",
    )
    p.add_argument(
        "--end",
        help="End of a date range; must be the same kind of value (day/month/year) as --start",
    )
    p.add_argument(
        "--show-daily",
        action="store_true",
        help="For multi-day periods, break the report into one per-day section instead of per-month",
    )
    p.add_argument(
        "--show-all-users",
        action="store_true",
        help="Include users with 0 token usage for the timeframe (default: hidden)",
    )
    p.add_argument(
        "--show-all-dates",
        action="store_true",
        help="Include timeframes with no usage data at all (default: hidden)",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Show a condensed report with only total tokens per timeframe, omitting per-user detail",
    )
    p.set_defaults(func=usage_report)

    args = parser.parse_args()

    try:
        args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
