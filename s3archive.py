#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""s3archive: Move files to S3 Glacier Instant Retrieval, leave .s3arc stub files with embedded metadata."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.exceptions import BotoCoreError, ClientError

from s3arc_common import (
    STUB_EXT, CHECKSUM_ALGORITHM, STORAGE_COSTS, DEFAULT_STORAGE_CLASS,
    FSX_TAG_ARCHIVE_BUCKET, FSX_TAG_STORAGE_CLASS, FSX_TAG_SNS_TOPIC, VERSION,
    fmt_size, get_s3_client, write_stub, send_sns_notification,
    get_fsx_id_from_mount, ProgressTracker,
)

# Configuration
DEFAULT_WORKERS = 8




def estimate_monthly_cost(nbytes, storage_class):
    """Estimate monthly storage cost for given bytes and storage class."""
    gb = nbytes / (1024 ** 3)
    rate = STORAGE_COSTS.get(storage_class, STORAGE_COSTS["GLACIER_IR"])
    return gb * rate


def format_cost_summary(nbytes, storage_class):
    """Return cost estimate string with per-TB reference rate."""
    cost = estimate_monthly_cost(nbytes, storage_class)
    rate = STORAGE_COSTS.get(storage_class, STORAGE_COSTS["GLACIER_IR"])
    tb_rate = rate * 1024
    return f"Est. monthly cost: ${cost:.8f}  (${tb_rate:.2f}/TB/mo for {storage_class})"






def validate_write_support(target_path):
    """Validate that we can create stub files on the target filesystem."""
    test_dir = os.path.dirname(target_path) if os.path.isfile(target_path) else target_path
    try:
        with tempfile.NamedTemporaryFile(dir=test_dir, delete=True, prefix=".s3arc_test_", suffix=STUB_EXT) as f:
            f.write(b'{"test": true}')
        return True
    except OSError as e:
        print(f"ERROR: Cannot create stub files in {test_dir}: {e}", file=sys.stderr)
        return False


def get_config_from_fsx(target_path):
    """Get all configuration from FSx tags with environment variable fallback."""
    config = {
        "bucket": None,
        "prefix": "",
        "mount_point": None,
        "storage_class": os.environ.get("S3ARC_STORAGE_CLASS", DEFAULT_STORAGE_CLASS),
        "sns_topic": os.environ.get("S3ARC_SNS_TOPIC", ""),
        "source": "defaults"
    }
    
    # Try to get FSx filesystem info
    fsx_id, fsx_type, region, mount_point = get_fsx_id_from_mount(target_path)
    
    if fsx_id:
        # Auto-generate prefix from FSx type and ID
        type_label = {"ONTAP": "ONTAP", "LUSTRE": "Lustre"}.get(fsx_type, fsx_type)
        config["prefix"] = f"FSx{type_label}/{fsx_id}/"
        config["mount_point"] = mount_point
        
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
                    if FSX_TAG_ARCHIVE_BUCKET in fsx_tags:
                        config["bucket"] = fsx_tags[FSX_TAG_ARCHIVE_BUCKET]
                        config["source"] = f"FSx tag ({fsx_id})"
                    
                    if FSX_TAG_STORAGE_CLASS in fsx_tags:
                        config["storage_class"] = fsx_tags[FSX_TAG_STORAGE_CLASS]
                    
                    if FSX_TAG_SNS_TOPIC in fsx_tags:
                        config["sns_topic"] = fsx_tags[FSX_TAG_SNS_TOPIC]
        
        except (BotoCoreError, ClientError) as e:
            print(f"Warning: cannot retrieve FSx tags for {fsx_id}: {e}", file=sys.stderr)
    
    # Fall back to environment variable for bucket if not in tags
    if not config["bucket"]:
        env_bucket = os.environ.get("S3ARC_BUCKET")
        if env_bucket:
            config["bucket"] = env_bucket
            config["source"] = "S3ARC_BUCKET env var"
    
    return config


def s3_key(filepath, base, prefix):
    return prefix + os.path.relpath(filepath, base)


def stub_file(filepath, base, bucket, prefix, storage_class, s3_client, dry_run=False, progress=None):
    """Upload a single file to S3 GIR and replace with a .s3arc stub containing metadata."""

    if filepath.endswith(STUB_EXT):
        return filepath, 0, True, "skip", "already stubbed"

    if not os.path.isfile(filepath):
        return filepath, 0, False, "skip", "not a regular file"

    file_size = os.path.getsize(filepath)
    if file_size == 0:
        return filepath, 0, True, "skip", "empty file"

    if not os.access(filepath, os.R_OK):
        return filepath, 0, False, "error", "no read permission"

    key = s3_key(filepath, base, prefix)
    stub_path = filepath + STUB_EXT

    try:
        file_stat = os.stat(filepath)
    except OSError as e:
        return filepath, 0, False, "error", f"cannot stat: {e}"

    # Ownership check: non-root can only archive their own files
    if os.getuid() != 0 and file_stat.st_uid != os.getuid():
        return filepath, 0, False, "error", f"ownership mismatch: file belongs to uid {file_stat.st_uid}, you are uid {os.getuid()} (run as root to archive other users' files)"

    if dry_run:
        return filepath, file_size, True, "would archive", f"-> s3://{bucket}/{key}"

    callback = None
    if progress:
        progress.set_current_file(filepath, file_stat.st_size)
        callback = progress.update_bytes

    try:
        s3_client.upload_file(
            filepath, bucket, key,
            ExtraArgs={
                "StorageClass": storage_class,
                "ChecksumAlgorithm": CHECKSUM_ALGORITHM,
                "Metadata": {
                    "original-mtime": str(file_stat.st_mtime),
                    "original-size": str(file_stat.st_size),
                    "original-mode": oct(file_stat.st_mode),
                    "original-uid": str(file_stat.st_uid),
                    "original-gid": str(file_stat.st_gid),
                }
            },
            Callback=callback
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "AccessDenied":
            return filepath, 0, False, "error", f"S3 upload failed: access denied. Verify your IAM policy includes s3:PutObject on this bucket."
        return filepath, 0, False, "error", f"S3 upload failed: {e}"
    except BotoCoreError as e:
        return filepath, 0, False, "error", f"S3 upload failed: {e}"

    try:
        head = s3_client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        remote_size = head["ContentLength"]
        if remote_size != file_stat.st_size:
            return filepath, 0, False, "error", f"size mismatch (local={file_stat.st_size}, remote={remote_size})"
        checksum = head.get("ChecksumSHA256", "")
    except (BotoCoreError, ClientError) as e:
        return filepath, 0, False, "error", f"upload verification failed: {e}"

    # Create stub with embedded JSON metadata
    stub_meta = {
        "type": "file",
        "s3key": key,
        "bucket": bucket,
        "storage_class": storage_class,
    }
    if checksum:
        stub_meta["checksum"] = checksum

    try:
        write_stub(stub_path, stub_meta)
    except OSError as e:
        try:
            os.remove(stub_path)
        except OSError:
            pass
        return filepath, 0, False, "error", f"cannot create stub: {e}"

    # Preserve metadata on stub
    try:
        os.utime(stub_path, (file_stat.st_atime, file_stat.st_mtime))
        os.chmod(stub_path, file_stat.st_mode)
    except OSError as e:
        print(f"Warning: cannot preserve metadata on stub {stub_path}: {e}", file=sys.stderr)

    try:
        os.remove(filepath)
    except OSError as e:
        return filepath, 0, False, "error", f"cannot remove original: {e}"

    if progress:
        progress.file_complete(file_stat.st_size)

    return filepath, file_stat.st_size, True, "stubbed", stub_path


def archive_aggregate(target_dir, config, dry_run=False, progress=None):
    """Archive directory using bottom-up recursive approach with in-place stubs."""
    if not os.path.isdir(target_dir):
        print(f"Error: {target_dir} is not a directory")
        return False

    # Ownership check: non-root must own all files in the tree
    if os.getuid() != 0:
        caller_uid = os.getuid()
        foreign = []
        for root, dirs, files in os.walk(target_dir):
            for f in files:
                fp = os.path.join(root, f)
                if f.endswith(STUB_EXT) or os.path.islink(fp):
                    continue
                try:
                    st = os.stat(fp)
                    if st.st_uid != caller_uid:
                        foreign.append((fp, st.st_uid))
                except OSError:
                    pass
        if foreign:
            print(f"Error: mixed ownership — {len(foreign)} file(s) owned by other users:")
            for fp, uid in foreign[:5]:
                print(f"  {fp} (uid {uid})")
            if len(foreign) > 5:
                print(f"  ... and {len(foreign) - 5} more")
            print("Run as root to archive directories with mixed ownership.")
            return False

    print(f"Starting recursive archive of: {target_dir}")
    success = archive_directory_recursive(target_dir, config, dry_run, depth=0)
    
    if success:
        if config["sns_topic"] and not dry_run:
            subject = "S3Arc: Recursive aggregate archive completed"
            message = f"Directory tree archived: {target_dir}"
            send_sns_notification(subject, message, config["sns_topic"])
        print(f"Recursive archive completed: {target_dir}")
    else:
        print(f"Recursive archive failed: {target_dir}")
    
    return success


def archive_directory_recursive(dir_path, config, dry_run=False, depth=0, max_depth=50):
    """Recursively archive directory from bottom-up, creating stubs at each level."""
    if depth > max_depth:
        print(f"Error: Maximum recursion depth ({max_depth}) exceeded at {dir_path}")
        return False
    
    if not os.path.isdir(dir_path):
        return False
    
    # Get directory contents
    try:
        entries = os.listdir(dir_path)
    except OSError as e:
        print(f"Error: cannot read directory {dir_path}: {e}")
        return False
    
    files = []
    subdirs = []
    
    for entry in entries:
        entry_path = os.path.join(dir_path, entry)
        if os.path.islink(entry_path):
            print(f"Warning: skipping symlink {entry_path}", file=sys.stderr)
        elif os.path.isfile(entry_path) and not entry.endswith(STUB_EXT):
            files.append(entry)
        elif os.path.isdir(entry_path):
            subdirs.append(entry)
    
    # First, recursively process all subdirectories (bottom-up)
    failed_subdirs = []
    for subdir in subdirs:
        subdir_path = os.path.join(dir_path, subdir)
        if not archive_directory_recursive(subdir_path, config, dry_run, depth + 1, max_depth):
            print(f"Warning: failed to archive subdirectory {subdir_path}")
            failed_subdirs.append(subdir)
    
    # Continue even if some subdirectories failed
    if failed_subdirs:
        print(f"Warning: {len(failed_subdirs)} subdirectories failed in {dir_path}")
    
    # Archive files in this directory (if any)
    if files:
        return archive_files_in_directory(dir_path, files, config, dry_run)
    else:
        print(f"No files to archive in: {dir_path}")
        return True


def archive_files_in_directory(dir_path, files, config, dry_run=False):
    """Archive files in a single directory, creating one archive stub."""
    # Calculate total size of files
    total_size = 0
    for file in files:
        try:
            total_size += os.path.getsize(os.path.join(dir_path, file))
        except OSError as e:
            print(f"Warning: cannot stat {os.path.join(dir_path, file)}: {e}", file=sys.stderr)
            continue
    
    if total_size == 0:
        print(f"No files to archive in: {dir_path}")
        return True
    
    # Check if compressed size would exceed 5TB (estimate 50% compression)
    estimated_compressed = total_size * 0.5
    if estimated_compressed > 5_000_000_000_000:  # 5TB
        print(f"Error: Files in {dir_path} too large (estimated {fmt_size(estimated_compressed)} compressed)")
        return False
    
    # Check available space (110% safety margin)
    free_space = shutil.disk_usage(tempfile.gettempdir()).free
    required_space = int(total_size * 1.1)
    
    if free_space < required_space:
        print(f"Error: Insufficient disk space for {dir_path}")
        print(f"Required: {fmt_size(required_space)}, Available: {fmt_size(free_space)}")
        return False
    
    dir_name = os.path.basename(dir_path.rstrip('/'))
    archive_name = f"{dir_name}_files.tar.gz"
    temp_archive_path = os.path.join(tempfile.gettempdir(), f"s3arc_{os.getpid()}_{archive_name}")
    archive_stub_path = os.path.join(dir_path, archive_name + STUB_EXT)
    # Use mount-point-relative path for S3 key
    mount_point = config.get("mount_point") or os.path.dirname(dir_path.rstrip('/'))
    rel_dir = os.path.relpath(dir_path, mount_point)
    s3_key = config["prefix"] + (rel_dir + "/" if rel_dir != "." else "") + archive_name
    
    if dry_run:
        compressed_est = total_size * 0.5
        print(f"Would archive files in: {dir_path} ({len(files)} files, {fmt_size(total_size)})")
        print(f"  Would create: {archive_stub_path}")
        print(f"  {format_cost_summary(compressed_est, config['storage_class'])} *")
        print(f"  * Cost estimate assumes 50% compression ratio")
        return True
    
    print(f"Archiving files in: {dir_path} ({len(files)} files, {fmt_size(total_size)})")
    
    # Create tar.gz archive of files only
    try:
        cmd = ['tar', 'czf', temp_archive_path, '-C', dir_path] + files
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error: tar compression failed for {dir_path}: {e.stderr}")
        return False
    except FileNotFoundError:
        print("Error: tar command not found")
        return False
    
    # Get compressed size
    try:
        compressed_size = os.path.getsize(temp_archive_path)
        compression_ratio = total_size / compressed_size if compressed_size > 0 else 1.0
    except OSError as e:
        print(f"Error: cannot determine compressed size for {dir_path}: {e}")
        try:
            os.remove(temp_archive_path)
        except OSError as e2:
            print(f"Warning: cannot remove temp archive {temp_archive_path}: {e2}", file=sys.stderr)
        return False
    
    print(f"Compressed: {fmt_size(total_size)} -> {fmt_size(compressed_size)} ({compression_ratio:.1f}x)")
    
    # Upload to S3
    s3_client = get_s3_client()
    
    manifest = {
        "directory": dir_name,
        "files": len(files),
        "file_list": files,
        "original_bytes": total_size,
        "compressed_bytes": compressed_size,
        "compression_ratio": round(compression_ratio, 2),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    
    print(f"Uploading: {s3_key} ({fmt_size(compressed_size)})")
    
    try:
        s3_client.upload_file(
            temp_archive_path, config["bucket"], s3_key,
            ExtraArgs={
                "StorageClass": config["storage_class"],
                "ChecksumAlgorithm": "SHA256",
                "Metadata": {
                    "directory": dir_name,
                    "file-count": str(len(files)),
                    "original-size": str(total_size),
                    "compressed-size": str(compressed_size),
                    "original-uid": str(os.getuid()),
                    "original-gid": str(os.getgid()),
                }
            }
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "AccessDenied":
            print(f"Error: S3 upload failed for {dir_path}: access denied. Verify your IAM policy includes s3:PutObject on this bucket.", file=sys.stderr)
        else:
            print(f"Error: S3 upload failed for {dir_path}: {e}", file=sys.stderr)
        try:
            os.remove(temp_archive_path)
        except OSError as e2:
            print(f"Warning: cannot remove temp archive {temp_archive_path}: {e2}", file=sys.stderr)
        return False
    except BotoCoreError as e:
        print(f"Error: S3 upload failed for {dir_path}: {e}", file=sys.stderr)
        try:
            os.remove(temp_archive_path)
        except OSError as e2:
            print(f"Warning: cannot remove temp archive {temp_archive_path}: {e2}", file=sys.stderr)
        return False
    
    # Verify upload
    try:
        head = s3_client.head_object(Bucket=config["bucket"], Key=s3_key)
        remote_size = head["ContentLength"]
        if remote_size != compressed_size:
            print(f"Error: upload size mismatch for {dir_path}")
            return False
        checksum = head.get("ChecksumSHA256", "")
    except (BotoCoreError, ClientError) as e:
        print(f"Error: upload verification failed for {dir_path}: {e}")
        return False
    
    print(f"Uploaded: verified ({fmt_size(remote_size)})")
    
    # Generate local manifest file from tar archive
    manifest_path = archive_stub_path.replace('.s3arc', '.manifest')
    try:
        print(f"Generating manifest: {os.path.basename(manifest_path)}")
        cmd = ['tar', '-tvzf', temp_archive_path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        with open(manifest_path, 'w') as f:
            f.write(result.stdout)
        
        print(f"Manifest created: {len(result.stdout.splitlines())} files listed")
        
    except subprocess.CalledProcessError as e:
        print(f"Warning: could not generate manifest: {e.stderr}")
        # Continue without manifest - not critical
    except OSError as e:
        print(f"Warning: could not write manifest file: {e}")
        # Continue without manifest - not critical
    
    # Create archive stub with embedded JSON metadata
    try:
        stub_meta = {
            "type": "aggregate",
            "s3key": s3_key,
            "bucket": config["bucket"],
            "storage_class": config["storage_class"],
            "manifest": manifest,
        }
        if checksum:
            stub_meta["checksum"] = checksum
        write_stub(archive_stub_path, stub_meta)
    except OSError as e:
        print(f"Error: cannot create archive stub: {e}")
        return False
    
    # Remove original files only after stub is created successfully
    failed_removals = []
    for file in files:
        try:
            os.remove(os.path.join(dir_path, file))
        except OSError as e:
            failed_removals.append(file)
            print(f"Warning: could not remove original file {file}: {e}")
    
    if not failed_removals:
        print(f"Stubbed: {archive_stub_path} (archived {len(files)} files)")
    else:
        print(f"Stubbed: {archive_stub_path} ({len(failed_removals)} removal failures)")
    
    # Clean up temp archive
    try:
        os.remove(temp_archive_path)
    except OSError as e:
        print(f"Warning: cannot remove temp archive {temp_archive_path}: {e}", file=sys.stderr)
    
    return True


def collect_files(target):
    """Collect files to process and calculate totals."""
    files = []
    total_bytes = 0
    if os.path.isfile(target):
        if not target.endswith(STUB_EXT):
            size = os.path.getsize(target)
            files.append((target, size))
            total_bytes += size
    elif os.path.isdir(target):
        for root, _, filenames in os.walk(target):
            for fname in sorted(filenames):
                fpath = os.path.join(root, fname)
                if os.path.islink(fpath):
                    print(f"Warning: skipping symlink {fpath}", file=sys.stderr)
                elif os.path.isfile(fpath) and not fpath.endswith(STUB_EXT):
                    size = os.path.getsize(fpath)
                    files.append((fpath, size))
                    total_bytes += size
    return files, total_bytes


def main():
    parser = argparse.ArgumentParser(description="Archive files to S3 Glacier, leave .s3arc stubs")
    parser.add_argument("--version", action="version", version=f"s3archive {VERSION}")
    parser.add_argument("target", help="File or directory to archive")
    parser.add_argument("--aggregate", action="store_true", help="Archive entire directory as single compressed archive")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be archived without doing it")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress indicator")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel upload workers (default: {DEFAULT_WORKERS})")
    tier_group = parser.add_mutually_exclusive_group()
    tier_group.add_argument("-online", action="store_true", help="Archive to Glacier Instant Retrieval (default)")
    tier_group.add_argument("-offline", action="store_true", help="Archive to Glacier Deep Archive")
    args = parser.parse_args()

    if not os.path.exists(args.target):
        print(f"ERROR: path not found: {args.target}", file=sys.stderr)
        sys.exit(1)

    # Validate we can write stub files
    if not args.dry_run:
        print("Validating filesystem write support...")
        if not validate_write_support(args.target):
            sys.exit(1)
        print("✓ Filesystem write support confirmed\n")

    # Get configuration from FSx tags or environment variables
    config = get_config_from_fsx(args.target)
    
    # Override storage class from command line args
    if args.offline:
        config["storage_class"] = "DEEP_ARCHIVE"
    elif args.online:
        config["storage_class"] = "GLACIER_IR"
    
    if not config["bucket"]:
        print("ERROR: No archive bucket configured.", file=sys.stderr)
        print("  Set 'ArchiveBucket' tag on FSx filesystem, or", file=sys.stderr)
        print("  Set S3ARC_BUCKET environment variable.", file=sys.stderr)
        sys.exit(1)
    
    print(f"Using bucket: {config['bucket']} (from {config['source']})")
    print(f"  Prefix: {config['prefix']}")
    print(f"  Storage class: {config['storage_class']}")

    # Verify credentials
    get_s3_client()

    # Verify bucket exists
    try:
        boto3.client("s3").head_bucket(Bucket=config["bucket"])
    except (BotoCoreError, ClientError) as e:
        print(f"ERROR: cannot access bucket '{config['bucket']}': {e}", file=sys.stderr)
        sys.exit(1)

    # Use mount point as base for S3 key paths (full filesystem path in key)
    base = config.get("mount_point") or os.path.dirname(args.target.rstrip("/"))

    # Handle aggregate mode
    if args.aggregate:
        if not os.path.isdir(args.target):
            print("ERROR: --aggregate requires a directory", file=sys.stderr)
            sys.exit(1)
        
        success = archive_aggregate(args.target, config, args.dry_run)
        if not success:
            sys.exit(1)
        
        sys.exit(0)

    # Collect files and totals for per-file mode
    files, total_bytes = collect_files(args.target)

    if not files:
        print("No files to archive.")
        sys.exit(0)

    if args.dry_run:
        print(f"Dry run: {len(files)} files, {fmt_size(total_bytes)} total")
        print(f"  Storage class: {config['storage_class']}")
        print(f"  {format_cost_summary(total_bytes, config['storage_class'])}\n")

    # Set up progress tracker
    progress = None
    if not args.dry_run and not args.no_progress and sys.stdout.isatty():
        progress = ProgressTracker(len(files), total_bytes)

    success = 0
    failed = 0
    archived_bytes = 0

    # Use thread pool for parallel uploads
    s3_client = get_s3_client()
    num_workers = min(args.workers, len(files))
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(stub_file, fpath, base, config["bucket"], config["prefix"], 
                          config["storage_class"], s3_client, args.dry_run, progress): fpath
            for fpath, _ in files
        }
        for future in as_completed(futures):
            fpath, fsize, ok, status, detail = future.result()
            if ok:
                success += 1
                if status == "stubbed":
                    archived_bytes += fsize
            else:
                failed += 1
            if args.dry_run or args.no_progress or not sys.stdout.isatty():
                print(f"  {status}: {fpath} ({detail})")

    if progress:
        progress.finish()

    # Summary
    cost_summary = format_cost_summary(total_bytes if args.dry_run else archived_bytes, config["storage_class"])
    print(f"\nComplete: {success} archived, {failed} failed")
    print(f"  Space freed: {fmt_size(archived_bytes)}")
    print(f"  {cost_summary}")

    # Send SNS notification
    if not args.dry_run and config["sns_topic"]:
        send_sns_notification(
            subject=f"S3Arc Archive Complete: {args.target}",
            message=(f"Archived {success} files from {args.target}\n"
                     f"Space freed: {fmt_size(archived_bytes)}\n"
                     f"{cost_summary}\n"
                     f"Failed: {failed}"),
            sns_topic=config["sns_topic"]
        )

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
