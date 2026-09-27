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
