# PostgreSQL Monitoring for Pandora FMS

> **Version:** 1.1.3  
> **Platform:** Pandora FMS Discovery (Application)  
> **Package:** `pandorafms.postgresql_monitor.disco`  
> **Status:** Community-maintained integration; test on your target versions before production use.

## Overview

A community-developed Pandora FMS Discovery application for remotely monitoring PostgreSQL using built-in health/performance metrics and SQL modules configured through the Discovery wizard.

This repository contains a **Pandora FMS Discovery application**, not a PostgreSQL installation and not a standalone Pandora agent. Monitoring runs remotely from the Pandora Discovery server and generates XML data modules ingested by Pandora FMS.

## Features

- **22 built-in SQL monitoring modules**, organized by group and individually enabled/disabled at group level.
- Up to **10 user-defined SQL modules per Discovery task**, entered via separate UI fields (name, type, unit, group, result mode and multiline query).
- Numeric and text modules, including a **readable multi-row/multi-column table** for Pandora snapshot views.
- Collector self-monitoring (connection status, query/error counters and collection time).
- **At most one database connection per execution**, with sequential SQL execution and per-task local overlap locking.
- Single-statement `SELECT`/`WITH` safety checking, query timeout controls, run logs, and per-task configuration supplied by Pandora.

## How it works

```text
Pandora Console (Discovery task wizard)
                 |
       discovery_definition.ini
                 |
        Python SQL collector
                 |
      1 database connection
                 |
        built-in + custom SQL
                 |
       Pandora XML .data file
                 |
        Pandora Data Server
                 |
        Agent / monitoring modules
```

## Requirements

- **Pandora FMS** with the Discovery Applications feature enabled (developed against the Pandora FMS 8.0NG.805 workflow; other versions not guaranteed).
- **Linux Pandora Discovery server** with `/usr/bin/python3` (the path invoked by this package).
- **Python driver:** `psycopg2` (Psycopg 2).
- **Database/network:** PostgreSQL TCP access (default `5432`), credentials, and a database name. TLS policy can be configured through the PostgreSQL SSL mode field.
- Write permission to Pandora incoming directory (provided as `__incomingDir__`) and to the configured run-log location.
- Linux `flock` support, used to suppress overlapping executions on the **same host/task**.

### Install and verify the driver

`sudo dnf install python3-psycopg2` (when available) or install `psycopg2` into the Python interpreter used by Discovery. For local experimentation, `python3 -m pip install psycopg2-binary` is convenient; review upstream guidance before choosing it for production.

```bash
/usr/bin/python3 -c "import psycopg2; print(psycopg2.__version__)"
```

**Important:** Installing a driver into a virtual environment or `root` user environment does not automatically make it available to the system Python executable `/usr/bin/python3` or the service account running Discovery. Match the interpreter, package location, and filesystem permissions.

## Installation in Pandora FMS

1. Obtain `pandorafms.postgresql_monitor.disco` from this repository or its GitHub Releases.
2. In Pandora FMS Console, open **Management → Discovery → Applications / Manage DISCO packages** (exact menu label may differ by build).
3. Select **Load/Upload DISCO** and upload the `.disco` file. Do **not** extract the archive before uploading.
4. Create a Discovery **Application** task for this extension, select the Discovery server and an execution interval (start with **300 seconds**).
5. Fill in the target host, port, database/service name, monitoring username/password and Pandora agent/group.
6. Enable the desired built-in groups and optionally create custom SQL modules; save the task.
7. Run the task, then check **Discovery Task execution summary**, resulting Pandora agent/modules and collection log.

The `.disco` file is a ZIP-format archive with a `.disco` extension. `discovery_definition.ini` must be located at the **archive root**.

## Configuration

| Setting | Purpose |
|---|---|
| Target host / port | Database endpoint reachable from Pandora Discovery server |
| Database / service | Database to connect to (Oracle uses a **service name**) |
| Monitoring credentials | Dedicated low-privilege database account |
| Agent name and group | Agent grouping in Pandora FMS |
| Built-in groups | Select which predefined metrics to collect |
| Custom SQL modules | Add up to ten custom monitoring modules |
| Result mode | `Auto`, `Table`, or `Scalar` for custom query output |
| Timeouts and overlap protection | Limit query runtime and duplicate task runs |

**PostgreSQL defaults:** database `postgres`, TCP `5432`, and `application_name=PandoraFMS-PostgreSQL-Discovery` for identifying active collector connections.

## Built-in monitoring

The bundled SQL catalog contains **22 modules**. Available monitoring groups: **Basic Info; Connections; Performance; Security & Roles; Transactions; Tuples Statistics**. Examples include:

- uptime, version, databases, active/idle connections, locks, transactions, table/index sizes and tuple statistics.
- `list_database` and `check_index_size` can produce multi-row text tables; use a string module for new custom tabular metrics.

Some built-in metrics require additional privileges or may differ by database edition/version. An individual query error is logged; it does not necessarily indicate a failed network connection.

## Adding custom SQL modules

In the Discovery task wizard, open **Custom SQL modules**, enable the feature, and enter a module in the field-based form. Enable the next module slot when needed.

| Field | Example |
|---|---|
| Name | `Active Connections` |
| Datatype | `generic_data` for numeric or `generic_data_string` / `async_string` for text |
| Result mode | `Auto` (single-cell scalar; multiple rows/columns become table), `Table`, or `Scalar` |
| Unit | `connections`, `bytes`, `%`, `ms`, etc. |
| Module group | `Custom SQL` |
| SQL query | Read-only `SELECT` or `WITH` query |

### Numeric module example

```sql
SELECT count(*) FROM pg_stat_activity WHERE state = 'active';
```

### String module example

```sql
SELECT version();
```

### Tabular / snapshot module example

```sql
SELECT pid, datname, usename, state
FROM pg_stat_activity
WHERE backend_type = 'client backend'
ORDER BY backend_start DESC
LIMIT 20;
```

For a multi-row result select a **string datatype** and **Table** mode. Custom SQL text can span multiple lines; each SQL textarea is materialized into its own Pandora temporary file to avoid truncation of multiline queries. Table output is intentionally bounded by a configurable maximum row count and may be truncated for large results.

**Query safety:** This extension applies a conservative read-only SQL syntax filter; this is **not a security boundary**. Always use read-only database permissions and avoid expensive full-table scans in frequent polling.

## Database permissions and security

Create a dedicated role with `LOGIN`, `CONNECT` to the monitored database and only required read privileges. If views hide session/statistics details, grant `pg_read_all_stats` or `pg_monitor` only after evaluating the exposure; neither role is needed for every metric. The collector sets `application_name=PandoraFMS-PostgreSQL-Discovery` by default.

- Restrict access to database port(s) from the Pandora Discovery server only.
- Prefer encrypted and certificate-verified transport when supported; do not store credentials in Git, public logs or screenshots.
- Pandora writes sensitive temporary configuration files during execution. Protect the Pandora host, task permissions and temporary-file directories.
- Monitoring metrics that include session SQL text may expose application literals; review access to Pandora modules and logs.

## Database session usage

The collector uses **one `psycopg2.connect()` and one cursor** for all enabled built-ins and custom queries in a single task execution, then closes the connection. It configures read-only transactions and connection/statement/lock/idle-in-transaction timeouts.

The non-blocking lock prevents **overlapping runs of the same task on one Discovery host**. It is **not a distributed lock**: multiple tasks, different Pandora servers, or external monitoring clients can still create additional database sessions. Tune interval and timeouts according to query cost.

### Inspect collector sessions on the database

```sql
SELECT pid, application_name, state, client_addr
FROM pg_stat_activity
WHERE application_name = 'PandoraFMS-PostgreSQL-Discovery';
```

## Troubleshooting

- **Dependency error:** run the driver verification command above using `/usr/bin/python3` on the selected Discovery server.
- **Connection timeout/refused:** check DNS/IP, TCP port, listener/bind address, firewall, DB authentication, and TLS configuration.
- **Permission denied / missing view:** inspect the failing SQL module and grant only the minimum needed database permissions.
- **`N/A` on a table module:** select `Table` with a text datatype and test with a **new module name**, since Pandora may preserve the existing module type. Also inspect task execution and SQL errors.
- **Query result empty:** confirm the query produces rows under the same DB user and database context.
- **Task unexpectedly skipped:** check whether another run holds the local task lock.

**Default run log:** ``/var/log/pandora-scan/postgresql_discovery.run.log``.

```bash
tail -100 /var/log/pandora-scan/postgresql_discovery.run.log
```

## Source files and packaging

The published `.disco` archive contains:

```text
pandorafms.postgresql_monitor.disco
├── discovery_definition.ini
├── pandorafms_postgresql.py
├── pg_queries_builtin.json
└── README.txt
```

To inspect/rebuild from extracted source files (requires `zip` / `unzip`):

```bash
unzip -l pandorafms.postgresql_monitor.disco
unzip -t pandorafms.postgresql_monitor.disco
# From the directory containing the files above:
zip -j pandorafms.postgresql_monitor.disco discovery_definition.ini pandorafms_postgresql.py pg_queries_builtin.json README.txt
```

Do not zip a containing parent directory; the `discovery_definition.ini` file must be directly inside the archive. You can use 7-Zip with **ZIP** output and rename `.zip` to `.disco` as well.

## Screenshot 
### Discovery
<img width="1717" height="897" alt="image" src="https://github.com/user-attachments/assets/254a526e-3b58-4cbf-bdcb-91923bf671e0" />
<img width="1714" height="852" alt="image" src="https://github.com/user-attachments/assets/95284087-9221-4e41-890e-52758c601f8a" />
<img width="1713" height="854" alt="image" src="https://github.com/user-attachments/assets/9932436b-b040-4ecc-9729-6d791b61f2fd" />
<img width="1710" height="901" alt="image" src="https://github.com/user-attachments/assets/d9d901eb-041d-431b-8e98-03048519a095" />
<img width="1716" height="905" alt="image" src="https://github.com/user-attachments/assets/d0450720-1cad-4014-9aed-964315f097c0" />

### Modules
<img width="1627" height="720" alt="image" src="https://github.com/user-attachments/assets/87d96061-ee89-42c6-a918-20f2a583ab99" />
<img width="1635" height="836" alt="image" src="https://github.com/user-attachments/assets/557ae01c-271a-4454-bbdd-77654e2fe6f9" />
<img width="1636" height="820" alt="image" src="https://github.com/user-attachments/assets/4c77e72f-eeaa-4aff-9463-befbbeefae4f" />

## Compatibility and project status

- **Plugin version:** `1.1.3`.
- Tested at package/parser/syntax level during development; **end-to-end compatibility with every Pandora FMS build or DB engine version is not guaranteed**.
- Monitoring uses database views/statistics and therefore may differ across versions or privilege sets.
- This version fixes multiline SQL from Discovery textareas by materializing each custom query into its **own temporary file**. Existing Pandora modules retain their originally registered data type: test with a new module name when changing numeric versus string output.

## Contributing

Contributions are welcome for additional built-in metrics, query optimization, version compatibility, tests, documentation and safe monitoring use cases. Please include database version, Pandora FMS version, reproduction steps and sanitized logs when filing an issue. Do not submit passwords, private IP inventories or database query results containing sensitive values.

## License and trademarks

**License:** No license is included automatically in this README. The repository owner should add an explicit `LICENSE` file before distributing this project as open-source software. The name Pandora FMS and database/vendor names are trademarks of their respective owners. This is an **unofficial, independently developed** integration, not an official Pandora FMS package. Note that Pandora FMS documentation reserves the `pandorafms.` package `short_name` prefix for official integrations; consider a unique community/vendor prefix before public distribution.

## References

- [Pandora FMS: Discovery plugin/package workflow](https://pandorafms.com/manual/!current/en/documentation/pandorafms/monitoring/17_discovery_2)

- [Pandora FMS: .disco development and discovery_definition.ini](https://pandorafms.com/manual/!current/en/documentation/pandorafms/technical_reference/12_disco_development)

- [Pandora FMS: Data XML interface](https://pandorafms.com/manual/!current/en/documentation/pandorafms/technical_reference/01_development_and_extension)

- [Psycopg 2 installation](https://www.psycopg.org/docs/install.html)

- [PostgreSQL monitoring statistics](https://www.postgresql.org/docs/current/monitoring-stats.html)

- [PostgreSQL predefined monitoring roles](https://www.postgresql.org/docs/current/predefined-roles.html)
