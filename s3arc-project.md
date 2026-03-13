# S3Arc Project — File Stubbing Utility for FSx for NetApp ONTAP

## 1. Overview

S3Arc is a pair of command-line utilities (`s3archive` and `s3recall`) that enable file-level and directory-level archival from an FSx for NetApp ONTAP file system to Amazon S3 Glacier Instant Retrieval (GIR). Archived files are replaced with zero-byte stub files marked with a `.s3arc` extension, preserving the directory structure and file metadata while freeing storage on the primary file system.

This approach provides a simple, vendor-independent hierarchical storage management (HSM) capability without requiring third-party software such as Komprise or Hammerspace.

## 2. Design Goals

- Files archived to S3 as native objects (no proprietary format)
- Stubs are immediately visible in directory listings via `.s3arc` extension
- Each stub is self-describing via extended attributes (xattrs)
- Original file metadata (mtime, permissions, ownership) preserved in S3 object metadata
- Directory structure remains intact after archival
- Single command to archive or recall a file or entire directory tree
- Glacier Instant Retrieval provides millisecond-latency recall (no async wait)
- No vendor lock-in — data recoverable with standard AWS CLI if scripts are lost

## 3. Target Environment

- File system: Amazon FSx for NetApp ONTAP (NFS mount)
- NFS version: NFSv4 recommended (native xattr support)
- S3 region: Any AWS region with FSx and S3 Glacier support
- S3 storage class: Glacier Instant Retrieval (GLACIER_IR)
- Python 3.8+
- Dependencies: boto3, xattr

## 4. How It Works

### 4.1 Archive Flow (s3archive)

```
User runs: s3archive /mnt/fsx/project-alpha/old-reports/

For each file in the directory:
  1. Upload file to S3 bucket with GLACIER_IR storage class
  2. Store original metadata (mtime, size, mode, uid, gid) as S3 object metadata
  3. Verify upload (size check via HeadObject)
  4. Create zero-byte stub file with .s3arc extension
  5. Set xattrs on stub: user.stub.s3key and user.stub.bucket
  6. Preserve original mtime and permissions on the stub
  7. Delete original file

Directories are never modified — only files within them are processed.
```

### 4.2 Recall Flow (s3recall)

```
User runs: s3recall /mnt/fsx/project-alpha/old-reports/

For each .s3arc file in the directory:
  1. Read S3 key and bucket from xattrs
  2. Check S3 object storage class and restore status
  3. Download file from S3 to original filename (strip .s3arc)
  4. Verify download size against S3 metadata
  5. Restore original mtime and permissions
  6. Delete the .s3arc stub
```

### 4.3 Stub File Anatomy

After archiving `/mnt/fsx/reports/q1-summary.pdf`:

```
Filesystem:
  /mnt/fsx/reports/q1-summary.pdf.s3arc   (zero bytes)
    xattr: user.stub.s3key  = "archived/reports/q1-summary.pdf"
    xattr: user.stub.bucket = "my-archive-bucket"
    mtime: (preserved from original)
    mode:  (preserved from original)

S3:
  s3://my-archive-bucket/archived/reports/q1-summary.pdf
    StorageClass: GLACIER_IR
    Metadata:
      original-mtime: 1705334400.0
      original-size:  5242880
      original-mode:  0o100644
      original-uid:   1000
      original-gid:   1000
```

## 5. Usage

### 5.1 Installation

```bash
pip install boto3 xattr
chmod +x s3archive.py s3recall.py

# Optional: symlink into PATH
ln -s /path/to/s3archive.py /usr/local/bin/s3archive
ln -s /path/to/s3recall.py /usr/local/bin/s3recall
```

### 5.2 Configuration

Configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| S3ARC_BUCKET | my-archive-bucket | Target S3 bucket name |
| S3ARC_PREFIX | archived/ | S3 key prefix for all archived objects |
| S3ARC_STORAGE_CLASS | GLACIER_IR | S3 storage class |

Example:
```bash
export S3ARC_BUCKET="my-project-archive"
export S3ARC_PREFIX="archived/"
export S3ARC_STORAGE_CLASS="GLACIER_IR"
```

AWS credentials must be configured via standard methods (IAM role, ~/.aws/credentials, environment variables).

### 5.3 Commands

Archive a single file:
```bash
s3archive /mnt/fsx/project-alpha/old-reports/q1-summary.pdf
```

Archive all files in a directory (recursive):
```bash
s3archive /mnt/fsx/project-alpha/old-reports/
```

Recall a single file:
```bash
s3recall /mnt/fsx/project-alpha/old-reports/q1-summary.pdf.s3arc
```

Recall all stubs in a directory (recursive):
```bash
s3recall /mnt/fsx/project-alpha/old-reports/
```

### 5.4 Example Session

```
$ ls -la /mnt/fsx/project-alpha/old-reports/
-rw-r--r--  1 user group  5.2M  Jan 15 2024  q1-summary.pdf
-rw-r--r--  1 user group  3.1M  Mar 22 2024  q2-summary.pdf
-rw-r--r--  1 user group  4.8M  Dec 01 2025  q4-summary.pdf

$ s3archive /mnt/fsx/project-alpha/old-reports/
  stubbed: .../q1-summary.pdf -> .../q1-summary.pdf.s3arc
  stubbed: .../q2-summary.pdf -> .../q2-summary.pdf.s3arc
  stubbed: .../q4-summary.pdf -> .../q4-summary.pdf.s3arc

Complete: 3 archived, 0 failed

$ ls -la /mnt/fsx/project-alpha/old-reports/
-rw-r--r--  1 user group     0  Jan 15 2024  q1-summary.pdf.s3arc
-rw-r--r--  1 user group     0  Mar 22 2024  q2-summary.pdf.s3arc
-rw-r--r--  1 user group     0  Dec 01 2025  q4-summary.pdf.s3arc

$ s3recall /mnt/fsx/project-alpha/old-reports/
  recalled: .../q1-summary.pdf
  recalled: .../q2-summary.pdf
  recalled: .../q4-summary.pdf

Complete: 3 recalled, 0 failed
```

## 6. Error Handling

Both utilities handle the following error conditions:

| Condition | Behavior |
|---|---|
| S3 credentials invalid | Exit with error before processing |
| S3 bucket not accessible | Exit with error before processing |
| File not readable | Skip file, log error, continue |
| S3 upload failure | Skip file, log error, continue |
| Upload size mismatch | Skip file, log error, do not create stub |
| xattr not supported | Skip file, clean up stub, log error |
| Cannot create stub file | Skip file, log error, continue |
| Cannot remove original | Log error (data safe in S3 and stub exists) |
| S3 object not found on recall | Skip file, log error, continue |
| Object in Glacier/Deep Archive | Initiate async restore, log status, skip |
| Download failure | Clean up partial file, log error, continue |
| Download size mismatch | Log warning, file still restored |
| Cannot restore metadata | Log warning, file still restored |
| Original path already exists on recall | Skip to prevent overwrite |
| Already stubbed file | Skip silently |
| Empty file (0 bytes) | Skip (nothing to archive) |
| Symlinks | Skipped (not followed) |

Both commands print a summary on completion and exit with code 1 if any files failed.

## 7. Data Recovery Without Scripts

If the s3archive/s3recall scripts are lost, data is still recoverable:

### Read stub xattrs manually:
```bash
getfattr -n user.stub.s3key /mnt/fsx/path/file.s3arc
getfattr -n user.stub.bucket /mnt/fsx/path/file.s3arc
```

### Download directly with AWS CLI:
```bash
aws s3 cp s3://my-archive-bucket/archived/path/file.pdf ./file.pdf
```

### Browse all archived objects:
```bash
aws s3 ls s3://my-archive-bucket/archived/ --recursive
```

### Read original metadata from S3:
```bash
aws s3api head-object --bucket my-archive-bucket --key archived/path/file.pdf
```

The S3 key structure mirrors the original filesystem path, making manual recovery straightforward.

## 8. Extended Attribute Storage Considerations

Each stub file carries two xattrs:

- `user.stub.s3key` — S3 object key (~60-100 bytes)
- `user.stub.bucket` — S3 bucket name (~20-80 bytes)

On ONTAP, xattrs that exceed the inode's inline capacity are stored in separate 4KB blocks. For environments with billions of files, the additional metadata overhead should be tested empirically. The per-file overhead is small, but at extreme scale (1 billion+ stubs), the aggregate metadata consumption may be significant.

To test: create a sample volume, populate with files, apply xattrs, and measure volume metadata usage via `volume show -fields filesys-metadata`.

## 9. Prerequisites and Limitations

### Prerequisites
- Python 3.8+
- boto3 and xattr Python packages
- AWS credentials with s3:PutObject, s3:GetObject, s3:HeadObject, s3:HeadBucket permissions
- NFSv4 mount with xattr support
- S3 bucket with appropriate permissions and optional Object Lock configuration

### Limitations
- Not transparent — users must know to use s3recall to restore files
- No automatic policy-based archival (manual invocation only)
- No parallel processing (files processed sequentially)
- Renaming a .s3arc stub breaks the association (xattrs travel with the file, but the S3 key reflects the original path)
- Copying stubs without preserving xattrs (e.g., cp without -a) loses the S3 pointer
- No integration with S3 Object Lock — must be configured separately on the bucket
- Glacier Instant Retrieval has a 90-day minimum storage duration charge

### Required IAM Permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:HeadObject",
        "s3:HeadBucket",
        "s3:ListBucket",
        "s3:RestoreObject"
      ],
      "Resource": [
        "arn:aws-us-gov:s3:::my-archive-bucket",
        "arn:aws-us-gov:s3:::my-archive-bucket/*"
      ]
    }
  ]
}
```

## 10. Comparison to FSx Built-in Tiering

This section compares s3arc to the built-in tiering capabilities of FSx for NetApp ONTAP (FabricPool) and FSx for Lustre (S3 data repository association).

### Advantages of s3arc

- **File/directory granularity** — you choose exactly what gets archived. FabricPool operates on cold data blocks automatically; you cannot say "archive this directory now."
- **Glacier storage classes** — FabricPool only tiers to S3 Standard or S3 Standard-IA. It cannot target Glacier classes directly. s3arc puts data into Glacier Instant Retrieval, and S3 lifecycle policies can transition objects further to Glacier Flexible Retrieval or Deep Archive.
- **Visible stubs** — the `.s3arc` extension makes it immediately obvious which files are archived. FabricPool is invisible to users, and reads of cold blocks silently incur latency with no indication the data was tiered.
- **S3 lifecycle compatibility** — archived objects are native S3 objects, so standard S3 lifecycle policies can transition them across storage classes over time. FabricPool stores data in a proprietary block format that cannot be lifecycled.
- **Object Lock / WORM** — S3 Object Lock (Compliance mode) can be applied to archived objects for government retention requirements. FabricPool's cloud tier does not support Object Lock.
- **No surprise retrieval costs** — with FabricPool, any read of a cold file silently pulls blocks from S3, incurring retrieval costs that are difficult to predict. With s3arc, recall is explicit and intentional.
- **Vendor independence** — data in S3 is stored as plain objects with human-readable keys and self-describing metadata. No dependency on ONTAP or Lustre to access it. Data survives a file system migration or decommission.
- **Cross-filesystem portability** — the same tool works on FSx for NetApp ONTAP, FSx for Lustre, EFS, or any NFS mount. It is not tied to one file system's tiering implementation.

### Disadvantages of s3arc

- **Manual operation** — someone must decide what to archive and run the command. FabricPool is fully automatic based on data temperature.
- **Not transparent to users** — users see `.s3arc` stub files and must explicitly recall them. FabricPool recall is seamless and invisible to applications.
- **No automatic re-tiering** — if a recalled file goes cold again, it remains on the file system unless someone runs s3archive again. FabricPool continuously manages hot/cold placement.
- **Operational overhead** — custom tooling must be maintained, documented, and supported. FabricPool is built-in and fully managed by AWS/NetApp.

### Summary

s3arc and built-in tiering solve different problems. s3arc is designed for intentional, policy-driven archival with full control over storage class, retention, and compliance. Built-in tiering is designed for automatic, transparent hot/cold data management. The two approaches can be used together: FabricPool for day-to-day automatic tiering, and s3arc for explicit long-term archival to Glacier storage classes with Object Lock compliance.

## 11. Future Enhancements

- `--dry-run` flag to preview operations without executing
- `--mtime-older-than` filter for policy-based archival
- Parallel file processing (multiprocessing pool)
- Transaction log for resumable operations
- SNS notification on completion
- Bash/AWS CLI version with zero Python dependencies
- Integration with cron or EventBridge for scheduled archival
- Support for S3 Glacier Deep Archive with async recall workflow

---

## Appendix A: s3archive.py

See `s3archive.py` in this project directory.

## Appendix B: s3recall.py

See `s3recall.py` in this project directory.

---

*Generated: 2026-03-10*
