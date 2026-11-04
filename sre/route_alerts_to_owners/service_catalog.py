"""The service catalog, as the recipe looks things up in it.

This stands in for a call to your real catalog, such as Backstage's catalog API
(`GET /api/catalog/entities/by-name/component/default/<name>`, reading `spec.owner`) or a
YAML file of services kept in a repo. Here it is backed by three readable CSV seeds in `data/`,
loaded into an in-memory SQLite database:

- `catalog_teams.csv`: every team that can own alerts, with a one-line description of what it
  runs. The descriptions are what the team model reads.
- `catalog_services.csv`: one row per service, its owning team (empty when nobody owns it) and
  the other names it shows up under in alert labels (`;`-separated).

Matching is exact, ignoring case and surrounding spaces: `checkout` is not `checkout-ui`, and
`payments-api-v2` is not `payments-api`. A name that is close but not the same is a different
service, and the recipe treats it as unknown.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Service:
    name: str
    owner: str | None  # a team id, or None when the catalog records no owner


class ServiceCatalog:
    """Teams and services from the catalog seeds, with plain lookups."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.executescript(
            """
            CREATE TABLE teams (team TEXT PRIMARY KEY, description TEXT NOT NULL);
            CREATE TABLE services (service TEXT PRIMARY KEY, owner TEXT);
            CREATE TABLE names (name TEXT PRIMARY KEY, service TEXT NOT NULL);
            """
        )
        for row in _rows(data_dir / "catalog_teams.csv"):
            self._db.execute(
                "INSERT INTO teams VALUES (?, ?)", (row["team"].strip(), row["description"].strip())
            )
        if not self.teams():
            raise ValueError(
                f"{data_dir / 'catalog_teams.csv'} lists no teams; the catalog needs at least one "
                "team for the model to choose from"
            )
        for row in _rows(data_dir / "catalog_services.csv"):
            service = row["service"].strip()
            self._db.execute(
                "INSERT INTO services VALUES (?, ?)", (service, row["owner"].strip() or None)
            )
            aliases = [alias.strip() for alias in row["aliases"].split(";") if alias.strip()]
            for name in [service, *aliases]:
                try:
                    self._db.execute("INSERT INTO names VALUES (?, ?)", (_key(name), service))
                except sqlite3.IntegrityError:
                    raise ValueError(f"catalog name {name!r} is used by two services") from None

    def teams(self) -> dict[str, str]:
        """Every team id and its one-line description, in the seed's order."""
        rows = self._db.execute("SELECT team, description FROM teams ORDER BY rowid")
        return {team: description for team, description in rows}

    def find_service(self, name: str) -> Service | None:
        """The service a label value names (its catalog name or an alias), or None."""
        row = self._db.execute(
            "SELECT s.service, s.owner FROM names n JOIN services s ON s.service = n.service "
            "WHERE n.name = ?",
            (_key(name),),
        ).fetchone()
        return Service(row[0], row[1]) if row else None


def _key(name: str) -> str:
    return name.strip().casefold()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
