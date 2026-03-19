#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ls-s3arc: List .s3arc stub files with metadata, S3 location, and optional restore status."""

import argparse
import os
import sys
import json
import boto3
from botocore.exceptions import BotoCoreError, ClientError

from s3arc_common import STUB_EXT, VERSION, fmt_size, read_stub

DEFAULT_BUCKET = os.environ.get("S3ARC_BUCKET")


def get_s3_status(s3_client, bucket, key):
    """Query S3 for storage class and restore status."""
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
        storage_class = head.get("StorageClass", "STANDARD")
        restore = head.get("Restore", "")
        if storage_class == "GLACIER_IR":
            return storage_class, "Available (instant recall)"
        elif storage_class in ("GLACIER", "DEEP_ARCHIVE"):
            if 'ongoing-request="true"' in restore:
                return storage_class, "Restore in progress"
            elif 'ongoing-request="false"' in restore:
                return storage_class, "Restore complete (temporary copy available)"
            else:
                return storage_class, "Archived (restore required)"
        else:
            return storage_class, "Available"
    except (BotoCoreError, ClientError) as e:
        return "UNKNOWN", f"Error: {e}"


def list_stub(filepath, s3_client=None):
    """Read and return metadata for a single .s3arc stub."""
    meta = read_stub(filepath)
    key = meta.get("s3key")
    bucket = meta.get("bucket") or DEFAULT_BUCKET
    stub_type = meta.get("type", "file")
    checksum = meta.get("checksum")
    storage_class = meta.get("storage_class")

    info = {
        "path": filepath,
        "type": stub_type,
        "bucket": bucket,
        "s3_key": key or "MISSING",
    }

    if storage_class:
        info["archived_storage_class"] = storage_class
        info["tier"] = "offline" if storage_class == "DEEP_ARCHIVE" else "online"

    if checksum:
        info["checksum"] = checksum

    try:
        st = os.stat(filepath)
        info["mtime"] = st.st_mtime
        info["mode"] = oct(st.st_mode)
    except OSError as e:
        print(f"Warning: cannot stat {filepath}: {e}", file=sys.stderr)

    if stub_type == "aggregate" and "manifest" in meta:
        info["manifest"] = meta["manifest"]

    if s3_client and key:
        info["storage_class"], info["status"] = get_s3_status(s3_client, bucket, key)

    return info


def print_stub(info):
    """Pretty-print a single stub's metadata."""
    print(f"  {info['path']}")
    print(f"    Type:     {info['type']}")
    if "tier" in info:
        print(f"    Tier:     {info['tier']} ({info['archived_storage_class']})")
    print(f"    Bucket:   {info['bucket']}")
    print(f"    S3 Key:   {info['s3_key']}")
    if "checksum" in info:
        print(f"    SHA256:   {info['checksum']}")
    if "storage_class" in info:
        print(f"    Class:    {info['storage_class']}")
        print(f"    Status:   {info['status']}")
    if "mode" in info:
        print(f"    Mode:     {info['mode']}")
    if info["type"] == "aggregate" and "manifest" in info:
        m = info["manifest"]
        orig = fmt_size(m.get("original_bytes", 0))
        arc = fmt_size(m.get("compressed_bytes", 0))
        ratio = ""
        if m.get("original_bytes") and m.get("compressed_bytes"):
            ratio = f" ({m['original_bytes'] / m['compressed_bytes']:.1f}x)"
        print(f"    Contents: {m.get('files', '?')} files")
        print(f"    Size:     {orig} original | {arc} compressed{ratio}")
        if "created" in m:
            print(f"    Archived: {m['created']}")
        
        # Check for local manifest file
        manifest_path = info['path'].replace('.s3arc', '.manifest')
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, 'r') as f:
                    lines = [l for l in f.readlines() if l.strip()]
                if not lines:
                    print(f"    Manifest: {os.path.basename(manifest_path)} (empty)")
                elif not lines[0][0] in '-dlcrwxbps':
                    print(f"    Manifest: {os.path.basename(manifest_path)} (invalid format)")
                else:
                    print(f"    Manifest: {len(lines)} files listed (local file available)")
                    print(f"              Use: cat {os.path.basename(manifest_path)}")
            except OSError as e:
                print(f"    Manifest: {os.path.basename(manifest_path)} (unreadable: {e})")
        else:
            print(f"    Manifest: {os.path.basename(manifest_path)} (missing)")
    print()


def main():
    parser = argparse.ArgumentParser(
        prog="ls-s3arc",
        description="List .s3arc stub files with metadata, S3 location, and optional restore status.",
        epilog=f"Environment variables:\n  S3ARC_BUCKET  Fallback bucket (default: {DEFAULT_BUCKET})",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check-status", action="store_true", help="Query S3 for storage class and restore status")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Output as JSON")
    parser.add_argument("--version", action="version", version=f"ls-s3arc {VERSION}")
    parser.add_argument("paths", nargs="+", metavar="path", help="Files or directories to scan")
    args = parser.parse_args()

    check_status = args.check_status
    json_output = args.json_output
    targets = args.paths

    s3_client = None
    if check_status:
        try:
            s3_client = boto3.client("s3")
        except (BotoCoreError, ClientError) as e:
            print(f"WARNING: cannot init S3 client: {e}. Skipping status checks.", file=sys.stderr)

    stubs = []
    for target in targets:
        if not os.path.exists(target):
            print(f"ERROR: path not found: {target}", file=sys.stderr)
            continue
        if os.path.isfile(target) and target.endswith(STUB_EXT):
            stubs.append(list_stub(target, s3_client))
        elif os.path.isdir(target):
            for root, _, files in os.walk(target):
                for fname in sorted(files):
                    fpath = os.path.join(root, fname)
                    if fpath.endswith(STUB_EXT) and os.path.isfile(fpath):
                        stubs.append(list_stub(fpath, s3_client))

    if json_output:
        print(json.dumps(stubs, indent=2, default=str))
    else:
        file_stubs = [s for s in stubs if s["type"] == "file"]
        agg_stubs = [s for s in stubs if s["type"] == "aggregate"]

        if file_stubs:
            print(f"\nFILE STUBS ({len(file_stubs)}):")
            for s in file_stubs:
                print_stub(s)

        if agg_stubs:
            print(f"\nAGGREGATE STUBS ({len(agg_stubs)}):")
            for s in agg_stubs:
                print_stub(s)

        if not stubs:
            print("No .s3arc stubs found.")
        else:
            print(f"Summary: {len(file_stubs)} file stubs, {len(agg_stubs)} aggregate stubs | {len(stubs)} total")

    sys.exit(0)


if __name__ == "__main__":
    main()
