# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for S3Arc.

Run:  python -m pytest tests/ -v
Deps: pip install pytest

These tests exercise pure logic with no AWS credentials or FSx mounts required.
Boto3 calls are mocked where needed.
"""

import json
import os
import sys
import tempfile
import threading
from unittest import mock

import pytest

# Add project root to path so imports work from tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# s3arc_common tests
# ---------------------------------------------------------------------------

class TestFmtSize:
    def test_bytes(self):
        from s3arc_common import fmt_size
        assert fmt_size(0) == "0.0 B"
        assert fmt_size(512) == "512.0 B"

    def test_kilobytes(self):
        from s3arc_common import fmt_size
        assert fmt_size(1024) == "1.0 KB"
        assert fmt_size(1536) == "1.5 KB"

    def test_megabytes(self):
        from s3arc_common import fmt_size
        assert fmt_size(5 * 1024 * 1024) == "5.0 MB"

    def test_terabytes(self):
        from s3arc_common import fmt_size
        assert fmt_size(2 * 1024 ** 4) == "2.0 TB"

    def test_petabytes(self):
        from s3arc_common import fmt_size
        assert fmt_size(3 * 1024 ** 5) == "3.0 PB"


class TestStubReadWrite:
    def test_roundtrip(self, tmp_path):
        from s3arc_common import read_stub, write_stub
        stub = tmp_path / "file.txt.s3arc"
        meta = {
            "type": "file",
            "s3key": "FSxONTAP/fs-abc123/data/file.txt",
            "bucket": "my-bucket",
            "storage_class": "GLACIER_IR",
            "checksum": "abc123==",
        }
        write_stub(str(stub), meta)
        result = read_stub(str(stub))
        assert result == meta

    def test_read_missing_file(self, tmp_path, capsys):
        from s3arc_common import read_stub
        result = read_stub(str(tmp_path / "nonexistent.s3arc"))
        assert result == {}
        assert "Warning" in capsys.readouterr().err

    def test_read_invalid_json(self, tmp_path, capsys):
        from s3arc_common import read_stub
        bad = tmp_path / "bad.s3arc"
        bad.write_text("not json{{{")
        result = read_stub(str(bad))
        assert result == {}
        assert "Warning" in capsys.readouterr().err


class TestConstants:
    def test_version_format(self):
        from s3arc_common import VERSION
        assert VERSION == "0.9"

    def test_stub_ext(self):
        from s3arc_common import STUB_EXT
        assert STUB_EXT == ".s3arc"

    def test_storage_costs_has_glacier_ir(self):
        from s3arc_common import STORAGE_COSTS, DEFAULT_STORAGE_CLASS
        assert DEFAULT_STORAGE_CLASS in STORAGE_COSTS


class TestProgressTracker:
    def test_progress_tracking(self):
        from s3arc_common import ProgressTracker
        pt = ProgressTracker(total_files=2, total_bytes=1000)
        pt.set_current_file("/tmp/file1.txt", 500)
        pt.update_bytes(250)
        assert pt.completed_bytes == 250
        assert pt.completed_files == 0
        pt.file_complete(500)
        assert pt.completed_files == 1

    def test_thread_safety(self):
        from s3arc_common import ProgressTracker
        pt = ProgressTracker(total_files=100, total_bytes=10000)
        def worker():
            for _ in range(100):
                pt.update_bytes(1)
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert pt.completed_bytes == 1000


# ---------------------------------------------------------------------------
# s3archive tests
# ---------------------------------------------------------------------------

class TestS3Key:
    def test_basic_key(self):
        from s3archive import s3_key
        result = s3_key("/mnt/fsx/data/file.txt", "/mnt/fsx", "FSxONTAP/fs-abc/")
        assert result == "FSxONTAP/fs-abc/data/file.txt"

    def test_nested_path(self):
        from s3archive import s3_key
        result = s3_key("/mnt/fsx/a/b/c/file.txt", "/mnt/fsx", "FSxLustre/fs-def/")
        assert result == "FSxLustre/fs-def/a/b/c/file.txt"

    def test_no_leading_slash(self):
        from s3archive import s3_key
        result = s3_key("/mnt/fsx/file.txt", "/mnt/fsx", "FSxONTAP/fs-abc/")
        assert not result.startswith("/")


class TestEstimateMonthlyCost:
    def test_zero_bytes(self):
        from s3archive import estimate_monthly_cost
        assert estimate_monthly_cost(0, "GLACIER_IR") == 0.0

    def test_one_gb_glacier_ir(self):
        from s3archive import estimate_monthly_cost
        cost = estimate_monthly_cost(1024 ** 3, "GLACIER_IR")
        assert abs(cost - 0.004) < 1e-9

    def test_unknown_class_defaults_to_glacier_ir(self):
        from s3archive import estimate_monthly_cost
        cost = estimate_monthly_cost(1024 ** 3, "NONEXISTENT")
        assert abs(cost - 0.004) < 1e-9


class TestFormatCostSummary:
    def test_contains_dollar_sign(self):
        from s3archive import format_cost_summary
        result = format_cost_summary(1024 ** 3, "GLACIER_IR")
        assert "$" in result
        assert "GLACIER_IR" in result

    def test_zero_bytes(self):
        from s3archive import format_cost_summary
        result = format_cost_summary(0, "GLACIER_IR")
        assert "$0.00000000" in result


class TestCollectFiles:
    def test_single_file(self, tmp_path):
        from s3archive import collect_files
        f = tmp_path / "data.txt"
        f.write_text("hello")
        files, total = collect_files(str(f))
        assert len(files) == 1
        assert total == 5

    def test_skips_stubs(self, tmp_path):
        from s3archive import collect_files
        (tmp_path / "data.txt.s3arc").write_text("{}")
        files, total = collect_files(str(tmp_path / "data.txt.s3arc"))
        assert len(files) == 0

    def test_directory_walk(self, tmp_path):
        from s3archive import collect_files
        (tmp_path / "a.txt").write_text("aaa")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("bbbbb")
        files, total = collect_files(str(tmp_path))
        assert len(files) == 2
        assert total == 8

    def test_skips_symlinks(self, tmp_path):
        from s3archive import collect_files
        real = tmp_path / "real.txt"
        real.write_text("data")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        files, total = collect_files(str(tmp_path))
        assert len(files) == 1  # only real.txt

    def test_skips_empty_files(self, tmp_path):
        from s3archive import collect_files
        (tmp_path / "empty.txt").write_text("")
        (tmp_path / "notempty.txt").write_text("x")
        files, total = collect_files(str(tmp_path))
        # collect_files includes empty files; stub_file skips them
        assert len(files) == 2


class TestStubFileOwnership:
    """Test that stub_file enforces ownership for non-root callers."""

    def test_ownership_mismatch_rejected(self, tmp_path):
        from s3archive import stub_file
        f = tmp_path / "file.txt"
        f.write_text("data")
        s3_mock = mock.MagicMock()
        # Pretend we are uid 1000 and file is owned by uid 9999
        with mock.patch("os.getuid", return_value=1000), \
             mock.patch("os.stat") as mock_stat:
            mock_stat.return_value = mock.MagicMock(
                st_uid=9999, st_gid=1000, st_mode=0o100644, st_size=4
            )
            _, _, success, status, msg = stub_file(
                str(f), str(tmp_path), "bucket", "prefix/", "GLACIER_IR", s3_mock
            )
        assert not success
        assert "ownership mismatch" in msg

    def test_root_bypasses_ownership(self, tmp_path):
        from s3archive import stub_file
        f = tmp_path / "file.txt"
        f.write_text("data")
        s3_mock = mock.MagicMock()
        # Root (uid 0) should not be blocked even if file owned by someone else
        with mock.patch("os.getuid", return_value=0):
            _, _, success, status, msg = stub_file(
                str(f), str(tmp_path), "bucket", "prefix/", "GLACIER_IR", s3_mock, dry_run=True
            )
        assert success
        assert status == "would archive"


# ---------------------------------------------------------------------------
# s3recall tests
# ---------------------------------------------------------------------------

class TestRecallOwnership:
    """Test that recall_file enforces ownership from S3 metadata."""

    def test_ownership_mismatch_rejected(self, tmp_path):
        from s3recall import recall_file
        from s3arc_common import write_stub
        stub = tmp_path / "file.txt.s3arc"
        write_stub(str(stub), {
            "type": "file",
            "s3key": "FSxONTAP/fs-abc/file.txt",
            "bucket": "my-bucket",
            "storage_class": "GLACIER_IR",
            "checksum": "abc==",
        })
        s3_mock = mock.MagicMock()
        cached_head = {
            "ContentLength": 100,
            "StorageClass": "GLACIER_IR",
            "Metadata": {"original-uid": "9999", "original-gid": "9999"},
        }
        config = {"bucket": "my-bucket"}
        with mock.patch("os.getuid", return_value=1000):
            _, _, success, status, msg = recall_file(
                str(stub), config, s3_mock, dry_run=False, cached_head=cached_head
            )
        assert not success
        assert "ownership mismatch" in msg

    def test_root_bypasses_recall_ownership(self, tmp_path):
        from s3recall import recall_file
        from s3arc_common import write_stub
        stub = tmp_path / "file.txt.s3arc"
        write_stub(str(stub), {
            "type": "file",
            "s3key": "FSxONTAP/fs-abc/file.txt",
            "bucket": "my-bucket",
            "storage_class": "GLACIER_IR",
            "checksum": "abc==",
        })
        s3_mock = mock.MagicMock()
        cached_head = {
            "ContentLength": 100,
            "StorageClass": "GLACIER_IR",
            "Metadata": {"original-uid": "9999", "original-gid": "9999"},
        }
        config = {"bucket": "my-bucket"}
        with mock.patch("os.getuid", return_value=0):
            _, _, success, status, msg = recall_file(
                str(stub), config, s3_mock, dry_run=True, cached_head=cached_head
            )
        assert success
