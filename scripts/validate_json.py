import json
import os
import argparse
import sys
from jsonschema import validate, ValidationError

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", required=True, help="Path to JSON file to validate")
    parser.add_argument("--schema", required=True, help="Path to JSON schema file")
    args = parser.parse_args()

    if not os.path.exists(args.json):
        print(f"JSON file not found: {args.json}")
        sys.exit(1)

    if not os.path.exists(args.schema):
        print(f"Schema file not found: {args.schema}")
        sys.exit(1)

    with open(args.json, "r", encoding="utf-8") as f:
        instance = json.load(f)

    with open(args.schema, "r", encoding="utf-8") as f:
        schema = json.load(f)

    try:
        validate(instance=instance, schema=schema)
        print(f"Validation successful: {args.json}")
    except ValidationError as e:
        print(f"Validation failed for {args.json}: {e.message}")
        sys.exit(1)

if __name__ == "__main__":
    main()
