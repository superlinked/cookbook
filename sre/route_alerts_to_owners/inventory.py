"""The host and namespace inventory, as the recipe looks things up in it.

This stands in for the records your company already keeps about where things run: a CMDB or
cloud-tag lookup by IP address or host name, and the owner annotation on a Kubernetes
namespace. Here it is backed by two readable CSV seeds in `data/`, loaded into an in-memory
SQLite database:

- `inventory_hosts.csv`: one row per host, its IP address, host name and the catalog service
  that runs on it.
- `inventory_namespaces.csv`: one row per Kubernetes namespace and the catalog service it holds.

Both map to a service, not a team; the service catalog says who owns the service. Matching is
exact, ignoring case: `10.0.4.170` is not `10.0.4.17`.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


class Inventory:
    """Hosts and namespaces from the inventory seeds, with plain lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.executescript(
            """
            CREATE TABLE hosts (address TEXT UNIQUE, hostname TEXT UNIQUE, service TEXT NOT NULL);
            CREATE TABLE namespaces (namespace TEXT PRIMARY KEY, service TEXT NOT NULL);
            """
        )
        for row in _rows(data_dir / "inventory_hosts.csv"):
            self._db.execute(
                "INSERT INTO hosts VALUES (?, ?, ?)",
                (_key(row["address"]), _key(row["hostname"]), row["service"].strip()),
            )
        for row in _rows(data_dir / "inventory_namespaces.csv"):
            self._db.execute(
                "INSERT INTO namespaces VALUES (?, ?)",
                (_key(row["namespace"]), row["service"].strip()),
            )

    def find_host(self, instance: str) -> str | None:
        """The service on the host an `instance` label names (`address:port`, `host:port`,
        `[ipv6]:port` or a bare address), or None."""
        host = host_part(instance)
        row = self._db.execute(
            "SELECT service FROM hosts WHERE address = ? OR hostname = ?", (host, host)
        ).fetchone()
        return str(row[0]) if row else None

    def find_namespace(self, namespace: str) -> str | None:
        """The service a Kubernetes namespace holds, or None."""
        row = self._db.execute(
            "SELECT service FROM namespaces WHERE namespace = ?", (_key(namespace),)
        ).fetchone()
        return str(row[0]) if row else None


def host_part(instance: str) -> str:
    """'10.0.4.17:9187' -> '10.0.4.17'; '[fd00::7]:9100' -> 'fd00::7'; casefolded."""
    value = _key(instance)
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:  # host:port; more than one colon is a bare IPv6 address
        return value.split(":", 1)[0]
    return value


def _key(value: str) -> str:
    return value.strip().casefold()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
