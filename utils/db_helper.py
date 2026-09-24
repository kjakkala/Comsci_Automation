import os
import pyodbc
from typing import Any


class DBHelper:
    """Thin wrapper around a pyodbc SQL Server connection."""

    def __init__(self):
        trusted = os.getenv("DB_TRUSTED_CONNECTION", "0") == "1"
        # TrustServerCertificate=yes mirrors the SSMS "Trust Server Certificate" checkbox
        if trusted:
            conn_str = (
                f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                f"SERVER={os.environ['DB_SERVER']};"
                f"DATABASE={os.environ['DB_NAME']};"
                "Trusted_Connection=yes;"
                "Encrypt=yes;"
                "TrustServerCertificate=yes;"
            )
        else:
            conn_str = (
                f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                f"SERVER={os.environ['DB_SERVER']};"
                f"DATABASE={os.environ['DB_NAME']};"
                f"UID={os.environ['DB_USER']};"
                f"PWD={os.environ['DB_PASSWORD']};"
                "Encrypt=yes;"
                "TrustServerCertificate=yes;"
            )
        self._conn = pyodbc.connect(conn_str, autocommit=True)

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute a SELECT and return rows as a list of dicts."""
        cursor = self._conn.cursor()
        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def fetch_one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        """Execute a SELECT and return the first row as a dict, or None."""
        results = self.fetch_all(query, params)
        return results[0] if results else None

    def fetch_scalar(self, query: str, params: tuple = ()) -> Any:
        """Execute a SELECT and return a single scalar value."""
        cursor = self._conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return row[0] if row else None

    def count_rows(self, table: str, where_clause: str = "", params: tuple = ()) -> int:
        """Return row count for a table, optionally filtered by a WHERE clause fragment."""
        query = f"SELECT COUNT(*) FROM {table}"
        if where_clause:
            query += f" WHERE {where_clause}"
        return self.fetch_scalar(query, params) or 0

    def execute(self, statement: str, params: tuple = ()) -> None:
        """Execute a non-SELECT statement (UPDATE/EXEC/etc). Connection is autocommit."""
        cursor = self._conn.cursor()
        cursor.execute(statement, params)

    def close(self):
        self._conn.close()
