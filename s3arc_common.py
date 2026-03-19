#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared utilities for S3Arc archive, recall, and listing tools."""

import json
import os
import re
import subprocess
import sys
import threading

import boto3
from botocore.exceptions import BotoCoreError, ClientError

STUB_EXT = ".s3arc"
CHECKSUM_ALGORITHM = "SHA256"

VERSION = "0.9"

# FSx tag names
FSX_TAG_ARCHIVE_BUCKET = "ArchiveBucket"
FSX_TAG_STORAGE_CLASS = "StorageClass"
FSX_TAG_SNS_TOPIC = "SnsTopicArn"
FSX_TAG_RESTORE_DAYS = "RestoreDays"
FSX_TAG_RESTORE_TIER = "RestoreTier"

DEFAULT_STORAGE_CLASS = "GLACIER_IR"
DEFAULT_RESTORE_DAYS = 7
DEFAULT_RESTORE_TIER = "Standard"

# S3 storage costs per GB/month (us-east-1 commercial, approximate)
STORAGE_COSTS = {
    "STANDARD": 0.023,
    "STANDARD_IA": 0.0125,
    "GLACIER_IR": 0.004,
    "GLACIER": 0.0036,
    "DEEP_ARCHIVE": 0.00099,
}


def fmt_size(nbytes):
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def get_s3_client():
    """Create and return an S3 client, exit on failure."""
    try:
        return boto3.client("s3")
    except (BotoCoreError, ClientError) as e:
        print(f"ERROR: Failed to initialize S3 client: {e}", file=sys.stderr)
        sys.exit(1)


def read_stub(filepath):
    """Read stub metadata from a .s3arc JSON file. Returns dict or empty dict on failure."""
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Warning: cannot read stub {filepath}: {e}", file=sys.stderr)
        return {}


def write_stub(stub_path, metadata):
    """Write stub metadata as JSON to a .s3arc file."""
    with open(stub_path, "w") as f:
        json.dump(metadata, f)


def send_sns_notification(subject, message, sns_topic):
    """Send an SNS notification. Silently fails with warning."""
    if not sns_topic:
        return
    try:
        region = sns_topic.split(":")[3] if sns_topic.startswith("arn:") else None
        sns = boto3.client("sns", region_name=region) if region else boto3.client("sns")
        sns.publish(TopicArn=sns_topic, Subject=subject, Message=message)
    except (BotoCoreError, ClientError) as e:
        print(f"  WARNING: SNS notification failed: {e}", file=sys.stderr)


def get_fsx_id_from_mount(target_path):
    """Find FSx filesystem ID, type, region, and mount point from target path.

    Returns (fsx_id, fsx_type, region, mount_point) or (None, None, None, None).
    """
    target_path = os.path.abspath(target_path)

    try:
        if os.path.exists("/proc/mounts"):
            with open("/proc/mounts", "r") as f:
                mounts = f.read()
        else:
            result = subprocess.run(["mount"], capture_output=True, text=True)
            mounts = result.stdout
    except Exception as e:
        print(f"Warning: cannot read mount information: {e}", file=sys.stderr)
        return None, None, None, None

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
        return None, None, None, None

    # ONTAP: svm-xxx.fs-xxx.fsx.region.amazonaws.com:/vol
    # Lustre: fs-xxx.fsx.region.amazonaws.com@tcp:/mountname

    # ONTAP pattern — has svm-xxx before fs-xxx
    ontap_match = re.search(r'svm-[a-f0-9]+\.(fs-[a-f0-9]+)\.fsx\.([a-z0-9-]+)\.amazonaws\.com', best_source)
    if ontap_match:
        return ontap_match.group(1), "ONTAP", ontap_match.group(2), best_mount

    # Lustre pattern (DNS-based) — fs-xxx directly, no svm- prefix
    lustre_match = re.search(r'(fs-[a-f0-9]+)\.fsx\.([a-z0-9-]+)\.amazonaws\.com', best_source)
    if lustre_match:
        return lustre_match.group(1), "LUSTRE", lustre_match.group(2), best_mount

    # Lustre IP-based mount (e.g., 10.0.1.191@tcp:/mountname)
    ip_match = re.match(r'(\d+\.\d+\.\d+\.\d+)@tcp:', best_source)
    if ip_match:
        mount_ip = ip_match.group(1)
        try:
            import socket
            session = boto3.session.Session()
            region = session.region_name or "us-east-1"
            fsx = boto3.client("fsx", region_name=region)
            for fs in fsx.describe_file_systems().get("FileSystems", []):
                if fs.get("FileSystemType") == "LUSTRE":
                    dns_name = fs.get("DNSName", "")
                    if dns_name:
                        try:
                            if socket.gethostbyname(dns_name) == mount_ip:
                                fsx_id = fs["FileSystemId"]
                                fs_region = fs["ResourceARN"].split(":")[3]
                                return fsx_id, "LUSTRE", fs_region, best_mount
                        except socket.gaierror:
                            pass
        except (BotoCoreError, ClientError) as e:
            print(f"Warning: cannot query FSx for Lustre IP match: {e}", file=sys.stderr)

    return None, None, None, None


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
