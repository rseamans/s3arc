#!/usr/bin/env python3
"""s3archive: Move files to S3 Glacier Instant Retrieval, leave zero-byte .s3arc stubs with xattrs."""

import argparse
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
import xattr
from botocore.exceptions import BotoCoreError, ClientError

S3_PREFIX = os.environ.get("S3ARC_PREFIX", "archived/")
STORAGE_CLASS = os.environ.get("S3ARC_STORAGE_CLASS", "GLACIER_IR")
SNS_TOPIC_ARN = os.environ.get("S3ARC_SNS_TOPIC", "")
XATTR_S3KEY = "user.stub.s3key"
XATTR_BUCKET = "user.stub.bucket"
XATTR_CHECKSUM = "user.stub.checksum"
XATTR_STORAGE_CLASS = "user.stub.storage_class"
STUB_EXT = ".s3arc"
CHECKSUM_ALGORITHM = "SHA256"
DEFAULT_WORKERS = 8
FSX_TAG_NAME = "ArchiveBucket"

# S3 storage costs per GB/month (us-east-1 commercial, approximate)
STORAGE_COSTS = {
    "STANDARD": 0.023,
    "STANDARD_IA": 0.0125,
    "GLACIER_IR": 0.004,
    "GLACIER": 0.0036,
    "DEEP_ARCHIVE": 0.00099,
}


class ProgressTracker:
    """Thread-safe progress tracker for file transfers."""
    def __init__(self, total_files, total_bytes):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.completed_files = 0
        self.completed_bytes = 0
        self.current_file = ""
        self.current_file_size = 0
        self.current_file_bytes = 0
        self._lock = threading.Lock()

    def set_current_file(self, filepath, size):
        with self._lock:
            self.current_file = os.path.basename(filepath)
            self.current_file_size = size
            self.current_file_bytes = 0

    def update_bytes(self, chunk_size):
        with self._lock:
            self.current_file_bytes += chunk_size
            self.completed_bytes += chunk_size
            self._print_progress()

    def file_complete(self, file_size):
        with self._lock:
            self.completed_files += 1
            # Ensure completed_bytes accounts for full file
            self._print_progress()

    def _print_progress(self):
        pct = (self.completed_bytes / self.total_bytes * 100) if self.total_bytes else 0
        line = (f"\r[{self.completed_files}/{self.total_files} files] "
                f"{fmt_size(self.completed_bytes)} / {fmt_size(self.total_bytes)} ({pct:.0f}%)")
        sys.stdout.write(f"{line:<80}")
        sys.stdout.flush()

    def finish(self):
        sys.stdout.write("\n")
        sys.stdout.flush()


def fmt_size(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def estimate_monthly_cost(nbytes, storage_class):
    """Estimate monthly storage cost for given bytes and storage class."""
    gb = nbytes / (1024 ** 3)
    rate = STORAGE_COSTS.get(storage_class, STORAGE_COSTS["GLACIER_IR"])
    return gb * rate


def get_s3_client():
    try:
        return boto3.client("s3")
    except (BotoCoreError, ClientError) as e:
        print(f"ERROR: Failed to initialize S3 client: {e}", file=sys.stderr)
        sys.exit(1)


def get_fsx_id_from_mount(target_path):
    """Find FSx filesystem ID from mount point containing target path."""
    target_path = os.path.abspath(target_path)
    
    try:
        # Read mount info
        if os.path.exists("/proc/mounts"):
            with open("/proc/mounts", "r") as f:
                mounts = f.read()
        else:
            # macOS fallback
            result = subprocess.run(["mount"], capture_output=True, text=True)
            mounts = result.stdout
    except Exception:
        return None, None
    
    # Find longest matching mount point
    best_mount = None
    best_len = 0
    best_source = None
    
    for line in mounts.strip().split("\n"):
        parts = line.split()
        if len(parts) < 2:
            continue
        source = parts[0]
        mount_point = parts[1] if os.path.exists("/proc/mounts") else parts[2]
        
        if target_path.startswith(mount_point) and len(mount_point) > best_len:
            best_mount = mount_point
            best_len = len(mount_point)
            best_source = source
    
    if not best_source:
        return None, None
    
    # Extract FSx filesystem ID from mount source
    # ONTAP: svm-xxx.fs-xxx.fsx.region.amazonaws.com:/vol
    # Lustre: fs-xxx.fsx.region.amazonaws.com@tcp:/mountname
    
    # ONTAP pattern - also extract region
    ontap_match = re.search(r'(fs-[a-f0-9]+)\.fsx\.([a-z0-9-]+)\.amazonaws\.com', best_source)
    if ontap_match:
        return ontap_match.group(1), "ONTAP", ontap_match.group(2)
    
    # Lustre pattern
    lustre_match = re.search(r'(fs-[a-f0-9]+)\.fsx\.([a-z0-9-]+)\.amazonaws\.com', best_source)
    if lustre_match:
        return lustre_match.group(1), "LUSTRE", lustre_match.group(2)
    
    return None, None, None


def get_bucket_from_fsx_tag(fsx_id, fsx_type, region=None):
    """Get ArchiveBucket tag value from FSx filesystem."""
    if not fsx_id:
        return None
    
    try:
        fsx = boto3.client("fsx", region_name=region) if region else boto3.client("fsx")
        
        # Get filesystem ARN first
        response = fsx.describe_file_systems(FileSystemIds=[fsx_id])
        
        if not response.get("FileSystems"):
            return None
        
        fs = response["FileSystems"][0]
        resource_arn = fs.get("ResourceARN")
        
        if not resource_arn:
            return None
        
        # Get tags
        tags_response = fsx.list_tags_for_resource(ResourceARN=resource_arn)
        for tag in tags_response.get("Tags", []):
            if tag.get("Key") == FSX_TAG_NAME:
                return tag.get("Value")
        
        return None
    except (BotoCoreError, ClientError):
        return None


def get_archive_bucket(target_path):
    """Determine archive bucket from FSx tag or environment variable."""
    # Try FSx tag first
    fsx_id, fsx_type, region = get_fsx_id_from_mount(target_path)
    if fsx_id:
        bucket = get_bucket_from_fsx_tag(fsx_id, fsx_type, region)
        if bucket:
            return bucket, f"FSx tag ({fsx_id})"
    
    # Fall back to environment variable
    env_bucket = os.environ.get("S3ARC_BUCKET")
    if env_bucket:
        return env_bucket, "S3ARC_BUCKET env var"
    
    # No bucket found
    return None, None


def send_sns_notification(subject, message):
    if not SNS_TOPIC_ARN:
        return
    try:
        sns = boto3.client("sns")
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
    except (BotoCoreError, ClientError) as e:
        print(f"  WARNING: SNS notification failed: {e}", file=sys.stderr)


def s3_key(filepath, base):
    return S3_PREFIX + os.path.relpath(filepath, base)


def stub_file(filepath, base, dry_run=False, progress=None):
    """Upload a single file to S3 GIR and replace with a zero-byte .s3arc stub."""
    # Each thread gets its own S3 client
    s3_client = boto3.client("s3")

    if filepath.endswith(STUB_EXT):
        return filepath, 0, True, "skip", "already stubbed"

    if not os.path.isfile(filepath):
        return filepath, 0, False, "skip", "not a regular file"

    file_size = os.path.getsize(filepath)
    if file_size == 0:
        return filepath, 0, True, "skip", "empty file"

    if not os.access(filepath, os.R_OK):
        return filepath, 0, False, "error", "no read permission"

    key = s3_key(filepath, base)
    stub_path = filepath + STUB_EXT

    try:
        file_stat = os.stat(filepath)
    except OSError as e:
        return filepath, 0, False, "error", f"cannot stat: {e}"

    if dry_run:
        return filepath, file_size, True, "would archive", f"-> s3://{S3_BUCKET}/{key}"

    # Set up progress callback
    callback = None
    if progress:
        progress.set_current_file(filepath, file_stat.st_size)
        callback = progress.update_bytes

    # Upload to S3 with checksum
    try:
        s3_client.upload_file(
            filepath, S3_BUCKET, key,
            ExtraArgs={
                "StorageClass": STORAGE_CLASS,
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
    except (BotoCoreError, ClientError) as e:
        return filepath, 0, False, "error", f"S3 upload failed: {e}"

    # Verify upload and get checksum
    try:
        head = s3_client.head_object(Bucket=S3_BUCKET, Key=key, ChecksumMode="ENABLED")
        remote_size = head["ContentLength"]
        if remote_size != file_stat.st_size:
            return filepath, 0, False, "error", f"size mismatch (local={file_stat.st_size}, remote={remote_size})"
        checksum = head.get("ChecksumSHA256", "")
    except (BotoCoreError, ClientError) as e:
        return filepath, 0, False, "error", f"upload verification failed: {e}"

    # Create zero-byte stub
    try:
        with open(stub_path, "w"):
            pass
    except OSError as e:
        return filepath, 0, False, "error", f"cannot create stub: {e}"

    # Set xattrs
    try:
        xattr.setxattr(stub_path, XATTR_S3KEY, key.encode())
        xattr.setxattr(stub_path, XATTR_BUCKET, S3_BUCKET.encode())
        xattr.setxattr(stub_path, XATTR_STORAGE_CLASS, STORAGE_CLASS.encode())
        if checksum:
            xattr.setxattr(stub_path, XATTR_CHECKSUM, checksum.encode())
    except OSError as e:
        try:
            os.remove(stub_path)
        except OSError:
            pass
        return filepath, 0, False, "error", f"cannot set xattr: {e}"

    # Preserve metadata on stub
    try:
        os.utime(stub_path, (file_stat.st_atime, file_stat.st_mtime))
        os.chmod(stub_path, file_stat.st_mode)
    except OSError:
        pass  # Non-fatal

    # Remove original
    try:
        os.remove(filepath)
    except OSError as e:
        return filepath, 0, False, "error", f"cannot remove original: {e}"

    if progress:
        progress.file_complete(file_stat.st_size)

    return filepath, file_stat.st_size, True, "stubbed", stub_path


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
                if os.path.isfile(fpath) and not os.path.islink(fpath) and not fpath.endswith(STUB_EXT):
                    size = os.path.getsize(fpath)
                    files.append((fpath, size))
                    total_bytes += size
    return files, total_bytes


def main():
    parser = argparse.ArgumentParser(description="Archive files to S3 Glacier, leave .s3arc stubs")
    parser.add_argument("target", help="File or directory to archive")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be archived without doing it")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress indicator")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel upload workers (default: {DEFAULT_WORKERS})")
    tier_group = parser.add_mutually_exclusive_group()
    tier_group.add_argument("-online", action="store_true", help="Archive to Glacier Instant Retrieval (default)")
    tier_group.add_argument("-offline", action="store_true", help="Archive to Glacier Deep Archive")
    args = parser.parse_args()

    # Determine storage class
    global STORAGE_CLASS
    if args.offline:
        STORAGE_CLASS = "DEEP_ARCHIVE"
    elif args.online:
        STORAGE_CLASS = "GLACIER_IR"
    # else use env var or default

    if not os.path.exists(args.target):
        print(f"ERROR: path not found: {args.target}", file=sys.stderr)
        sys.exit(1)

    # Determine archive bucket (FSx tag first, then env var)
    global S3_BUCKET
    S3_BUCKET, bucket_source = get_archive_bucket(args.target)
    if not S3_BUCKET:
        print("ERROR: No archive bucket configured.", file=sys.stderr)
        print("  Set 'ArchiveBucket' tag on FSx filesystem, or", file=sys.stderr)
        print("  Set S3ARC_BUCKET environment variable.", file=sys.stderr)
        sys.exit(1)
    
    print(f"Using bucket: {S3_BUCKET} (from {bucket_source})")

    # Verify credentials
    get_s3_client()

    # Verify bucket exists
    try:
        boto3.client("s3").head_bucket(Bucket=S3_BUCKET)
    except (BotoCoreError, ClientError) as e:
        print(f"ERROR: cannot access bucket '{S3_BUCKET}': {e}", file=sys.stderr)
        sys.exit(1)

    base = os.path.dirname(args.target.rstrip("/"))

    # Collect files and totals
    files, total_bytes = collect_files(args.target)

    if not files:
        print("No files to archive.")
        sys.exit(0)

    if args.dry_run:
        monthly_cost = estimate_monthly_cost(total_bytes, STORAGE_CLASS)
        print(f"Dry run: {len(files)} files, {fmt_size(total_bytes)} total")
        print(f"  Storage class: {STORAGE_CLASS}")
        print(f"  Est. monthly cost: ${monthly_cost:.2f}\n")

    # Set up progress tracker
    progress = None
    if not args.dry_run and not args.no_progress and sys.stdout.isatty():
        progress = ProgressTracker(len(files), total_bytes)

    success = 0
    failed = 0
    archived_bytes = 0

    # Use thread pool for parallel uploads
    num_workers = min(args.workers, len(files))
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(stub_file, fpath, base, args.dry_run, progress): fpath
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
    monthly_cost = estimate_monthly_cost(archived_bytes, STORAGE_CLASS)
    print(f"\nComplete: {success} archived, {failed} failed")
    print(f"  Space freed: {fmt_size(archived_bytes)}")
    print(f"  Storage class: {STORAGE_CLASS}")
    print(f"  Est. monthly cost: ${monthly_cost:.2f}")

    # Send SNS notification
    if not args.dry_run and SNS_TOPIC_ARN:
        send_sns_notification(
            subject=f"S3Arc Archive Complete: {args.target}",
            message=(f"Archived {success} files from {args.target}\n"
                     f"Space freed: {fmt_size(archived_bytes)}\n"
                     f"Storage class: {STORAGE_CLASS}\n"
                     f"Est. monthly cost: ${monthly_cost:.2f}\n"
                     f"Failed: {failed}")
        )

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
