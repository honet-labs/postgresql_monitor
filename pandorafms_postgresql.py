#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pandora FMS PostgreSQL Discovery collector.

Design goals:
- One PostgreSQL connection/session per Discovery task execution.
- One cursor reused for every built-in and custom query.
- Non-overlapping runs via flock to prevent session pile-up.
- Read-only PostgreSQL session with statement and lock timeouts.
- Password is read from a temporary Discovery config file, not command line.
- Built-in query catalogue is based on the user's pg_queries.json.
- Custom modules are entered in the Discovery UI; no server-side JSON editing.
"""

import argparse
import datetime
import fcntl
import html
import json
import os
import re
import sys
import time
from pathlib import Path

VERSION = "1.1.3"
BASE_DIR = Path(__file__).resolve().parent
BUILTIN_FILE = BASE_DIR / "pg_queries_builtin.json"
STRING_TYPES = {"generic_data_string", "async_string"}
NUMERIC_TYPES = {"generic_data", "async_data", "generic_proc", "async_proc"}

GROUP_FLAGS = {
    "Basic Info": "basic_info",
    "Connections": "connections",
    "Performance": "performance",
    "Security & Roles": "security",
    "Transactions": "transactions",
    "Tuples Statistics": "tuples",
}


def parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def safe_int(value, default=0, minimum=None, maximum=None):
    try:
        result = int(float(value))
    except Exception:
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def xml_escape(value):
    return html.escape("" if value is None else str(value), quote=True)


def xml_text(value):
    """Remove characters forbidden by XML 1.0 while preserving tabs/newlines."""
    text = "" if value is None else str(value)
    return "".join(
        ch for ch in text
        if ch in "\t\n\r" or 0x20 <= ord(ch) <= 0xD7FF or 0xE000 <= ord(ch) <= 0xFFFD
    )


def xml_cdata(value):
    # A literal ]]> cannot occur inside a CDATA section; split it safely.
    return xml_text(value).replace("]]>", "]]]]><![CDATA[>")


def clean_name(value, fallback="PostgreSQL"):
    text = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:200] or fallback


def clean_agent(value):
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return text.strip("_") or "PostgreSQL"


def read_kv_config(path):
    cfg = {}
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        cfg[key.strip()] = value.strip()
    return cfg


def ensure_parent(path):
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def log_line(path, level, message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}\n"
    if not path:
        return
    try:
        ensure_parent(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def replace_macros(text, cfg):
    value = str(text or "")
    replacements = {
        "{{DBNAME}}": cfg.get("database", "postgres"),
        "$__self_dbname": cfg.get("database", "postgres"),
        "{{HOST}}": cfg.get("host", ""),
        "{{PORT}}": cfg.get("port", "5432"),
        "{{USER}}": cfg.get("user", ""),
    }
    for old, new in replacements.items():
        value = value.replace(old, str(new))
    return value


def select_only(sql):
    # Remove leading SQL comments before checking the first statement keyword.
    text = str(sql or "").strip()
    text = re.sub(r"\A(?:--[^\n]*\n|/\*.*?\*/\s*)+", "", text, flags=re.S).lstrip()
    return bool(re.match(r"^(SELECT|WITH)\b", text, flags=re.I))


def parse_custom_queries(path):
    """
    Supports two UI-friendly formats.

    Quick one-line format:
      name|datatype|unit|module_group|SELECT ...

    Advanced block format, compatible with Pandora-style custom queries:
      check_begin
      name long_running_queries
      description Active queries older than 60 seconds
      operation value
      datatype generic_data
      unit queries
      module_group Custom SQL
      min_warning 1
      min_critical 5
      target SELECT count(*) FROM pg_stat_activity ...;
      check_end
    """
    items = []
    if not path or not Path(path).exists():
        return items
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    block = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.lower() == "check_begin":
            block = {}
            continue
        if line.lower() == "check_end":
            if block:
                if "sql" in block and "target" not in block:
                    block["target"] = block["sql"]
                items.append(block)
            block = None
            continue
        if block is not None:
            parts = line.split(None, 1)
            if len(parts) == 2:
                block[parts[0].strip().lower()] = parts[1].strip()
            continue
        # Quick format, preserve any further | in SQL with maxsplit=4.
        if "|" in line:
            parts = line.split("|", 4)
            if len(parts) == 5:
                name, datatype, unit, group, sql = [p.strip() for p in parts]
                items.append({
                    "name": name,
                    "datatype": datatype or "generic_data",
                    "unit": unit,
                    "module_group": group or "Custom SQL",
                    "target": sql,
                    "operation": "value",
                })
    return items


def parse_custom_slots(cfg, query_files=None, max_slots=10):
    """Build UI modules, reading each SQL textarea from its own temp file.

    A textarea can contain newlines or '='. It MUST NOT be embedded inside the
    ordinary key=value Discovery config, otherwise only the first SQL line is
    kept by the config reader (the v1.1.2 custom module regression).
    """
    query_files = query_files or {}
    items = []
    if not parse_bool(cfg.get("custom_enabled"), False):
        return items
    for i in range(1, max_slots + 1):
        if not parse_bool(cfg.get(f"custom{i}_enabled"), False):
            continue
        name = str(cfg.get(f"custom{i}_name", "") or "").strip()
        datatype = str(cfg.get(f"custom{i}_datatype", "generic_data") or "generic_data").strip()
        unit = str(cfg.get(f"custom{i}_unit", "") or "").strip()
        group = str(cfg.get(f"custom{i}_group", "Custom SQL") or "Custom SQL").strip()
        # Pandora replaces the _sqlFileN_ macro with the path of a temporary
        # file whose entire contents come from the SQL textarea.
        sql = ""
        query_path = query_files.get(i)
        if query_path:
            path = Path(query_path)
            if path.is_file():
                sql = path.read_text(encoding="utf-8", errors="replace").strip()
        # Backward compatibility for direct invocations or one-line tasks.
        if not sql:
            sql = str(cfg.get(f"custom{i}_query", "") or "").strip()
        result_mode = str(cfg.get(f"custom{i}_result", "auto") or "auto").strip().lower()
        items.append({
            "name": name,
            "datatype": datatype,
            "unit": unit,
            "module_group": group,
            "target": sql,
            "operation": result_mode,
        })
    return items


def format_table(rows, cols, max_rows=100):
    rows = list(rows or [])
    cols = [str(c) for c in (cols or [])]
    if max_rows > 0:
        shown = rows[:max_rows]
    else:
        shown = rows
    # Keep a SQL query containing newlines from breaking the ASCII table.
    def cell(value):
        text = "NULL" if value is None else str(value)
        text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
        return text[:400] + "..." if len(text) > 400 else text

    all_data = [cols] + [[cell(x) for x in row] for row in shown]
    if not all_data or not cols:
        return "N/A"
    widths = [max(len(row[i]) if i < len(row) else 0 for row in all_data) for i in range(len(cols))]
    out = []
    for idx, row in enumerate(all_data):
        padded = [(row[i] if i < len(row) else "").ljust(widths[i]) for i in range(len(cols))]
        line = " | ".join(padded)
        out.append(line)
        if idx == 0:
            out.append("-" * len(line))
    if len(rows) > len(shown):
        out.append(f"... truncated: showing {len(shown)} of {len(rows)} rows")
    text = "\n".join(out)
    if len(text) > 60000:
        text = text[:60000] + "\n... output truncated to 60000 characters"
    return text


def result_value(cursor, datatype, operation="value", max_rows=100):
    rows = cursor.fetchall() if cursor.description else []
    cols = [d[0] for d in cursor.description] if cursor.description else []
    operation = str(operation or "value").lower()

    # Preserve the configured STRING type when it is already a string module.
    # In particular, an existing async_string module should not suddenly be
    # sent as generic_data_string: Pandora need not mutate the module type.
    string_type = datatype if datatype in STRING_TYPES else "generic_data_string"

    # Explicit table/full mode is intended for pg_stat_activity results.
    if operation in {"table", "full"}:
        return format_table(rows, cols, max_rows), string_type

    # Explicit scalar mode uses only the first column of the first row.
    if operation == "scalar":
        if not rows:
            return ("N/A" if datatype in STRING_TYPES else 0), datatype
        val = rows[0][0]
        if val is None:
            val = "N/A" if datatype in STRING_TYPES else 0
        return val, datatype

    # Custom modules default to auto. Multi-row / multi-column query results are
    # converted to a textual table regardless of the selected datatype. This
    # prevents Pandora from trying to ingest a table as a scalar module.
    if operation == "auto":
        if len(rows) > 1 or (rows and len(rows[0]) > 1):
            return format_table(rows, cols, max_rows), string_type
        if not rows:
            return ("N/A" if datatype in STRING_TYPES else 0), datatype
        val = rows[0][0]
        if val is None:
            val = "N/A" if datatype in STRING_TYPES else 0
        return val, datatype

    # Legacy/built-in behavior. Keep existing type for string catalog modules,
    # but protect numeric modules from receiving a multi-cell table.
    if len(rows) > 1 or (rows and len(rows[0]) > 1):
        effective_type = datatype if datatype in STRING_TYPES else "generic_data_string"
        return format_table(rows, cols, max_rows), effective_type
    if not rows:
        return ("N/A" if datatype in STRING_TYPES else 0), datatype
    val = rows[0][0]
    if val is None:
        val = "N/A" if datatype in STRING_TYPES else 0
    return val, datatype


def module_xml(name, value, datatype="generic_data", unit="", group="", description="", cfg=None):
    cfg = cfg or {}
    parts = [
        "<module>",
        f"<name>{xml_escape(clean_name(name))}</name>",
        f"<type>{xml_escape(datatype)}</type>",
    ]
    if description:
        parts.append(f"<description>{xml_escape(description)}</description>")
    if unit:
        parts.append(f"<unit>{xml_escape(unit)}</unit>")
    if group:
        parts.append(f"<module_group>{xml_escape(group)}</module_group>")
    if datatype in STRING_TYPES:
        parts.append(f"<data><![CDATA[{xml_cdata(value)}]]></data>")
    else:
        parts.append(f"<data>{xml_escape(value)}</data>")
    for key in ("min_warning", "max_warning", "min_critical", "max_critical", "str_warning", "str_critical", "warning_inverse", "critical_inverse", "module_interval"):
        if key in cfg and str(cfg[key]).strip() != "":
            parts.append(f"<{key}>{xml_escape(cfg[key])}</{key}>")
    parts.append("</module>")
    return "\n".join(parts)


def write_agent_xml(agent, group, address, modules):
    ts = datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    return (
        f"<agent_data agent_name='{xml_escape(agent)}' timestamp='{ts}' "
        f"group='{xml_escape(group)}' os_name='PostgreSQL' os_version='-' "
        f"alias='{xml_escape(agent)}' address='{xml_escape(address)}' "
        f"agent_version='postgresql_disco.{VERSION}'>\n"
        + "\n".join(modules)
        + "\n</agent_data>\n"
    )


def write_xml(outdir, agent, xml_text):
    Path(outdir).mkdir(parents=True, exist_ok=True)
    path = Path(outdir) / f"{agent}_{int(time.time())}.data"
    path.write_text(xml_text, encoding="utf-8")
    return str(path)


def acquire_lock(path, enabled=True):
    if not enabled:
        return None
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    except BlockingIOError:
        fh.close()
        return False


def main():
    ap = argparse.ArgumentParser(description="Pandora FMS PostgreSQL Discovery collector")
    ap.add_argument("--config", required=True)
    ap.add_argument("--custom-queries")
    ap.add_argument("--sql-files", nargs=10, metavar="SQLFILE", help="One Discovery tempfile per custom SQL textarea")
    ap.add_argument("--outdir", default="/var/spool/pandora/data_in")
    ap.add_argument("--lock-file", default="/tmp/pandorafms-postgresql.lock")
    ap.add_argument("--run-log")
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()

    started = time.time()
    cfg = read_kv_config(args.config)
    run_log = args.run_log or cfg.get("run_log", "")
    host = cfg.get("host", "").strip()
    port = str(cfg.get("port", "5432") or "5432")
    database = cfg.get("database", "postgres").strip() or "postgres"
    user = cfg.get("user", "").strip()
    password = cfg.get("password", "")
    group = cfg.get("group", "Databases") or "Databases"
    prefix = cfg.get("module_prefix", "")
    agent = clean_agent(cfg.get("agent") or f"PG-{host}-{database}")
    max_rows = safe_int(cfg.get("max_rows"), 100, 1, 5000)
    overlap = parse_bool(cfg.get("overlap_protection"), True)

    if not host or not user:
        print(json.dumps({"summary": {"Result": "PLUGIN ERROR"}, "info": "host and user are required"}))
        return 2

    lock_handle = acquire_lock(args.lock_file, overlap)
    if lock_handle is False:
        log_line(run_log, "WARNING", f"Skipped overlapping execution for agent={agent} host={host}")
        print(json.dumps({"summary": {"Agent": agent, "Target": host, "Result": "SKIPPED"}, "info": "Another execution of this Discovery task is still running; no extra PostgreSQL session was opened."}))
        return 0

    modules = []
    output_path = ""
    conn = None
    success = 0
    failed = 0
    query_count = 0

    # Always report one connection/session collector indicator.
    try:
        try:
            import psycopg2
        except ImportError as e:
            raise RuntimeError("Python module psycopg2 is not installed. Install python3-psycopg2 or psycopg2-binary on the Pandora Discovery server.") from e

        connect_timeout = safe_int(cfg.get("connect_timeout"), 10, 1, 120)
        statement_timeout = safe_int(cfg.get("statement_timeout_ms"), 15000, 100, 3600000)
        lock_timeout = safe_int(cfg.get("lock_timeout_ms"), 3000, 0, 3600000)
        sslmode = cfg.get("sslmode", "prefer") or "prefer"
        application_name = cfg.get("application_name", "PandoraFMS-PostgreSQL-Discovery") or "PandoraFMS-PostgreSQL-Discovery"
        options = (
            f"-c statement_timeout={statement_timeout} "
            f"-c lock_timeout={lock_timeout} "
            f"-c idle_in_transaction_session_timeout={statement_timeout} "
            f"-c default_transaction_read_only=on"
        )

        log_line(run_log, "INFO", f"Start agent={agent} host={host}:{port} db={database}; opening exactly one PostgreSQL session")
        conn = psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=database,
            connect_timeout=connect_timeout,
            sslmode=sslmode,
            application_name=application_name,
            options=options,
        )
        conn.autocommit = True

        modules.append(module_xml(prefix + "PostgreSQL:Connection", 1, "generic_proc", group="PostgreSQL Collector", cfg={"min_critical": 0, "max_critical": 0}))
        modules.append(module_xml(prefix + "PostgreSQL:CollectorSessions", 1, "generic_data", unit="session", group="PostgreSQL Collector", description="Database sessions opened by this collector execution"))

        with conn.cursor() as cursor:
            # Small identity query using the same session.
            try:
                cursor.execute("SELECT current_database(), current_user, version()")
                row = cursor.fetchone()
                query_count += 1
                if row:
                    modules.append(module_xml(prefix + "PostgreSQL:CurrentDatabase", row[0], "generic_data_string", group="PostgreSQL Collector"))
                    modules.append(module_xml(prefix + "PostgreSQL:MonitoringUser", row[1], "generic_data_string", group="PostgreSQL Collector"))
            except Exception as e:
                failed += 1
                log_line(run_log, "WARNING", f"Identity query failed: {e}")

            # Built-in modules, using one cursor/session for all queries.
            builtins = json.loads(BUILTIN_FILE.read_text(encoding="utf-8"))
            for mod_name, item in builtins.items():
                group_name = item.get("module_group", "PostgreSQL")
                flag = GROUP_FLAGS.get(group_name)
                if flag and not parse_bool(cfg.get(flag), True):
                    continue
                sql = replace_macros(item.get("query", ""), cfg)
                if not select_only(sql):
                    failed += 1
                    log_line(run_log, "WARNING", f"Built-in query rejected (not SELECT/WITH): {mod_name}")
                    continue
                try:
                    cursor.execute(sql)
                    query_count += 1
                    datatype = item.get("type", "generic_data")
                    value, datatype = result_value(cursor, datatype, "value", max_rows)
                    modules.append(module_xml(
                        prefix + mod_name,
                        value,
                        datatype,
                        item.get("unit", ""),
                        group_name,
                        replace_macros(item.get("desc", ""), cfg),
                    ))
                    success += 1
                except Exception as e:
                    failed += 1
                    log_line(run_log, "WARNING", f"Built-in query failed [{mod_name}]: {e}")

            # Custom SQL modules defined directly in the Discovery UI.
            if parse_bool(cfg.get("custom_enabled"), False):
                custom_items = parse_custom_slots(
                    cfg,
                    query_files={i: name for i, name in enumerate(args.sql_files or [], 1)},
                )
                # Backward compatibility: optional bulk file can still be parsed if supplied manually.
                if args.custom_queries:
                    custom_items.extend(parse_custom_queries(args.custom_queries))
                for idx, item in enumerate(custom_items, 1):
                    name = replace_macros(item.get("name", f"custom_query_{idx}"), cfg)
                    sql = replace_macros(item.get("target") or item.get("sql", ""), cfg)
                    if not name or not sql:
                        failed += 1
                        log_line(run_log, "WARNING", f"Custom query #{idx} missing name or target")
                        continue
                    if not select_only(sql):
                        failed += 1
                        log_line(run_log, "WARNING", f"Custom query rejected (only SELECT/WITH allowed): {name}")
                        continue
                    datatype = item.get("datatype", item.get("type", "generic_data")) or "generic_data"
                    operation = item.get("operation", "value") or "value"
                    try:
                        cursor.execute(sql)
                        query_count += 1
                        value, effective_type = result_value(cursor, datatype, operation, max_rows)
                        modules.append(module_xml(
                            prefix + name,
                            value,
                            effective_type,
                            item.get("unit", ""),
                            item.get("module_group", "Custom SQL") or "Custom SQL",
                            replace_macros(item.get("description", item.get("desc", "")), cfg),
                            item,
                        ))
                        success += 1
                    except Exception as e:
                        failed += 1
                        log_line(run_log, "WARNING", f"Custom query failed [{name}]: {e}")

        duration_ms = int((time.time() - started) * 1000)
        modules.append(module_xml(prefix + "PostgreSQL:CollectorQueries", query_count, "generic_data", unit="queries", group="PostgreSQL Collector"))
        modules.append(module_xml(prefix + "PostgreSQL:CollectorQueryErrors", failed, "generic_data", unit="errors", group="PostgreSQL Collector"))
        modules.append(module_xml(prefix + "PostgreSQL:CollectionTime", duration_ms, "generic_data", unit="ms", group="PostgreSQL Collector"))

    except Exception as e:
        failed += 1
        errmsg = str(e)
        log_line(run_log, "ERROR", f"Connection/collector failure host={host}:{port} db={database}: {errmsg}")
        modules.append(module_xml(prefix + "PostgreSQL:Connection", 0, "generic_proc", group="PostgreSQL Collector", cfg={"min_critical": 0, "max_critical": 0}))
        modules.append(module_xml(prefix + "PostgreSQL:ConnectionError", errmsg[:1500], "generic_data_string", group="PostgreSQL Collector"))
        modules.append(module_xml(prefix + "PostgreSQL:CollectorSessions", 0, "generic_data", unit="session", group="PostgreSQL Collector"))
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

    xml_text = write_agent_xml(agent, group, host, modules)
    if args.stdout:
        print(xml_text)
        output_path = "stdout"
    else:
        output_path = write_xml(args.outdir, agent, xml_text)

    log_line(run_log, "INFO", f"Finish agent={agent}; success={success} failed={failed} SQL={query_count} sessions<=1 output={output_path}")
    print(json.dumps({
        "summary": {
            "Agent": agent,
            "Target": f"{host}:{port}/{database}",
            "SQL queries": query_count,
            "Modules OK": success,
            "Errors": failed,
            "DB sessions used": 1 if conn is not None else 0,
            "Output": output_path,
        },
        "info": "PostgreSQL monitoring data generated using one reusable database session."
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
