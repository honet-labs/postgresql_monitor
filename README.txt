Pandora FMS PostgreSQL Query Monitoring Discovery
=================================================
Version: 1.1.3
Short name: pandorafms.postgresql_monitor

Purpose
-------
This package converts the supplied PostgreSQL XML querier into a Pandora FMS
Discovery application. The built-in module catalogue comes from pg_queries.json,
but new SQL modules are added from the Discovery task UI instead of editing a
JSON file on the server.

Session design
--------------
Each task execution opens ONE PostgreSQL connection and reuses ONE cursor for
all enabled built-in and custom SQL modules. The connection is closed when the
run finishes. A non-blocking lock prevents two runs of the same Discovery task
from overlapping, avoiding session pile-up if a previous run is still active.

The session is configured read-only and uses:
- connect_timeout
- statement_timeout
- lock_timeout
- idle_in_transaction_session_timeout
- application_name=PandoraFMS-PostgreSQL-Discovery

Requirements
------------
Python 3 and psycopg2 on the Pandora Discovery server.
Examples for RHEL/Rocky/Alma depend on the enabled repository, commonly:
  dnf install python3-psycopg2
or use the same Python environment where the original pg_querier.py already
works.

Custom modules from UI
----------------------
Step 3 uses separate fields for each custom SQL module:

Previous versions supported a text-only bulk format:

1) Quick format (one module per line):

  NAME|DATATYPE|UNIT|MODULE_GROUP|SELECT QUERY

Example:

  active_connections|generic_data|connections|Custom SQL|SELECT count(*) FROM pg_stat_activity WHERE state='active';
  database_size|generic_data|bytes|Custom SQL|SELECT pg_database_size(current_database());

The SQL part is the remainder after the fourth pipe, so PostgreSQL pipe
operators can still appear in the SQL.

2) Advanced Pandora-style block format:

  check_begin
  name long_running_queries
  description Active queries older than 60 seconds
  operation value
  datatype generic_data
  unit queries
  module_group Custom SQL
  min_warning 1
  min_critical 5
  target SELECT count(*) FROM pg_stat_activity WHERE state='active' AND query_start < now() - interval '60 seconds';
  check_end

Supported block keys:
  name                 required
  target / sql         required
  description
  operation            value | full
  datatype             generic_data | generic_data_string | generic_proc
  unit
  module_group
  min_warning
  max_warning
  min_critical
  max_critical
  str_warning
  str_critical
  warning_inverse
  critical_inverse
  module_interval

Only SELECT/WITH statements are accepted, and the PostgreSQL session itself is
read-only for an additional safety layer.

Macros available in built-in/custom SQL:
  {{DBNAME}} or $__self_dbname
  {{HOST}}
  {{PORT}}
  {{USER}}

Password substitution is intentionally not supported in SQL.

Built-in groups
---------------
Basic Info
Connections
Performance
Security & Roles
Transactions
Tuples Statistics

The supplied pg_queries.json is bundled as pg_queries_builtin.json. Each group
can be enabled/disabled from the Discovery UI.

Collector self-monitoring modules
---------------------------------
PostgreSQL:Connection
PostgreSQL:CollectorSessions
PostgreSQL:CollectorQueries
PostgreSQL:CollectorQueryErrors
PostgreSQL:CollectionTime
PostgreSQL:CurrentDatabase
PostgreSQL:MonitoringUser

A failed database connection still writes XML with Connection=0 and the error
text, so Pandora can show the outage instead of simply receiving no data.

Manual test
-----------
Pandora creates the config and ten individual SQL temporary files through
[tempfile_confs]; no need to write pg_queries.json for custom modules.
The real Discovery command automatically supplies --sql-files with ten paths.
For standalone testing call the Python script with --config and --sql-files
(ten paths, one per textarea). The output can be inspected with --stdout.

Diagnostic query:
  grep -iE "Custom query|failed|SyntaxError|error" \
    /var/log/pandora-scan/postgresql_discovery.run.log | tail -30



UI v1.1.1 - FIELD-BASED CUSTOM MODULES
---------------------------------------
The custom SQL textarea bulk format has been replaced in the Discovery UI by up to 10 progressive module slots.
Enable custom SQL modules, then enable Module 1. Each enabled module has separate fields for Name, Datatype, Unit, Module Group and SQL Query.
Enabling a module reveals the checkbox for the next module, approximating an Add Module workflow while remaining compatible with the standard Pandora FMS .disco form field system.

Pandora FMS .disco does not document a dynamic repeater/add-row field type; standard supported fields are string, number, password, textarea, checkbox, select, multiselect and tree. Therefore this package uses progressive optional slots instead of modifying Pandora Console PHP/JavaScript.


UI v1.1.1:
- Custom SQL step uses a single-column layout for tighter spacing.
- Module 1 no longer needs a separate Add module 1 toggle; enabling Custom SQL directly shows Module 1.
- Additional modules remain progressive using Add another custom module toggles.


Version 1.1.2 - custom query result fix
----------------------------------------
- Custom query Result mode added: Auto / Table / Scalar.
- Auto converts any multi-row or multi-column result to generic_data_string.
- String module data is emitted as CDATA and XML 1.0-invalid control characters are removed.
- This specifically supports list queries such as SELECT ... FROM pg_stat_activity.
- Existing Pandora modules created with an older datatype may need to be deleted/recreated once after upgrading.

Version 1.1.3 - multiline custom SQL and existing module compatibility
-----------------------------------------------------------------------
Root cause identified in v1.1.2: temporary key=value config included a raw
multiline SQL textarea. The collector parsed only the first SQL line, such as
"SELECT", resulting in a PostgreSQL syntax error and unchanged/N/A Pandora data.

Fix:
- Each UI custom SQL query is now stored in its OWN Pandora temporary file.
- The Python collector reads the full file including newlines and operators (=).
- The ordinary temporary config only stores scalar fields, not multiline SQL.
- Auto/Table result modes format multirow data as a readable ASCII table.
- Existing async_string datatype is preserved; a text module is not silently
  switched to generic_data_string when its SELECT returns multiple rows.
- One PostgreSQL connection per execution; built-ins and custom queries share it.
- SQL text included in pg_stat_activity.query may expose application literals:
  use a suitably privileged monitoring role and restrict module visibility.

Upgrade from 1.1.2:
- Upload v1.1.3 to replace the existing Discovery package.
- Edit the existing Discovery task and SAVE it again so temporary macros and
  the execution definition are regenerated from the new package.
- For List Connections choose async_string (or generic_data_string if creating
  a new module), Result Mode = Auto or Table, and the multiline SELECT query.
- For a non-destructive test give the module a NEW name, e.g. List Connections v113.
- Force-run Discovery, then check module data and collector log.
- If the new module works but the old one remains N/A, the old module type or
  historic data could be incompatible; delete/recreate ONLY the affected module
  after confirming the new one works. Do not delete the whole agent.
