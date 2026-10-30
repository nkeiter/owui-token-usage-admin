# Open WebUI Token Usage Admin

Developed by Nathan I Keiter, Gettysburg College.

`owui_token_usage_admin.py` is an admin CLI for the [Open WebUI](https://github.com/open-webui/open-webui)
usage tracking filter ([owui-token-usage-tracking-plugin](https://github.com/nkeiter/owui-token-usage-tracking-plugin)).
It manages usage tiers (native `token-limit-*`
Open WebUI groups), tier limits, per-user overrides, and usage reports directly
in PostgreSQL.

The web console ([owui-token-usage-management-console](https://github.com/nkeiter/owui-token-usage-management-console)) is the
graphical (GUI) version of this CLI and the preferred interface. This CLI is the fallback when the console is unavailable, and it behaves the same way
(a user has one default tier plus any number of custom tiers, whose limits add up; limits are validated; every change prints an `AUDIT {...}` JSON line to stderr).

## Requirements

- Python 3.9+, `pip install "psycopg[binary]" boto3`
- Network access and database credentials that allow the script to connect
  directly to the PostgreSQL database used by Open WebUI. The script reads and
  writes Open WebUI user/group membership data and the usage tracking tables.
- For best results, run the script from a CLI on the server hosting Open WebUI.
  It can also run from another machine if that machine has the required network
  access to the Open WebUI PostgreSQL database and, when applicable, AWS Secrets
  Manager.
- The [owui-token-usage-tracking-plugin](https://github.com/nkeiter/owui-token-usage-tracking-plugin) must be installed, enabled, and
  properly configured on the Open WebUI instance to use this database. Run at
  least one chat through the enabled filter so it initializes the usage schema
  and built-in tier groups before using this CLI.
- If using AWS Secrets Manager instead of `USAGE_DB_DSN`, AWS credentials,
  region configuration, and permission to read the selected secret.

## Connection

The script connects directly to the Open WebUI PostgreSQL database. Connection
settings are resolved in this order:

1. `USAGE_DB_DSN` - explicit DSN
2. `AWS_SECRET_ID` - explicit AWS Secrets Manager secret name
3. `APP_ENV` - environment label used to look up `AWS_SECRET_ID_<APP_ENV>`.
   For example, `APP_ENV=prod` uses `AWS_SECRET_ID_PROD`.

`AWS_SECRET_DSN_KEY` selects the field containing the DSN in the secret's JSON
value (default: `DATABASE_URL`). The AWS SDK uses its normal credential and
region resolution. Outside AWS, set `USAGE_DB_DSN` to connect directly.

| Variable | Purpose |
| --- | --- |
| `USAGE_DB_DSN` | Direct PostgreSQL DSN; takes precedence over Secrets Manager. |
| `AWS_SECRET_ID` | Explicit Secrets Manager secret name; used when `USAGE_DB_DSN` is unset and takes precedence over the `APP_ENV` mapping. |
| `APP_ENV` | Environment label used to select `AWS_SECRET_ID_<APP_ENV>` when neither `USAGE_DB_DSN` nor `AWS_SECRET_ID` is set. The label is uppercased when forming the variable name. |
| `AWS_SECRET_ID_<APP_ENV>` | Secret name for the selected environment; for example, `AWS_SECRET_ID_PROD` when `APP_ENV=prod`. |
| `AWS_SECRET_DSN_KEY` | JSON key containing the DSN in the selected secret (default: `DATABASE_URL`). |

Example using a local or managed PostgreSQL DSN:

```sh
export USAGE_DB_DSN='postgresql://db-user:password@db.example.org:5432/open_webui'
./owui_token_usage_admin.py list-limits
```

Example using Secrets Manager:

```sh
export APP_ENV=prod
export AWS_SECRET_ID_PROD='your-open-webui-database-secret'
# Set this if the secret stores its DSN under a different JSON key:
export AWS_SECRET_DSN_KEY='DATABASE_URL'
./owui_token_usage_admin.py list-limits
```

### Match the usage-filter configuration

The CLI's tier and timezone settings must match those configured on the
Open WebUI usage tracking filter. This is especially important for usage
reports (which query usage by date) and for creating or deleting tiers. Set
these optional environment variables before running the script:

| Variable | Purpose | Default |
| --- | --- | --- |
| `USAGE_TIMEZONE` | IANA timezone for report dates and timestamps. Match the filter's timezone valve. | `UTC` |
| `USAGE_TIER_PREFIX` | Prefix identifying tier groups. Match the filter's tier prefix. | `token-limit-` |
| `USAGE_BUILTIN_TIERS` | Comma-separated built-in group names, ordered by tie-break priority. Must match the filter's configured tiers. | `<prefix>enterprise,<prefix>pro,<prefix>standard,<prefix>test` |
| `USAGE_DEFAULT_TIER` | Built-in tier used when users have no assigned default tier and when custom-tier members need a fallback. Must be in `USAGE_BUILTIN_TIERS`. | `<prefix>standard` |
| `USAGE_DEFAULT_DAILY_TOKENS` | Fallback daily quota used when creating a custom tier or inserting a missing limit value. | `500000` |
| `USAGE_DEFAULT_MONTHLY_TOKENS` | Fallback monthly quota used when creating a custom tier or inserting a missing limit value. | `5000000` |

If `USAGE_BUILTIN_TIERS` is omitted, its default names are generated from
`USAGE_TIER_PREFIX`. Set `USAGE_BUILTIN_TIERS` explicitly if your deployment
uses different built-in tiers or a different order.

For example, if the filter uses `America/Chicago`, set `USAGE_TIMEZONE` to
`America/Chicago` for this CLI too. Tier group names and limits should likewise
reflect the filter and the target database. The default values are included
for compatibility with the companion plugin's standard configuration, not as
requirements for other deployments.

## Commands

Run `./owui_token_usage_admin.py <command> --help` for options.

| Command | Purpose |
| --- | --- |
| `list-limits` | Tier limits and per-user overrides |
| `list-members [--group T] [--details]` | Tiers and their members |
| `set-group-limit --group T ...` | Change a tier's limits (built-in or custom) |
| `set-all-limits ...` | Change limits on every tier, custom tiers included |
| `assign-user --email E --group T` | Add a user to a tier: a built-in tier replaces their default tier, a custom tier is added to their others |
| `remove-user --email E --group T` | Remove a user from one tier (other tiers kept) |
| `create-tier --group NAME ...` | Create a custom tier `<configured-prefix><name>` (default 500K/5M) |
| `delete-tier --group T [--yes]` | Delete a custom tier; members in no other tier return to the configured default tier |
| `import-tiers --csv FILE [--dry-run] [--yes]` | Create several custom tiers and their members from one CSV (see below) |
| `add-users --group T [--email E]... [--user-id ID]... [--csv FILE] [--dry-run]` | Bulk-add users; missing users are created |
| `set-user-limit`, `clear-user-limit` | Per-user limit override (does not change tier) |
| `show-user` | A user's tier, limits, usage and memberships |
| `audit-tiers` | Users in several default tier groups, or in none |
| `assign-default-tier` | Bulk-assign users to a default tier: `--all` (everyone in no default tier group), `--email` (repeatable) and/or `--email-file`; `--group` picks the tier (defaults to the configured default tier); `--dry-run` previews, `-y` skips the prompt. Replaces any other default tier the users have |
| `usage-report` | Color-coded usage vs. quota report |

Limit options: `--daily-tokens`, `--monthly-tokens` (`-1` = unlimited, max 10^12), or `--daily-usd` /
`--monthly-usd` with `--blended-usd-per-1m`.

## Custom tiers and bulk users

Example files using fake data are available in [`examples/`](examples/):

- [multi-import-example.csv](examples/multi-import-example.csv) is for
  `import-tiers --csv FILE`. It shows how one file can define multiple custom
  tiers, their limits, descriptions, and member email addresses.
- [single-import-example.csv](examples/single-import-example.csv) is for
  `add-users --group T --csv FILE`. It shows a simple list of email addresses
  to add to one existing tier.
- [env-example.txt](examples/env-example.txt) is a reference for the script's
  environment configuration options. Adapt the values to match your Open WebUI
  tracking-filter configuration, then set/export the needed variables in your
  shell or deployment environment before running the script. The script does
  not automatically load this text file.

```bash
./owui_token_usage_admin.py create-tier --group spring-2026 --daily-tokens 200000 --monthly-tokens 2000000
./owui_token_usage_admin.py add-users --group token-limit-spring-2026 --csv students.csv --dry-run
./owui_token_usage_admin.py add-users --group token-limit-spring-2026 --csv students.csv
./owui_token_usage_admin.py delete-tier --group token-limit-spring-2026
```

- Custom tier names are stored as `<configured-prefix><name>` (letters, numbers, single hyphens; the suffix
  must fit within the 50-character group name limit). Built-in tiers cannot be deleted.
- `add-users --csv` accepts any CSV layout: the delimiter and the column holding e-mail addresses are detected
  automatically and other columns are ignored (a `name` column is used if present). A header row is optional (a
  first row whose email cell has no `@` is skipped as a header). Max 1000 users per run.
- `import-tiers --csv FILE` reads `group-name,email,daily-token-limit,monthly-token-limit,description` (one row
  per member; the header row is optional: with one, columns are matched by name in any order, without one they are
  read in this order). Rows are grouped by group name in any order; limits are needed on at least one row per group
  (greatest value wins; `500K`, `5M` and `-1` = unlimited are accepted); the first non-empty description wins,
  otherwise it defaults to the limits, e.g. `500K/day, 5M/month`. It prints a summary (groups, limits, member counts,
  errors, warnings such as duplicate rows) and creates nothing if there is any error. `--dry-run` stops
  after the summary; otherwise it asks you to type `yes` (or pass `--yes`). Existing group names are rejected, as
  are built-in tier names. The CSV is only read locally and no copy is made.
- A tier created without a description gets its limits as the description (e.g. `500K/day, 5M/month`).
- Users that do not exist are created with role `user` and an unusable password; they sign in with SSO, which links
  to the account by e-mail.
- `delete-tier` asks you to retype the tier name unless `--yes` is given.
- Custom tiers are only enforced once the current `usage_tracking_filter.py` is loaded into Open WebUI (the
  console can also refresh the database's tier functions, but an old filter reverts it on restart).

## Notes

- `set-user-limit` writes `user_limit_overrides` only; it never changes group membership. Unlike the console, it
  keeps whichever limit you do not pass.
- Errors print `ERROR: ...` and exit with status 1.

## Related projects

- [owui-token-usage-tracking-plugin](https://github.com/nkeiter/owui-token-usage-tracking-plugin):
  the required Open WebUI filter plugin that creates and enforces the usage
  tables and tiers this CLI manages.
- [owui-token-usage-management-console](https://github.com/nkeiter/owui-token-usage-management-console):
  the graphical (web) version of this CLI, deployed as an AWS Lambda app.
- [Open WebUI](https://github.com/open-webui/open-webui): the application
  these projects extend. Thank you to its authors and contributors.

### Other related work by this author

- [owui-scim-admin](https://github.com/nkeiter/owui-scim-admin): stand-alone
  command-line utility for administering Open WebUI users and groups through
  its SCIM 2.0 API.

## Disclaimer

The software is provided “AS IS,” without warranty of any kind, as stated in
the MIT License. To the fullest extent permitted by applicable law, Gettysburg
College, its trustees, officers, employees, and contributors disclaim liability
for claims, damages, or other liability arising from, out of, or in connection
with the software or its use. Use and deploy the software at your own risk;
operators are responsible for validating its behavior, security, database
permissions, and compliance with their policies. This notice supplements but
does not replace the MIT License. Have counsel review the disclaimer for your
jurisdiction if a specific legal outcome is required.
