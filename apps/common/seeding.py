"""Load the anonymised UMaT sample dataset (seed/output/json) into the database.

Tables are matched to models by `db_table`, columns to fields by database column name, so the
loader keeps working as new modules add models. Tables or columns without a model are reported
and skipped. Roles and permissions come from the code catalogue; seed ids are remapped by code.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from django.apps import apps
from django.contrib.auth.hashers import make_password
from django.db import connection, models, transaction

from apps.access.catalog import sync_catalog

CATALOG_TABLES = {"roles", "permissions", "role_permissions"}


@dataclass
class LoadReport:
    loaded: dict[str, int] = field(default_factory=dict)
    skipped_tables: list[str] = field(default_factory=list)
    dropped_columns: dict[str, set[str]] = field(default_factory=dict)


def table_registry():
    return {m._meta.db_table: m for m in apps.get_models() if not m._meta.proxy}


def _catalog_models():
    return (
        apps.get_model("access", "Role"),
        apps.get_model("access", "Permission"),
        apps.get_model("access", "RolePermission"),
    )


def flush(registry=None):
    """Delete all data in DeptHub tables (keeps migrations), then reinstall the role catalogue."""
    registry = registry or table_registry()
    tables = [
        name
        for name, model in registry.items()
        if model._meta.managed and model.__module__.startswith("apps.")
    ]
    if tables:
        quoted = ", ".join(connection.ops.quote_name(t) for t in tables)
        with connection.cursor() as cursor:
            cursor.execute(f"TRUNCATE {quoted} CASCADE")  # noqa: S608 - names come from model metadata
    sync_catalog(*_catalog_models())


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _catalog_remap(seed_dir: Path):
    """Map seed role/permission ids to the ids of the catalogue rows with the same code."""
    Role, Permission, _ = _catalog_models()
    sync_catalog(*_catalog_models())
    role_ids = {r.role_code: r.pk for r in Role.objects.all()}
    perm_ids = {p.permission_code: p.pk for p in Permission.objects.all()}
    remap = {"role_id": {}, "permission_id": {}}
    for row in _read(seed_dir / "roles.json"):
        if row["role_code"] not in role_ids:
            raise ValueError(f"Seed role {row['role_code']!r} is not in the catalogue.")
        remap["role_id"][row["id"]] = role_ids[row["role_code"]]
    for row in _read(seed_dir / "permissions.json"):
        if row["permission_code"] not in perm_ids:
            raise ValueError(f"Seed permission {row['permission_code']!r} is not in the catalogue.")
        remap["permission_id"][row["id"]] = perm_ids[row["permission_code"]]
    return remap


def _convert(model_field, value):
    if value is None:
        return None
    if isinstance(model_field, models.JSONField):
        return value
    return model_field.to_python(value)


@transaction.atomic
def load(seed_dir: Path, password: str, batch_size: int = 1000) -> LoadReport:
    manifest = _read(seed_dir.parent / "manifest.json")
    registry = table_registry()
    remap = _catalog_remap(seed_dir)
    password_hash = make_password(password)
    report = LoadReport()

    for table in manifest["row_counts"]:
        if table in CATALOG_TABLES:
            continue
        model = registry.get(table)
        if model is None:
            report.skipped_tables.append(table)
            continue
        columns = {f.column: f for f in model._meta.concrete_fields}
        objects = []
        for row in _read(seed_dir / f"{table}.json"):
            values = {}
            for column, value in row.items():
                model_field = columns.get(column)
                if model_field is None:
                    report.dropped_columns.setdefault(table, set()).add(column)
                    continue
                if column in remap and table != "role_permissions":
                    value = remap[column][value]
                values[model_field.attname] = _convert(model_field, value)
            if table == "users":
                values["password"] = password_hash
            objects.append(model(**values))
        model.objects.bulk_create(objects, batch_size=batch_size)
        report.loaded[table] = len(objects)
    _analyze(report.loaded)
    return report


def _analyze(tables):
    """Refresh planner statistics after the bulk load.

    Without them PostgreSQL may believe the tables are still empty and pick nested-loop plans
    that make queries such as v_dues_balances crawl.
    """
    if not tables:
        return
    quoted = ", ".join(connection.ops.quote_name(t) for t in tables)
    with connection.cursor() as cursor:
        cursor.execute(f"ANALYZE {quoted}")  # noqa: S608 - names come from model metadata
