#!/usr/bin/env python3
"""Normalize the challenge TSV files for downstream entity matching.

The raw input files are never modified. Each source output retains the raw
columns and adds normalized matching fields and explicit missing-value flags.
This script uses only the Python standard library and processes files in a
streaming fashion.
"""

from __future__ import annotations

import argparse
import csv
import re
import unicodedata
from pathlib import Path


def normalize_text(value: str) -> str:
    """Return a conservative, Unicode-aware matching representation."""
    if not value:
        return ""

    value = unicodedata.normalize("NFKC", value).casefold()
    cleaned = []
    for char in value:
        category = unicodedata.category(char)
        # Replace control/format characters and punctuation with a separator.
        # Letters and numbers from all scripts are retained.
        if category.startswith("C") or category.startswith("P"):
            cleaned.append(" ")
        else:
            cleaned.append(char)

    return re.sub(r"\s+", " ", "".join(cleaned)).strip()


def normalize_country(value: str) -> str:
    """Normalize an open-set country label without hard-coding countries."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", value).strip()


def normalize_source(input_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with input_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        expected = {"entity_id", "business_name", "business_address", "country"}
        if set(reader.fieldnames or []) != expected:
            raise ValueError(
                f"Unexpected columns in {input_path}: {reader.fieldnames!r}"
            )

        fieldnames = [
            "entity_id",
            "business_name",
            "business_address",
            "country",
            "business_name_normalized",
            "business_address_normalized",
            "country_normalized",
            "business_name_missing",
            "business_address_missing",
        ]

        with output_path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(
                destination,
                fieldnames=fieldnames,
                delimiter="\t",
                lineterminator="\n",
            )
            writer.writeheader()

            for row in reader:
                name = row.get("business_name", "") or ""
                address = row.get("business_address", "") or ""
                country = row.get("country", "") or ""
                writer.writerow(
                    {
                        "entity_id": row.get("entity_id", ""),
                        "business_name": name,
                        "business_address": address,
                        "country": country,
                        "business_name_normalized": normalize_text(name),
                        "business_address_normalized": normalize_text(address),
                        "country_normalized": normalize_country(country),
                        "business_name_missing": str(not bool(name.strip())).lower(),
                        "business_address_missing": str(not bool(address.strip())).lower(),
                    }
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    # The challenge files are named train_source1.tsv/test_source1.tsv, while
    # callers pass the split directory. Resolve the actual files explicitly.
    split = args.input_dir.name
    for source_number in (1, 2, 3):
        input_path = args.input_dir / f"{split}_source{source_number}.tsv"
        output_path = args.output_dir / f"{split}_source{source_number}.tsv"
        normalize_source(input_path, output_path)


if __name__ == "__main__":
    main()
