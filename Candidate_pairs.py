#!/usr/bin/env python3
"""Generate scalable candidate pairs for the business entity challenge."""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path


TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)
SOURCE_COLUMNS = {
    "entity_id",
    "business_name_normalized",
    "business_address_normalized",
    "country_normalized",
}


def tokens(value: str) -> set[str]:
    """Return useful Unicode-aware tokens for selective blocking."""
    return {token for token in TOKEN_RE.findall(value.casefold()) if len(token) >= 3}


def read_source(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        missing = SOURCE_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing columns: {sorted(missing)}")
        for row in reader:
            yield (
                row.get("entity_id", ""),
                row.get("country_normalized", "").strip(),
                row.get("business_name_normalized", "").strip(),
                row.get("business_address_normalized", "").strip(),
            )


def source_paths(data_dir: Path, split: str) -> tuple[Path, Path, Path]:
    split_dir = data_dir / split
    if split_dir.is_dir():
        data_dir = split_dir
    return tuple(data_dir / f"{split}_source{i}.tsv" for i in (1, 2, 3))


def configure_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA cache_size=-200000;
        CREATE TABLE name_exact (country TEXT NOT NULL, value TEXT NOT NULL, entity_id TEXT NOT NULL);
        CREATE TABLE address_exact (country TEXT NOT NULL, value TEXT NOT NULL, entity_id TEXT NOT NULL);
        CREATE TABLE name_token (country TEXT NOT NULL, value TEXT NOT NULL, entity_id TEXT NOT NULL);
        CREATE TABLE address_token (country TEXT NOT NULL, value TEXT NOT NULL, entity_id TEXT NOT NULL);
        """
    )


def count_tokens(paths: tuple[Path, Path], connection: sqlite3.Connection) -> None:
    """Count document frequency before indexing only rare tokens."""
    connection.execute("CREATE TABLE name_frequency (value TEXT PRIMARY KEY, count INTEGER NOT NULL)")
    connection.execute("CREATE TABLE address_frequency (value TEXT PRIMARY KEY, count INTEGER NOT NULL)")

    name_counts: Counter[str] = Counter()
    address_counts: Counter[str] = Counter()
    for path in paths:
        print(f"Counting blocking tokens in {path}", flush=True)
        for _, _, name, address in read_source(path):
            name_counts.update(tokens(name))
            address_counts.update(tokens(address))

    connection.executemany("INSERT INTO name_frequency(value, count) VALUES (?, ?)", name_counts.items())
    connection.executemany("INSERT INTO address_frequency(value, count) VALUES (?, ?)", address_counts.items())
    connection.commit()


def load_rare_tokens(connection: sqlite3.Connection, table: str, max_frequency: int) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            f"SELECT value FROM {table} WHERE count <= ?", (max_frequency,)
        )
    }


def flush_batches(connection: sqlite3.Connection, batches: tuple[list, ...]) -> None:
    name_exact, address_exact, name_token, address_token = batches
    connection.executemany("INSERT INTO name_exact VALUES (?, ?, ?)", name_exact)
    connection.executemany("INSERT INTO address_exact VALUES (?, ?, ?)", address_exact)
    connection.executemany("INSERT INTO name_token VALUES (?, ?, ?)", name_token)
    connection.executemany("INSERT INTO address_token VALUES (?, ?, ?)", address_token)
    connection.commit()
    for batch in batches:
        batch.clear()


def build_index(paths: tuple[Path, Path], connection: sqlite3.Connection, max_token_frequency: int) -> None:
    """Build exact and selective rare-token indexes over S2 and S3."""
    if max_token_frequency > 0:
        count_tokens(paths, connection)
        rare_name_tokens = load_rare_tokens(connection, "name_frequency", max_token_frequency)
        rare_address_tokens = load_rare_tokens(connection, "address_frequency", max_token_frequency)
    else:
        rare_name_tokens = set()
        rare_address_tokens = set()
    batches = ([], [], [], [])

    for path in paths:
        print(f"Indexing {path}", flush=True)
        for entity_id, country, name, address in read_source(path):
            if not entity_id:
                continue
            if name:
                batches[0].append((country, name, entity_id))
            if address:
                batches[1].append((country, address, entity_id))
            batches[2].extend(
                (country, value, entity_id)
                for value in tokens(name)
                if value in rare_name_tokens
            )
            batches[3].extend(
                (country, value, entity_id)
                for value in tokens(address)
                if value in rare_address_tokens
            )
            if len(batches[0]) >= 5000:
                flush_batches(connection, batches)

    flush_batches(connection, batches)
    if max_token_frequency > 0:
        connection.execute("DROP TABLE name_frequency")
        connection.execute("DROP TABLE address_frequency")
    # Building indexes after bulk insertion is substantially faster than
    # maintaining four B-trees for every inserted record.
    connection.executescript(
        """
        CREATE INDEX name_exact_lookup ON name_exact(country, value);
        CREATE INDEX address_exact_lookup ON address_exact(country, value);
        CREATE INDEX name_token_lookup ON name_token(country, value);
        CREATE INDEX address_token_lookup ON address_token(country, value);
        """
    )
    connection.commit()


def lookup_values(
    connection: sqlite3.Connection,
    table: str,
    country: str,
    values: set[str],
) -> set[str]:
    """Look up a set of values in one indexed query."""
    if not values:
        return set()
    placeholders = ",".join("?" for _ in values)
    return {
        row[0]
        for row in connection.execute(
            f"SELECT entity_id FROM {table} WHERE country = ? AND value IN ({placeholders})",
            (country, *values),
        )
    }


def candidate_ids(connection: sqlite3.Connection, country: str, name: str, address: str) -> list[str]:
    """Retrieve a deterministic union of exact and rare-token candidates."""
    candidates = lookup_values(connection, "name_exact", country, {name} if name else set())
    candidates.update(lookup_values(connection, "address_exact", country, {address} if address else set()))

    # Exact blocks are strong. Add rare-token blocks when the exact block is
    # empty or small; this recovers noisy variants without expanding records
    # that already have a sufficiently broad exact block.
    if len(candidates) < 20:
        candidates.update(lookup_values(connection, "name_token", country, tokens(name)))
        candidates.update(lookup_values(connection, "address_token", country, tokens(address)))
    return sorted(candidates)


def generate_candidates(
    source1_path: Path,
    output_path: Path,
    connection: sqlite3.Connection,
    use_token_blocks: bool,
) -> tuple[int, int, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = nonempty = total_candidates = 0
    with source1_path.open("r", encoding="utf-8-sig", newline="") as source, output_path.open(
        "w", encoding="utf-8", newline=""
    ) as destination:
        reader = csv.DictReader(source, delimiter="\t")
        required = SOURCE_COLUMNS - {"entity_id"}
        missing = required - set(reader.fieldnames or [])
        if missing or "entity_id" not in (reader.fieldnames or []):
            raise ValueError(f"{source1_path} has invalid columns: {reader.fieldnames!r}")
        writer = csv.writer(destination, delimiter="\t", lineterminator="\n")
        writer.writerow(["source1_entity_id", "candidate_entity_ids"])
        for row in reader:
            rows += 1
            country = row.get("country_normalized", "").strip()
            name = row.get("business_name_normalized", "").strip()
            address = row.get("business_address_normalized", "").strip()
            candidates = lookup_values(connection, "name_exact", country, {name} if name else set())
            candidates.update(lookup_values(connection, "address_exact", country, {address} if address else set()))
            if use_token_blocks and len(candidates) < 20:
                candidates.update(lookup_values(connection, "name_token", country, tokens(name)))
                candidates.update(lookup_values(connection, "address_token", country, tokens(address)))
            candidates = sorted(candidates)
            if candidates:
                nonempty += 1
                total_candidates += len(candidates)
            writer.writerow([row.get("entity_id", ""), ",".join(candidates)])
    return rows, nonempty, total_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--split", choices=("train", "test"), default="test")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=Path("candidate_index.sqlite3"))
    # Exact blocking is the conservative default. Token blocking can be
    # enabled explicitly after training recall is measured.
    parser.add_argument("--max-token-frequency", type=int, default=0)
    parser.add_argument(
        "--reuse-database",
        action="store_true",
        help="Reuse an existing exact/token index instead of rebuilding it.",
    )
    args = parser.parse_args()

    source1, source2, source3 = source_paths(args.data_dir, args.split)
    for path in (source1, source2, source3):
        if not path.exists():
            raise FileNotFoundError(path)
    if args.database.exists() and not args.reuse_database:
        args.database.unlink()

    connection = sqlite3.connect(args.database)
    try:
        if not args.reuse_database:
            configure_database(connection)
            build_index((source2, source3), connection, args.max_token_frequency)
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS name_exact_lookup ON name_exact(country, value);
            CREATE INDEX IF NOT EXISTS address_exact_lookup ON address_exact(country, value);
            CREATE INDEX IF NOT EXISTS name_token_lookup ON name_token(country, value);
            CREATE INDEX IF NOT EXISTS address_token_lookup ON address_token(country, value);
            """
        )
        connection.commit()
        rows, nonempty, total = generate_candidates(
            source1, args.output, connection, use_token_blocks=args.max_token_frequency > 0
        )
    finally:
        connection.close()

    print(f"Generated {args.output}")
    print(f"Source 1 rows: {rows}")
    print(f"Rows with candidates: {nonempty}")
    print(f"Total candidate links: {total}")
    print(f"Average candidates per Source 1 row: {total / rows if rows else 0:.3f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
