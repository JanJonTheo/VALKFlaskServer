"""Bounded table search and a compact catalog for the authorized admin route."""
import json
from datetime import datetime, timezone

from sqlalchemy import text


def quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def contains(value, needle):
    if value is None:
        return False
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return needle in str(value).lower()


def table_payload(session, table, args):
    table_sql = quoted(table)
    columns = [row[1] for row in session.execute(text(f"PRAGMA table_info({table_sql})"))]
    if not columns:
        raise ValueError("Unknown table")
    filters = json.loads(args.get("filters", "[]"))
    if not isinstance(filters, list):
        raise ValueError("filters must be a list")
    clauses, params = [], {}
    for index, item in enumerate(filters[:20]):
        if not isinstance(item, dict) or item.get("field") not in columns:
            continue
        field, operator, key = quoted(item["field"]), item.get("operator", "eq"), f"filter_{index}"
        if operator == "contains":
            clauses.append(f"CAST({field} AS TEXT) LIKE :{key}")
            params[key] = f'%{item.get("value", "")}%'
        elif operator in {"gte", "lte", "eq"}:
            sql_operator = {"gte": ">=", "lte": "<=", "eq": "="}[operator]
            clauses.append(f"{field} {sql_operator} :{key}")
            params[key] = item.get("value")
    needle = args.get("search", "").strip().lower()
    if needle:
        # SQLite's built-in lower()/LIKE are ASCII-only. Keep the browser's
        # Unicode, literal-substring semantics, including % and _ in JSON.
        session.connection().connection.driver_connection.create_function("dashboard_contains", 2, contains, deterministic=True)
        clauses.append("(" + " OR ".join(f"dashboard_contains({quoted(column)}, :search)" for column in columns) + ")")
        params["search"] = needle
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = session.execute(text(f"SELECT COUNT(*) FROM {table_sql}{where}"), params).scalar_one()
    page = max(1, args.get("page", 1, type=int) or 1)
    size = min(250, max(1, args.get("page_size", 25, type=int) or 25))
    result = {"data": [], "generated_at": datetime.now(timezone.utc).isoformat(), "pagination": {"page": page, "page_size": size, "total": total}, "meta": {"columns": columns}}
    if args.get("options") == "1":
        options = {}
        for field, key in [("cmdr", "cmdrs"), ("event", "events"), ("tickid", "tickids")]:
            options[key] = [row[0] for row in session.execute(text(f"SELECT DISTINCT {quoted(field)} FROM {table_sql}{where}"), params) if row[0] is not None] if field in columns else []
        result["meta"]["filter_options"] = options
        return result
    sort = args.get("sort")
    if sort not in columns:
        sort = "id" if "id" in columns else columns[0]
    direction = "DESC" if args.get("direction", "asc").lower() == "desc" else "ASC"
    order = f" ORDER BY {quoted(sort)} {direction}"
    if sort != "id" and "id" in columns:
        order += f', "id" {direction}'
    rows = session.execute(text(f"SELECT * FROM {table_sql}{where}{order} LIMIT :limit OFFSET :offset"), {**params, "limit": size, "offset": (page - 1) * size})
    result["data"] = [dict(row._mapping) for row in rows]
    return result
