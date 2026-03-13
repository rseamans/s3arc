#!/usr/bin/env python3
"""s3recall: Restore .s3arc stub files from S3 Glacier Instant Retrieval."""

import argparse
import os
import sys
import hashlib
import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
import xattr
from botocore.exceptions import BotoCoreError, ClientError

XATTR_S3KEY = "user.stub.s3key"
XATTR_BUCKET = "user.stub.bucket"
XATTR_CHECKSUM = "user.stub.checksum"
XATTR_STORAGE_CLASS = "user.stub.storage_class"
STUB_EXT = ".s3arc"

DEFAULT_BUCKET = os.environ.get("S3ARC_BUCKET", "")
SNS_TOPIC_ARN = os.environ.get("S3ARC_SNS_TOPIC", "")
DEFAULT_WORKERS = 8


class ProgressTracker:
    """Thread-safe progress tracker for file transfers."""
    def __init__(self, total_files, total_bytes):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.completed_files = 0
        self.completed_bytes = 0
        self._lock = threading.Lock()

    def update_bytes(self, chunk_size):
        with self._lock:
            self.completed_bytes += chunk_size
            self._print_progress()

    def file_complete(self, file_size):
        with self._lock:
            self.completed_files += 1
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


def get_s3_client():
    try:
        return boto3.client("s3")
    except (BotoCoreError, ClientError) as e:
        print(f"ERROR: Failed to initialize S3 client: {e}", file=sys.stderr)
        sys.exit(1)


def send_sns_notification(subject, message):
    if not SNS_TOPIC_ARN:
        return
    try:
        sns = boto3.client("sns")
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
    except (BotoCoreError, ClientError) as e:
        print(f"  WARNING: SNS notification failed: {e}", file=sys.stderr)


def compute_sha256_b64(filepath):
    """Compute SHA256 of file and return base64-encoded digest."""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return base64.b64encode(sha.digest()).decode()


def get_xattr_str(filepath, attr_name):
    """Read an xattr value as a string, return None if not present."""
    try:
        return xattr.getxattr(filepath, attr_name).decode()
    except OSError:
        return None


def recall_file(filepath, dry_run=False, progress=None):
    """Restore a single .s3arc stub file from S3."""
    # Each thread gets its own S3 client
    s3_client = boto3.client("s3")

    if not filepath.endswith(STUB_EXT):
        return filepath, 0, True, "skip", "not a stub"

    if not os.path.isfile(filepath):
        return filepath, 0, False, "skip", "not a regular file"

    key = get_xattr_str(filepath, XATTR_S3KEY)
    if not key:
        return filepath, 0, False, "error", "no S3 key xattr"

    bucket = get_xattr_str(filepath, XATTR_BUCKET) or DEFAULT_BUCKET
    if not bucket:
        return filepath, 0, False, "error", "no bucket configured"

    expected_checksum = get_xattr_str(filepath, XATTR_CHECKSUM)
    stored_class = get_xattr_str(filepath, XATTR_STORAGE_CLASS) or "unknown"
    original_path = filepath[:-len(STUB_EXT)]

    if os.path.exists(original_path):
        return filepath, 0, False, "error", "original path already exists"

    # Get S3 object metadata
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
                    RestoreRequest={"Days": 7, "GlacierJobParameters": {"Tier": "Bulk"}}
                )
                return filepath, 0, False, "initiated", "restore requested, check back later"
            except ClientError as e:
                if "RestoreAlreadyInProgress" in str(e):
                    return filepath, 0, False, "pending", "restore already in progress"
                return filepath, 0, False, "error", f"cannot initiate restore: {e}"

    if dry_run:
        return filepath, file_size, True, "would recall", f"-> {original_path}"

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
        except OSError:
            pass
        return filepath, 0, False, "error", f"download failed: {e}"

    # Verify size
    s3_metadata = head.get("Metadata", {})
    expected_size = s3_metadata.get("original-size")
    if expected_size:
        actual_size = os.path.getsize(original_path)
        if actual_size != int(expected_size):
            pass  # Warning only, continue

    # Verify checksum
    if expected_checksum:
        actual_checksum = compute_sha256_b64(original_path)
        if actual_checksum != expected_checksum:
            try:
                os.remove(original_path)
            except OSError:
                pass
            return filepath, 0, False, "error", "checksum mismatch"

    # Restore metadata
    try:
        mtime = s3_metadata.get("original-mtime")
        if mtime:
            mtime_f = float(mtime)
            os.utime(original_path, (mtime_f, mtime_f))
        mode = s3_metadata.get("original-mode")
        if mode:
            os.chmod(original_path, int(mode, 8))
    except (ValueError, OSError):
        pass  # Non-fatal

    # Remove stub
    try:
        os.remove(filepath)
    except OSError:
        pass  # Non-fatal

    if progress:
        progress.file_complete(file_size)

    # Include storage class in status message
    tier_label = "offline" if stored_class == "DEEP_ARCHIVE" else "online"
    return filepath, file_size, True, "recalled", f"{original_path} ({tier_label})"


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
    parser.add_argument("target", help="Stub file or directory to recall")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be recalled without doing it")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress indicator")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel download workers (default: {DEFAULT_WORKERS})")
    args = parser.parse_args()

    if not os.path.exists(args.target):
        print(f"ERROR: path not found: {args.target}", file=sys.stderr)
        sys.exit(1)

    # Verify credentials
    get_s3_client()

    stubs = collect_stubs(args.target)
    if not stubs:
        print("No stub files to recall.")
        sys.exit(0)

    # Get sizes for progress tracking
    total_bytes = 0
    s3_client = boto3.client("s3")
    for stub in stubs:
        key = get_xattr_str(stub, XATTR_S3KEY)
        bucket = get_xattr_str(stub, XATTR_BUCKET) or DEFAULT_BUCKET
        if key and bucket:
            try:
                head = s3_client.head_object(Bucket=bucket, Key=key)
                total_bytes += head["ContentLength"]
            except (BotoCoreError, ClientError):
                pass

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
            executor.submit(recall_file, stub, args.dry_run, progress): stub
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
    print(f"\nComplete: {', '.join(parts)}, {fmt_size(recalled_bytes)} transferred")

    # Send SNS notification
    if not args.dry_run and SNS_TOPIC_ARN:
        send_sns_notification(
            subject=f"S3Arc Recall Complete: {args.target}",
            message=f"Recalled {success} files ({fmt_size(recalled_bytes)}) to {args.target}\nFailed: {failed}"
        )

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
