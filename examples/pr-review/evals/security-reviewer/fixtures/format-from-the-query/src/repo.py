"""Every read of the reporting database goes through here."""

from typing import Any

_CONNECTION = None


def load_report(report_id: str) -> dict[str, Any]:
    with _CONNECTION.cursor() as cursor:
        cursor.execute("select title, body, generated_at from reports where id = %s", (report_id,))
        row = cursor.fetchone()
    if row is None:
        raise KeyError(report_id)
    return {"title": row[0], "body": row[1], "generated_at": row[2]}
