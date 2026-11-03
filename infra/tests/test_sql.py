"""The Cloud SQL settings a stack chooses are parsed strictly."""

from __future__ import annotations

import pytest

from themis_infra import sql


def test_a_weekday_and_hour_parse_to_the_admin_apis_numbering() -> None:
    assert sql.parse_maintenance_window('Mon 00:00 UTC') == sql.MaintenanceWindow(day=1, hour=0)
    assert sql.parse_maintenance_window('Sun 23:00 UTC') == sql.MaintenanceWindow(day=7, hour=23)


def test_any_leaves_the_window_to_cloud_sql() -> None:
    assert sql.parse_maintenance_window(sql.ANY_MAINTENANCE_WINDOW) is None


@pytest.mark.parametrize(
    'value', ['', 'Sun 24:00 UTC', 'Sun 16:30 UTC', 'sun 16:00 UTC', 'Sunday 16:00 UTC', 'Sun 16:00', 'Fun 16:00 UTC']
)
def test_anything_else_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match='maintenance window'):
        sql.parse_maintenance_window(value)


def test_an_unknown_availability_type_is_refused() -> None:
    with pytest.raises(ValueError, match='availability_type'):
        sql.CloudSqlDatabase(
            project='p', region='r', tier='db-f1-micro', availability_type='zonal', maintenance_window=None
        )
