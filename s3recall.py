#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""s3recall: Restore .s3arc stub files from S3 Glacier Instant Retrieval."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.exceptions import BotoCoreError, ClientError

from s3arc_common import (
    STUB_EXT, FSX_TAG_SNS_TOPIC, FSX_TAG_RESTORE_DAYS, FSX_TAG_RESTORE_TIER,
    DEFAULT_RESTORE_DAYS, DEFAULT_RESTORE_TIER, VERSION,
    fmt_size, get_s3_client, read_stub, send_sns_notification,
    get_fsx_id_from_mount, ProgressTracker,
)

DEFAULT_WORKERS = 8
DEFAULT_BUCKET = os.environ.get("S3ARC_BUCKET")


def get_config_from_fsx(target_path):
    """Get configuration from FSx tags with environment variable fallback."""
    config = {
        "sns_topic": os.environ.get("S3ARC_SNS_TOPIC", ""),
        "restore_days": int(os.environ.get("S3ARC_RESTORE_DAYS", DEFAULT_RESTORE_DAYS)),
        "restore_tier": os.environ.get("S3ARC_RESTORE_TIER", DEFAULT_RESTORE_TIER)
    }
    
    # Try to get FSx filesystem info
    fsx_id, fsx_type, region, mount_point = get_fsx_id_from_mount(target_path)
    
    if fsx_id:
        try:
            fsx = boto3.client("fsx", region_name=region) if region else boto3.client("fsx")
            response = fsx.describe_file_systems(FileSystemIds=[fsx_id])
            
            if response.get("FileSystems"):
                fs = response["FileSystems"][0]
                resource_arn = fs.get("ResourceARN")
                
                if resource_arn:
                    tags_response = fsx.list_tags_for_resource(ResourceARN=resource_arn)
                    fsx_tags = {}
                    for tag in tags_response.get("Tags", []):
                        fsx_tags[tag.get("Key")] = tag.get("Value")
                    
                    # Override with FSx tags if present
                    if FSX_TAG_SNS_TOPIC in fsx_tags:
                        config["sns_topic"] = fsx_tags[FSX_TAG_SNS_TOPIC]
                    
                    if FSX_TAG_RESTORE_DAYS in fsx_tags:
                        try:
                            config["restore_days"] = int(fsx_tags[FSX_TAG_RESTORE_DAYS])
                        except ValueError:
                            pass
                    
                    if FSX_TAG_RESTORE_TIER in fsx_tags:
                        config["restore_tier"] = fsx_tags[FSX_TAG_RESTORE_TIER]
        
        except (BotoCoreError, ClientError) as e:
            print(f"Warning: cannot retrieve FSx tags for {fsx_id}: {e}", file=sys.stderr)
    
    return config



def recall_file(filepath, config, s3_client, dry_run=False, progress=None, cached_head=None):
    """Restore a single .s3arc stub file from S3."""

    if not filepath.endswith(STUB_EXT):
        return filepath, 0, True, "skip", "not a stub"

    if not os.path.isfile(filepath):
        return filepath, 0, False, "skip", "not a regular file"

    meta = read_stub(filepath)
    key = meta.get("s3key")
    if not key:
        return filepath, 0, False, "error", "no S3 key in stub"

    bucket = meta.get("bucket") or DEFAULT_BUCKET
    if not bucket:
        return filepath, 0, False, "error", "no bucket configured"

    expected_checksum = meta.get("checksum")
    stored_class = meta.get("storage_class", "unknown")
    original_path = filepath[:-len(STUB_EXT)]

    if os.path.exists(original_path):
        return filepath, 0, False, "error", "original path already exists"

    # Get S3 object metadata (use cache if available)
    if cached_head:
        head = cached_head
    else:
        try:
            head = s3_client.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "404":
                return filepath, 0, False, "error", "S3 object not found"
            return filepath, 0, False, "error", f"cannot read S3 object: {e}"
        except BotoCoreError as e:
            return filepath, 0, False, "error", f"S3 error: {e}"

    file_size = head["ContentLength"]

    # Check storage class
    storage_class = head.get("StorageClass", "STANDARD")
    if storage_class in ("GLACIER", "DEEP_ARCHIVE"):
        restore_status = head.get("Restore", "")
        if 'ongoing-request="true"' in restore_status:
            return filepath, 0, False, "pending", "restore in progress"
        if 'ongoing-request="false"' not in restore_status and storage_class != "GLACIER_IR":
            if dry_run:
                return filepath, file_size, True, "would initiate", f"restore from {storage_class}"
            try:
                s3_client.restore_object(
                    Bucket=bucket, Key=key,
                    RestoreRequest={"Days": config["restore_days"], "GlacierJobParameters": {"Tier": config["restore_tier"]}}
                )
                return filepath, 0, False, "initiated", "restore requested, check back later"
            except ClientError as e:
                if "RestoreAlreadyInProgress" in str(e):
                    return filepath, 0, False, "pending", "restore already in progress"
                return filepath, 0, False, "error", f"cannot initiate restore: {e}"

    if dry_run:
        return filepath, file_size, True, "would recall", f"-> {original_path}"

    # Ownership check: non-root can only restore their own files
    s3_metadata = head.get("Metadata", {})
    original_uid = s3_metadata.get("original-uid")
    if original_uid and os.getuid() != 0 and int(original_uid) != os.getuid():
        return filepath, 0, False, "error", f"ownership mismatch: file belongs to uid {original_uid}, you are uid {os.getuid()} (run as root to restore other users' files)"

    # Set up progress callback
    callback = None
    if progress:
        callback = progress.update_bytes

    # Download file
    try:
        s3_client.download_file(bucket, key, original_path, Callback=callback)
    except (BotoCoreError, ClientError) as e:
        try:
            if os.path.exists(original_path):
                os.remove(original_path)
        except OSError as e:
            print(f"Warning: cannot remove partial download {original_path}: {e}", file=sys.stderr)
        return filepath, 0, False, "error", f"download failed: {e}"

    # Verify size
    expected_size = s3_metadata.get("original-size")
    if expected_size:
        actual_size = os.path.getsize(original_path)
        if actual_size != int(expected_size):
            pass  # Warning only, continue

    # Verify download size matches S3 object
    actual_size = os.path.getsize(original_path)
    if actual_size != file_size:
        try:
            os.remove(original_path)
        except OSError as e:
            print(f"Warning: cannot remove corrupted file {original_path}: {e}", file=sys.stderr)
        return filepath, 0, False, "error", f"size mismatch (expected={file_size}, actual={actual_size})"

    # Restore metadata
    try:
        mtime = s3_metadata.get("original-mtime")
        if mtime:
            mtime_f = float(mtime)
            os.utime(original_path, (mtime_f, mtime_f))
        mode = s3_metadata.get("original-mode")
        if mode:
            os.chmod(original_path, int(mode, 8))
        uid = s3_metadata.get("original-uid")
        gid = s3_metadata.get("original-gid")
        if uid and gid:
            os.chown(original_path, int(uid), int(gid))
    except (ValueError, OSError) as e:
        print(f"Warning: cannot restore metadata on {original_path}: {e}", file=sys.stderr)

    # Remove stub
    try:
        os.remove(filepath)
    except OSError as e:
        print(f"Warning: cannot remove stub {filepath}: {e}", file=sys.stderr)

    if progress:
        progress.file_complete(file_size)

    # Include storage class in status message
    tier_label = "offline" if stored_class == "DEEP_ARCHIVE" else "online"
    return filepath, file_size, True, "recalled", f"{original_path} ({tier_label})"


def recall_aggregate(filepath, config, dry_run=False):
    """Restore an aggregate .s3arc stub (directory archive)."""
    if not filepath.endswith(STUB_EXT):
        print(f"Error: {filepath} is not a stub file")
        return False
    
    # Get stub metadata
    meta = read_stub(filepath)
    key = meta.get("s3key")
    if not key:
        print(f"Error: no S3 key in stub {filepath}")
        return False
    
    bucket = meta.get("bucket") or DEFAULT_BUCKET
    if not bucket:
        print("Error: no bucket configured")
        return False
    
    manifest = meta.get("manifest", {})
    
    # Get directory where archive stub is located
    target_dir = os.path.dirname(filepath)
    
    s3_client = get_s3_client()
    
    # Get S3 object metadata
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "404":
            print(f"Error: S3 object not found: s3://{bucket}/{key}")
            return False
        print(f"Error: cannot read S3 object: {e}")
        return False
    except BotoCoreError as e:
        print(f"Error: S3 error: {e}")
        return False
    
    file_size = head["ContentLength"]
    storage_class = head.get("StorageClass", "STANDARD")

    # Ownership check: non-root can only recall their own archives
    s3_metadata = head.get("Metadata", {})
    original_uid = s3_metadata.get("original-uid")
    if original_uid and os.getuid() != 0 and int(original_uid) != os.getuid():
        print(f"Error: ownership mismatch on {filepath}: archive belongs to uid {original_uid}, you are uid {os.getuid()}")
        print("Run as root to restore other users' archives.")
        return False
    
    # Check if restore is needed for Deep Archive
    if storage_class in ("GLACIER", "DEEP_ARCHIVE"):
        restore_status = head.get("Restore", "")
        if 'ongoing-request="true"' in restore_status:
            print(f"Restore in progress for s3://{bucket}/{key}")
            print("Check back later when restore completes.")
            return False
        if 'ongoing-request="false"' not in restore_status and storage_class != "GLACIER_IR":
            if dry_run:
                print(f"Would initiate restore for s3://{bucket}/{key}")
                return True
            try:
                s3_client.restore_object(
                    Bucket=bucket, Key=key,
                    RestoreRequest={"Days": config["restore_days"], "GlacierJobParameters": {"Tier": config["restore_tier"]}}
                )
                print(f"Initiated {config['restore_tier']} retrieval for s3://{bucket}/{key}")
                tier_hours = "12" if config["restore_tier"] == "Standard" else "48"
                print(f"Restore initiated. Object will be available in ~{tier_hours} hours.")
                print("Run this command again to check status and complete recall.")
                return False
            except ClientError as e:
                if "RestoreAlreadyInProgress" in str(e):
                    print("Restore already in progress")
                    return False
                print(f"Error: cannot initiate restore: {e}")
                return False
    
    if dry_run:
        print(f"Would recall: {filepath}")
        print(f"  Archive: s3://{bucket}/{key} ({fmt_size(file_size)})")
        print(f"  Target directory: {target_dir}")
        if manifest and 'file_list' in manifest:
            print(f"  Files in archive: {', '.join(manifest['file_list'])}")
        return True
    
    # Check if any files would be overwritten
    if manifest and 'file_list' in manifest:
        existing_files = []
        for file in manifest['file_list']:
            file_path = os.path.join(target_dir, file)
            if os.path.exists(file_path):
                existing_files.append(file)
        
        if existing_files:
            print(f"Error: {len(existing_files)} files already exist and would be overwritten:")
            for file in existing_files[:5]:  # Show first 5
                print(f"  {file}")
            if len(existing_files) > 5:
                print(f"  ... and {len(existing_files) - 5} more")
            return False
    
    # Download archive to temp location
    temp_archive = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as f:
            temp_archive = f.name
        
        print(f"Downloading: {key} ({fmt_size(file_size)})")
        s3_client.download_file(bucket, key, temp_archive)
        
        # Verify download size
        actual_size = os.path.getsize(temp_archive)
        if actual_size != file_size:
            print(f"Error: download size mismatch (expected={file_size}, actual={actual_size})")
            return False
        
    except (BotoCoreError, ClientError) as e:
        print(f"Error: download failed: {e}")
        if temp_archive and os.path.exists(temp_archive):
            os.remove(temp_archive)
        return False
    
    # Extract archive to target directory
    try:
        print(f"Extracting files to: {target_dir}")
        cmd = ['tar', 'xzf', temp_archive, '-C', target_dir]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
    except subprocess.CalledProcessError as e:
        print(f"Error: extraction failed: {e.stderr}")
        if temp_archive:
            os.remove(temp_archive)
        return False
    except FileNotFoundError:
        print("Error: tar command not found")
        if temp_archive:
            os.remove(temp_archive)
        return False
    
    # Verify extraction against manifest
    if manifest and 'file_list' in manifest:
        try:
            extracted_files = manifest['file_list']
            missing_files = []
            for fname in extracted_files:
                if not os.path.exists(os.path.join(target_dir, fname)):
                    missing_files.append(fname)
            
            if missing_files:
                print(f"Warning: {len(missing_files)} files not found after extraction")
            else:
                print(f"Verified: {len(extracted_files)} files extracted")
        except OSError as e:
            print(f"Warning: could not verify extraction: {e}")
    
    # Clean up
    if temp_archive:
        os.remove(temp_archive)
    
    # Remove archive stub
    try:
        os.remove(filepath)
    except OSError as e:
        print(f"Warning: could not remove stub: {e}")
    
    # Remove associated manifest file
    manifest_path = filepath.replace('.s3arc', '.manifest')
    try:
        if os.path.exists(manifest_path):
            os.remove(manifest_path)
            print(f"Removed manifest: {os.path.basename(manifest_path)}")
    except OSError as e:
        print(f"Warning: could not remove manifest: {e}")
    
    # Send notification
    if config["sns_topic"]:
        subject = "S3Arc: Aggregate recall completed"
        message = f"Archive: s3://{bucket}/{key}\nRestored files to: {target_dir}"
        if manifest:
            message += f"\nFiles: {manifest.get('files', 'unknown')}\nOriginal size: {fmt_size(manifest.get('original_bytes', 0))}"
        send_sns_notification(subject, message, config["sns_topic"])
    
    tier_label = "offline" if storage_class == "DEEP_ARCHIVE" else "online"
    file_count = len(manifest.get('file_list', []))
    print(f"Recalled: {file_count} files to {target_dir} ({tier_label})")
    return True


def collect_stubs(target):
    """Collect stub files to process."""
    stubs = []
    if os.path.isfile(target) and target.endswith(STUB_EXT):
        stubs.append(target)
    elif os.path.isdir(target):
        for root, _, filenames in os.walk(target):
            for fname in sorted(filenames):
                fpath = os.path.join(root, fname)
                if os.path.isfile(fpath) and fpath.endswith(STUB_EXT):
                    stubs.append(fpath)
    return stubs


def main():
    parser = argparse.ArgumentParser(description="Recall files from S3 Glacier to local filesystem")
    parser.add_argument("--version", action="version", version=f"s3recall {VERSION}")
    parser.add_argument("target", help="Stub file or directory to recall")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be recalled without doing it")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress indicator")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel download workers (default: {DEFAULT_WORKERS})")
    args = parser.parse_args()

    if not os.path.exists(args.target):
        print(f"ERROR: path not found: {args.target}", file=sys.stderr)
        sys.exit(1)

    # Get configuration from FSx tags or environment variables
    config = get_config_from_fsx(args.target)

    # Verify credentials
    get_s3_client()

    # Check if target is a single aggregate stub
    if args.target.endswith(STUB_EXT) and os.path.isfile(args.target):
        meta = read_stub(args.target)
        if meta.get("type") == "aggregate":
            success = recall_aggregate(args.target, config, args.dry_run)
            sys.exit(0 if success else 1)

    stubs = collect_stubs(args.target)
    if not stubs:
        print("No stub files to recall.")
        sys.exit(0)

    # Separate aggregate and file stubs
    aggregate_stubs = []
    file_stubs = []
    
    for stub in stubs:
        meta = read_stub(stub)
        if meta.get("type") == "aggregate":
            aggregate_stubs.append(stub)
        else:
            file_stubs.append(stub)
    
    # Process aggregate stubs first (sequential processing)
    aggregate_success = 0
    aggregate_failed = 0
    
    for stub in aggregate_stubs:
        print(f"\nProcessing aggregate stub: {stub}")
        success = recall_aggregate(stub, config, args.dry_run)
        if success:
            aggregate_success += 1
        else:
            aggregate_failed += 1
    
    # Continue with file stubs using existing parallel logic
    stubs = file_stubs
    if not stubs and aggregate_stubs:
        # Only had aggregate stubs
        if aggregate_failed > 0:
            print(f"\nComplete: {aggregate_success} aggregate recalls, {aggregate_failed} failed")
            sys.exit(1)
        else:
            print(f"\nComplete: {aggregate_success} aggregate recalls")
            sys.exit(0)

    # Pre-scan: get sizes and cache HEAD responses
    total_bytes = 0
    head_cache = {}
    s3_client = get_s3_client()
    for stub in stubs:
        meta = read_stub(stub)
        key = meta.get("s3key")
        bucket = meta.get("bucket")
        if key and bucket:
            try:
                head = s3_client.head_object(Bucket=bucket, Key=key)
                total_bytes += head["ContentLength"]
                head_cache[stub] = head
            except (BotoCoreError, ClientError) as e:
                print(f"Warning: cannot get size for s3://{bucket}/{key}: {e}", file=sys.stderr)

    if args.dry_run:
        print(f"Dry run: {len(stubs)} stubs, {fmt_size(total_bytes)} total\n")

    progress = None
    if not args.dry_run and not args.no_progress and sys.stdout.isatty():
        progress = ProgressTracker(len(stubs), total_bytes)

    success = 0
    failed = 0
    pending = 0
    recalled_bytes = 0

    # Use thread pool for parallel downloads
    num_workers = min(args.workers, len(stubs))
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(recall_file, stub, config, s3_client, args.dry_run, progress, head_cache.get(stub)): stub
            for stub in stubs
        }
        for future in as_completed(futures):
            fpath, fsize, ok, status, detail = future.result()
            if ok:
                success += 1
                if status == "recalled":
                    recalled_bytes += fsize
            elif status in ("pending", "initiated"):
                pending += 1
                print(f"  {status}: {fpath} ({detail})")
            else:
                failed += 1
                print(f"  {status}: {fpath} ({detail})")

    if progress:
        progress.finish()

    parts = [f"{success} recalled"]
    if pending:
        parts.append(f"{pending} pending restore")
    if failed:
        parts.append(f"{failed} failed")
    
    # Include aggregate results in summary
    if aggregate_stubs:
        if aggregate_success > 0:
            parts.append(f"{aggregate_success} aggregate recalled")
        if aggregate_failed > 0:
            parts.append(f"{aggregate_failed} aggregate failed")
    
    print(f"\nComplete: {', '.join(parts)}, {fmt_size(recalled_bytes)} transferred")

    # Send SNS notification
    if not args.dry_run and config["sns_topic"]:
        total_success = success + aggregate_success
        total_failed = failed + aggregate_failed
        send_sns_notification(
            subject=f"S3Arc Recall Complete: {args.target}",
            message=f"Recalled {total_success} items ({fmt_size(recalled_bytes)}) to {args.target}\nFailed: {total_failed}",
            sns_topic=config["sns_topic"]
        )

    sys.exit(1 if (failed + aggregate_failed) > 0 else 0)


if __name__ == "__main__":
    main()
