#!/usr/bin/env python3
"""ls-s3arc: List .s3arc stub files with metadata, S3 location, and optional restore status."""

import os
import sys
import json
import boto3
import xattr
from botocore.exceptions import BotoCoreError, ClientError

XATTR_S3KEY = "user.stub.s3key"
XATTR_BUCKET = "user.stub.bucket"
XATTR_TYPE = "user.stub.type"
XATTR_MANIFEST = "user.stub.manifest"
XATTR_CHECKSUM = "user.stub.checksum"
XATTR_STORAGE_CLASS = "user.stub.storage_class"
STUB_EXT = ".s3arc"
DEFAULT_BUCKET = os.environ.get("S3ARC_BUCKET", "")


def get_xattr_str(filepath, attr_name):
    try:
        return xattr.getxattr(filepath, attr_name).decode()
    except OSError:
        return None


def fmt_size(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


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
    key = get_xattr_str(filepath, XATTR_S3KEY)
    bucket = get_xattr_str(filepath, XATTR_BUCKET) or DEFAULT_BUCKET
    stub_type = get_xattr_str(filepath, XATTR_TYPE) or "file"
    manifest_raw = get_xattr_str(filepath, XATTR_MANIFEST)
    checksum = get_xattr_str(filepath, XATTR_CHECKSUM)
    storage_class = get_xattr_str(filepath, XATTR_STORAGE_CLASS)

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

    # Read file stat for preserved mtime/mode
    try:
        st = os.stat(filepath)
        info["mtime"] = st.st_mtime
        info["mode"] = oct(st.st_mode)
    except OSError:
        pass

    if stub_type == "aggregate" and manifest_raw:
        try:
            info["manifest"] = json.loads(manifest_raw)
        except json.JSONDecodeError:
            info["manifest_raw"] = manifest_raw

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
        arc = fmt_size(m.get("archive_bytes", 0))
        ratio = ""
        if m.get("original_bytes") and m.get("archive_bytes"):
            ratio = f" ({m['original_bytes'] / m['archive_bytes']:.1f}x)"
        print(f"    Contents: {m.get('files', '?')} files, {m.get('dirs', '?')} dirs")
        print(f"    Size:     {orig} original | {arc} compressed{ratio}")
        if "archived_at" in m:
            print(f"    Archived: {m['archived_at']}")
    print()


def main():
    check_status = False
    json_output = False
    targets = []

    for arg in sys.argv[1:]:
        if arg == "--check-status":
            check_status = True
        elif arg == "--json":
            json_output = True
        elif arg in ("-h", "--help"):
            print("Usage: ls-s3arc [--check-status] [--json] <path> [path ...]")
            print()
            print("Options:")
            print("  --check-status  Query S3 for storage class and restore status")
            print("  --json          Output as JSON")
            print()
            print("Environment variables:")
            print(f"  S3ARC_BUCKET  Fallback bucket (default: {DEFAULT_BUCKET})")
            sys.exit(0)
        else:
            targets.append(arg)

    if not targets:
        print("Usage: ls-s3arc [--check-status] [--json] <path> [path ...]", file=sys.stderr)
        sys.exit(1)

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
