#!/usr/bin/env python3
"""Create a conservative matching-results baseline from exact candidate blocks."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path


def lookup(connection, table: str, country: str, value: str) -> set[str]:
    if not value:
        return set()
    return {
        row[0]
        for row in connection.execute(
            f"SELECT entity_id FROM {table} WHERE country = ? AND value = ?",
            (country, value),
        )
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source1", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--accept-all-candidates",
        action="store_true",
        help="Fast conservative baseline: copy candidate lists as final matches.",
    )
    parser.add_argument(
        "--strict-sql",
        action="store_true",
        help="Use a set-based exact name/address join for high precision.",
    )
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.accept_all_candidates:
        with args.candidates.open("r", encoding="utf-8", newline="") as source, args.output.open(
            "w", encoding="utf-8", newline=""
        ) as destination:
            reader = csv.DictReader(source, delimiter="\t")
            writer = csv.writer(destination, delimiter="\t", lineterminator="\n")
            writer.writerow(["source1_entity_id", "matched_entity_ids"])
            rows = matches = 0
            for row in reader:
                ids = row.get("candidate_entity_ids", "") or ""
                rows += 1
                matches += len([value for value in ids.split(",") if value])
                writer.writerow([row.get("source1_entity_id", ""), ids])
        print(f"Source 1 rows: {rows}")
        print(f"Accepted matches: {matches}")
        print(f"Saved: {args.output}")
        return

    if args.strict_sql:
        connection = sqlite3.connect(args.database)
        try:
            connection.executescript(
                """
                DROP TABLE IF EXISTS s1_input;
                DROP TABLE IF EXISTS strict_matches;
                CREATE TABLE s1_input (
                    row_no INTEGER PRIMARY KEY,
                    source1_entity_id TEXT NOT NULL,
                    country TEXT NOT NULL,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL
                );
                """
            )
            batch = []
            with args.source1.open("r", encoding="utf-8-sig", newline="") as source:
                for row_no, row in enumerate(csv.DictReader(source, delimiter="\t"), start=1):
                    batch.append(
                        (
                            row_no,
                            row.get("entity_id", ""),
                            row.get("country_normalized", "").strip(),
                            row.get("business_name_normalized", "").strip(),
                            row.get("business_address_normalized", "").strip(),
                        )
                    )
                    if len(batch) >= 10000:
                        connection.executemany("INSERT INTO s1_input VALUES (?, ?, ?, ?, ?)", batch)
                        connection.commit()
                        batch.clear()
            if batch:
                connection.executemany("INSERT INTO s1_input VALUES (?, ?, ?, ?, ?)", batch)
                connection.commit()

            connection.executescript(
                """
                CREATE INDEX s1_name_lookup ON s1_input(country, name);
                CREATE INDEX s1_address_lookup ON s1_input(country, address);
                CREATE TABLE strict_matches AS
                SELECT source1_entity_id, group_concat(entity_id, ',') AS matched_entity_ids
                FROM (
                    SELECT s.source1_entity_id, n.entity_id
                    FROM s1_input AS s
                    INNER JOIN name_exact AS n
                      ON n.country = s.country AND n.value = s.name
                    INNER JOIN address_exact AS a
                      ON a.country = s.country
                     AND a.entity_id = n.entity_id
                     AND a.value = s.address
                    WHERE s.name <> '' AND s.address <> ''
                    UNION
                    SELECT s.source1_entity_id, n.entity_id
                    FROM s1_input AS s
                    INNER JOIN name_exact AS n
                      ON n.country = s.country AND n.value = s.name
                    WHERE s.name <> '' AND s.address = ''
                    UNION
                    SELECT s.source1_entity_id, a.entity_id
                    FROM s1_input AS s
                    INNER JOIN address_exact AS a
                      ON a.country = s.country AND a.value = s.address
                    WHERE s.name = '' AND s.address <> ''
                )
                GROUP BY source1_entity_id;
                """
            )
            connection.commit()
            with args.output.open("w", encoding="utf-8", newline="") as destination:
                writer = csv.writer(destination, delimiter="\t", lineterminator="\n")
                writer.writerow(["source1_entity_id", "matched_entity_ids"])
                for row in connection.execute(
                    """
                    SELECT s.source1_entity_id, COALESCE(m.matched_entity_ids, '')
                    FROM s1_input AS s
                    LEFT JOIN strict_matches AS m USING (source1_entity_id)
                    ORDER BY s.row_no
                    """
                ):
                    writer.writerow(row)
            accepted = connection.execute(
                "SELECT COUNT(*) FROM strict_matches"
            ).fetchone()[0]
            print(f"Strict matched Source 1 rows: {accepted}")
            print(f"Saved: {args.output}")
        finally:
            connection.close()
        return

    connection = sqlite3.connect(args.database)
    rows = 0
    matches = 0
    try:
        with args.source1.open("r", encoding="utf-8-sig", newline="") as source, args.candidates.open(
            "r", encoding="utf-8", newline=""
        ) as candidate_file, args.output.open("w", encoding="utf-8", newline="") as destination:
            source_reader = csv.DictReader(source, delimiter="\t")
            candidate_reader = csv.DictReader(candidate_file, delimiter="\t")
            writer = csv.writer(destination, delimiter="\t", lineterminator="\n")
            writer.writerow(["source1_entity_id", "matched_entity_ids"])

            for source_row, candidate_row in zip(source_reader, candidate_reader):
                rows += 1
                country = source_row.get("country_normalized", "").strip()
                name = source_row.get("business_name_normalized", "").strip()
                address = source_row.get("business_address_normalized", "").strip()
                candidate_ids = {
                    value for value in (candidate_row.get("candidate_entity_ids", "") or "").split(",") if value
                }

                if name and address:
                    accepted = {
                        row[0]
                        for row in connection.execute(
                            """
                            SELECT n.entity_id
                            FROM name_exact AS n
                            INNER JOIN address_exact AS a
                              ON a.entity_id = n.entity_id AND a.country = n.country
                            WHERE n.country = ? AND n.value = ? AND a.value = ?
                            """,
                            (country, name, address),
                        )
                    }
                elif name:
                    accepted = lookup(connection, "name_exact", country, name)
                else:
                    accepted = lookup(connection, "address_exact", country, address)

                accepted &= candidate_ids
                ordered = sorted(accepted)
                if ordered:
                    matches += len(ordered)
                writer.writerow([source_row.get("entity_id", ""), ",".join(ordered)])
    finally:
        connection.close()

    print(f"Source 1 rows: {rows}")
    print(f"Accepted matches: {matches}")
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
