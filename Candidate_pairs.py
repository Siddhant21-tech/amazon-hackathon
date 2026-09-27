import pandas as pd
import re

files = [
    r"C:\Users\MISTHI\amazon-hackathon\student_resource\dataset_normalized\test\test_source1.tsv",
    r"C:\Users\MISTHI\amazon-hackathon\student_resource\dataset_normalized\test\test_source2.tsv",
    r"C:\Users\MISTHI\amazon-hackathon\student_resource\dataset_normalized\test\test_source3.tsv"
]

output_file = r"C:\Users\MISTHI\amazon-hackathon\combined_cleaned.tsv"


# Read all files
dataframes = []

for file in files:
    print("Reading:", file)

    df = pd.read_csv(
        file,
        sep="\t",
        dtype=str
    )

    df.columns = [
        re.sub(r'[^a-z0-9]+', '_', str(c).lower()).strip('_')
        for c in df.columns
    ]

    dataframes.append(df)

    print("Rows:", len(df))


# Combine all files
combined = pd.concat(
    dataframes,
    ignore_index=True
)

print("\nTotal rows:", len(combined))


# Use normalized business name and address
name = combined["business_name_normalized"].fillna("").str.strip()
address = combined["business_address_normalized"].fillna("").str.strip()


# Create duplicate key
combined["_key"] = name + "|" + address


# If address is missing, use business name only
combined.loc[
    address == "",
    "_key"
] = name[address == ""]


# Remove records with no business name
combined = combined[name != ""]


print("Removing duplicates...")


# Remove duplicate businesses
result = combined.drop_duplicates(
    subset="_key",
    keep="first"
)


# Remove temporary key
result = result.drop(
    columns="_key"
)


# Save result
result.to_csv(
    output_file,
    sep="\t",
    index=False
)


print("\n==============================")
print("COMBINATION COMPLETE")
print("==============================")
print("Rows before:", len(combined))
print("Rows after:", len(result))
print("Duplicates removed:", len(combined) - len(result))
print("Saved to:", output_file)
