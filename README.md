# S3Arc - Explicit HSM for Amazon FSx to S3

S3Arc provides file-level archival from Amazon FSx file systems to Amazon S3, replacing archived files with lightweight stub files that preserve directory structure and metadata.

## Overview

S3Arc enables explicit, policy-driven tiering between FSx (ONTAP, Lustre) and S3 storage classes. Unlike automated HSM solutions, S3Arc gives users direct control over what gets archived and when.

**Key Features:**
- Archive files to S3 Glacier Instant Retrieval (millisecond recall)
- `.s3arc` stub files with embedded JSON metadata preserve directory structure
- S3 key encodes full filesystem path — bucket contents mirror the original filesystem
- S3 key prefix auto-generated from FSx type and ID (e.g., `FSxONTAP/fs-abc123/`)
- Original metadata (mtime, permissions, ownership) preserved in S3 and restored on recall
- Ownership enforcement: non-root users can only archive and recall their own files
- SHA-256 checksums for end-to-end data integrity
- Parallel uploads with configurable workers (default: 8)
- Progress indicator with transfer stats
- Cost estimates based on storage class
- SNS notifications on completion
- No vendor lock-in — data recoverable with standard AWS CLI

## Architecture

```
┌─────────────────┐     s3archive      ┌──────────────────────────────────────┐
│   FSx Volume    │ ─────────────────► │   Amazon S3 (Glacier IR)            │
│                 │                    │                                      │
│  file.dat       │     s3recall       │  FSxONTAP/fs-abc123/path/file.dat   │
│  file.dat.s3arc │ ◄───────────────── │  FSxLustre/fs-def456/path/file.dat  │
└─────────────────┘                    └──────────────────────────────────────┘
```

## Quick Start

```bash
# 1. Tag your FSx filesystem with the archive bucket name
aws fsx tag-resource \
    --resource-arn arn:aws:fsx:us-east-1:123456789012:file-system/fs-0123456789abcdef \
    --tags Key=ArchiveBucket,Value=my-archive-bucket

# 2. Archive a file (uploads to S3, replaces original with a small .s3arc stub)
./s3archive.py /mnt/fsx/projects/report.pdf

# 3. See what's archived
./ls-s3arc.py /mnt/fsx/projects/

# 4. Bring it back (downloads from S3, restores original, removes stub)
./s3recall.py /mnt/fsx/projects/report.pdf.s3arc
```

Use `--dry-run` on any command to preview without making changes.

## Prerequisites

- Python 3.9+ (AL2023, RHEL 9, or similar)
- boto3 (`pip install boto3`)
- AWS CLI configured with appropriate credentials
- Amazon FSx file system (ONTAP via NFS, or Lustre) mounted
- S3 bucket for archive storage

## Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/s3arc.git
cd s3arc

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Test Environment

A CloudFormation template is provided in `cloudformation/` to deploy a complete test environment (EC2, FSx ONTAP, FSx Lustre, S3 bucket). See [cloudformation/README.md](cloudformation/README.md) for details.

⚠️ **Cost warning:** The test stack runs ~$1.38/hour (~$33/day). A few days of testing costs roughly $75–150. Delete the stack when done to avoid ongoing charges.

## Configuration

S3Arc determines the archive bucket from:
1. `ArchiveBucket` tag on the FSx filesystem (ONTAP or Lustre)
2. `S3ARC_BUCKET` environment variable

The S3 key prefix is auto-generated from the FSx filesystem type and ID:
- `FSxONTAP/fs-0123456789abcdef/path/to/file.dat`
- `FSxLustre/fs-0123456789abcdef/path/to/file.dat`

This means the S3 bucket contents mirror the original filesystem structure, and different filesystems can archive to the same bucket without collision.

To tag your FSx filesystem:
```bash
aws fsx tag-resource \
    --resource-arn arn:aws:fsx:us-east-1:123456789:file-system/fs-0123456789abcdef \
    --tags Key=ArchiveBucket,Value=my-archive-bucket
```

Additional environment variables:

```bash
export S3ARC_BUCKET="your-archive-bucket"      # Fallback if no FSx tag
export S3ARC_STORAGE_CLASS="GLACIER_IR"        # or DEEP_ARCHIVE
export S3ARC_SNS_TOPIC="arn:aws:sns:us-east-1:123456789:s3arc-notifications"  # optional
```

## Storage Tiers

S3Arc supports two tiers, selected with `-online` (default) or `-offline`:

| Flag       | S3 Storage Class          | Cost (per TB/mo) | Recall Time       | Best For                          |
|------------|---------------------------|-------------------|-------------------|-----------------------------------|
| `-online`  | Glacier Instant Retrieval | ~$4.10            | Milliseconds      | Projects that may be needed again |
| `-offline` | Glacier Deep Archive      | ~$1.01            | 12–48 hours       | Completed work, long-term storage |

With `-offline`, recall is asynchronous. `s3recall` initiates the restore and, if `S3ARC_SNS_TOPIC` is configured, sends an email or SMS notification when the file is ready for download. See [Notifications](#notifications) for setup.

## Usage

### Archive files

```bash
# Archive a single file (default: online/GIR)
./s3archive.py /mnt/fsx/project/data.csv

# Archive to online tier (Glacier Instant Retrieval)
./s3archive.py -online /mnt/fsx/project/

# Archive to offline tier (Glacier Deep Archive)
./s3archive.py -offline /mnt/fsx/project/

# Archive entire directory as single compressed archive
./s3archive.py --aggregate /mnt/fsx/project/old-data/

# Archive aggregate to offline tier (recommended for long-term storage)
./s3archive.py --aggregate -offline /mnt/fsx/project/completed/

# Dry run — show what would be archived
./s3archive.py --dry-run /mnt/fsx/project/

# Disable progress indicator
./s3archive.py --no-progress /mnt/fsx/project/

# Use more workers for many small files
./s3archive.py --workers 16 /mnt/fsx/logs/

# Use fewer workers for large files or limited bandwidth
./s3archive.py --workers 4 /mnt/fsx/datasets/
```

### Recall files

```bash
# Recall a single stub
./s3recall.py /mnt/fsx/project/data.csv.s3arc

# Recall an aggregate archive
./s3recall.py /mnt/fsx/project/old-data.s3arc

# Recall all stubs in a directory
./s3recall.py /mnt/fsx/project/

# Dry run — show what would be recalled
./s3recall.py --dry-run /mnt/fsx/project/

# Disable progress indicator
./s3recall.py --no-progress /mnt/fsx/project/

# Use more workers for many small files
./s3recall.py --workers 16 /mnt/fsx/logs/

# Use fewer workers for large files or limited bandwidth
./s3recall.py --workers 4 /mnt/fsx/datasets/
```

**Recalling from Deep Archive (`-offline`):**

Files archived with `-offline` cannot be downloaded immediately. The recall workflow is:

1. Run `s3recall.py` — the tool detects the Deep Archive storage class, initiates a restore request with S3, and exits. The stub file remains in place.
2. Wait 12–48 hours for S3 to make the file available. If `S3ARC_SNS_TOPIC` is configured, you'll receive an email or SMS when the restore completes.
3. Run `s3recall.py` again on the same stub — this time the file is available, so it downloads, restores the original, and removes the stub.

If you run `s3recall.py` while a restore is still in progress, it reports "restore in progress" and leaves the stub intact. You can safely re-run it as many times as needed — it won't create duplicate restore requests.

### List archived files

```bash
# List stubs with metadata
./ls-s3arc.py /mnt/fsx/project/

# Include S3 storage class and restore status
./ls-s3arc.py --check-status /mnt/fsx/project/

# JSON output
./ls-s3arc.py --json /mnt/fsx/project/
```

## How It Works

Stub files (`.s3arc`) are intentionally visible in directory listings. This is by design — S3Arc is an explicit HSM tool, meaning users archive and recall files on demand, not speculatively or automatically. For project-based workloads, users need to see at a glance which files are archived and which are local. The `.s3arc` extension makes it clear that a file has been moved to S3 and needs to be recalled before use.

### Per-File Mode (Default)

1. **Archive**: `s3archive` uploads the file to S3 with SHA-256 checksum, creates a `.s3arc` stub file containing JSON metadata (S3 key, bucket, storage class, checksum), and removes the original
2. **Stub**: The stub preserves the original filename (with `.s3arc` extension), mtime, and permissions. The JSON content stores the S3 bucket, key, storage class, and SHA-256 checksum
3. **Recall**: `s3recall` reads the S3 location from the stub JSON, downloads the file, verifies the size, restores metadata, and removes the stub

### Aggregate Mode (--aggregate)

1. **Archive**: `s3archive --aggregate` uses a bottom-up recursive approach. It starts at the deepest directories (leaves), archives each directory's files into a `.tar.gz` file, creates a stub, generates a local manifest file, and removes the original files. It then moves up one level and continues recursively until reaching the target directory.
2. **Stub**: Each directory gets its own archive stub (e.g., `docs_files.tar.gz.s3arc`) containing metadata about the archive, plus a local manifest file (e.g., `docs_files.tar.gz.manifest`) with detailed file listings from `tar -tvzf`.
3. **Recall**: `s3recall` detects aggregate stubs, downloads the corresponding `.tar.gz` archive, extracts it to restore the files, verifies against the manifest, and removes both the stub and manifest files.

**When to use aggregate mode:**
- When single-file recovery is not expected — aggregate mode bundles files into a `.tar.gz`, so you must restore the entire directory to retrieve any file
- Completed projects that are rarely accessed
- Large directory trees with many small files — reduces filesystem metadata overhead (one stub per directory vs. one per file)
- Long-term archival to Glacier Deep Archive — per-item retrieval from GDA incurs higher costs than restoring a single aggregate file
- When you need hierarchical directory-level control

**Manifest files provide instant visibility:**
- See archived file lists without downloading from S3
- Standard tar format with permissions, timestamps, and sizes
- No size limits — can handle millions of files
- Use standard tools: `cat`, `grep`, `less` to browse contents

## Recovering Without Stub Files

If a stub file is accidentally deleted, your data is still safe in S3. The S3 key mirrors the original filesystem path, so you can locate and recover it manually.

### Per-file recovery

```bash
# Find your file in S3 (the key matches the original path)
aws s3 ls s3://my-archive-bucket/FSxONTAP/fs-0123456789abcdef/projects/report.pdf

# Download it back
aws s3 cp s3://my-archive-bucket/FSxONTAP/fs-0123456789abcdef/projects/report.pdf /mnt/fsx/projects/report.pdf
```

### Directory recovery

```bash
# List everything under a prefix
aws s3 ls s3://my-archive-bucket/FSxONTAP/fs-0123456789abcdef/projects/ --recursive

# Download an entire directory tree
aws s3 cp s3://my-archive-bucket/FSxONTAP/fs-0123456789abcdef/projects/ /mnt/fsx/projects/ --recursive
```

### Aggregate recovery

Aggregate archives are stored as `.tar.gz` files. Download and extract:

```bash
aws s3 cp s3://my-archive-bucket/FSxLustre/fs-0123456789abcdef/data/data_files.tar.gz /tmp/
tar xzf /tmp/data_files.tar.gz -C /mnt/lustre/data/
```

### Offline (Deep Archive) recovery

If files were archived with `-offline` (Glacier Deep Archive), you must initiate a restore before downloading. This takes 12–48 hours:

```bash
# Initiate restore (Standard tier, available for 7 days)
aws s3api restore-object \
    --bucket my-archive-bucket \
    --key FSxONTAP/fs-0123456789abcdef/projects/report.pdf \
    --restore-request '{"Days": 7, "GlacierJobParameters": {"Tier": "Standard"}}'

# Check restore status (look for "ongoing-request=false")
aws s3api head-object \
    --bucket my-archive-bucket \
    --key FSxONTAP/fs-0123456789abcdef/projects/report.pdf

# Once restored, download normally
aws s3 cp s3://my-archive-bucket/FSxONTAP/fs-0123456789abcdef/projects/report.pdf /mnt/fsx/projects/
```

Files archived with the default `-online` (Glacier Instant Retrieval) require no restore step — they can be downloaded immediately.

When using `s3recall.py` with Deep Archive files, the tool initiates the restore automatically and sends an SNS notification when complete (if `S3ARC_SNS_TOPIC` is configured). You can subscribe to the SNS topic via email or SMS to be notified when your files are ready for download.

## Data Integrity

S3Arc uses SHA-256 checksums for end-to-end data integrity:
- Checksum computed during upload and verified by S3
- Checksum stored in stub file JSON metadata
- Size verified after download during recall
- S3 validates checksums on every GET automatically
- No additional cost — S3 checksums are included free

## Notifications

Set `S3ARC_SNS_TOPIC` to receive SNS notifications on archive/recall completion. Useful for automation and monitoring.

To create an SNS topic and subscribe to it, see [Getting started with Amazon SNS](https://docs.aws.amazon.com/sns/latest/dg/sns-getting-started.html).

Example setup:
```bash
# Create topic
aws sns create-topic --name s3arc-notifications

# Subscribe your email
aws sns subscribe --topic-arn arn:aws:sns:us-east-1:123456789:s3arc-notifications \
    --protocol email --notification-endpoint your-email@example.com

# Set the environment variable
export S3ARC_SNS_TOPIC="arn:aws:sns:us-east-1:123456789:s3arc-notifications"
```

## Security

### IAM Permissions

S3Arc requires the following minimum IAM permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetObjectAttributes",
        "s3:RestoreObject"
      ],
      "Resource": [
        "arn:aws:s3:::my-archive-bucket",
        "arn:aws:s3:::my-archive-bucket/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "fsx:DescribeFileSystems",
        "fsx:ListTagsForResource",
        "fsx:DescribeStorageVirtualMachines"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "arn:aws:sns:us-east-1:123456789012:s3arc-notifications"
    }
  ]
}
```

| Action                              | Used by              | Purpose                                |
|-------------------------------------|----------------------|----------------------------------------|
| `s3:PutObject`                      | s3archive            | Upload files to S3                     |
| `s3:GetObject`                      | s3recall, ls-s3arc   | Download files, read metadata          |
| `s3:DeleteObject`                   | s3archive            | Remove replaced objects                |
| `s3:ListBucket`                     | ls-s3arc             | List archived objects                  |
| `s3:GetObjectAttributes`            | s3recall, ls-s3arc   | Check storage class and size           |
| `s3:RestoreObject`                  | s3recall             | Initiate Deep Archive restore          |
| `fsx:DescribeFileSystems`           | s3archive            | Detect FSx type from mount             |
| `fsx:ListTagsForResource`           | s3archive            | Read bucket/config from FSx tags       |
| `fsx:DescribeStorageVirtualMachines` | s3archive           | Resolve ONTAP SVM to filesystem        |
| `sns:Publish`                       | s3archive, s3recall  | Send completion notifications (optional) |

The SNS statement can be omitted if notifications are not used. The test stack CloudFormation template includes all of these permissions.

- Uses existing AWS credentials (IAM roles, profiles, or environment variables)
- No credentials stored in stub files

### Ownership Enforcement

- **Archive**: non-root users can only archive files they own. In aggregate mode, the entire directory tree is scanned upfront — if any file has a different uid, the operation is refused before any archiving occurs.
- **Recall**: non-root users can only restore files/archives they originally owned. The caller's uid is compared against `original-uid` in S3 object metadata. Mismatches are refused and stubs are left intact.
- **Root** can archive and restore any user's files, with original uid/gid preserved.

### Trust Model

- Stub files (`.s3arc`) are untrusted pointers — they contain only the S3 bucket, key, storage class, and checksum. No ownership or permission data is stored in stubs.
- File ownership (uid/gid) and permissions (mode, mtime) are stored in S3 object metadata, protected by IAM policies. The ownership check on both archive and recall reads from S3 or the filesystem, never from the stub.
- S3 bucket policies control access to archived data

## Unit Tests

No AWS account, credentials, or FSx mounts are needed to run the unit tests. All AWS SDK calls are automatically mocked, so the test suite runs on any machine with Python 3.9+. To archive and recall real files, see [Installation](#installation) and [Usage](#usage).

```bash
pip install pytest
python -m pytest tests/ -v
```

The 30 unit tests cover:
- Stub file read/write and JSON roundtrip
- S3 key generation from filesystem paths
- Storage cost estimation and formatting
- File collection (skips stubs, symlinks, handles directories)
- Ownership enforcement on archive and recall (non-root rejection, root bypass)
- Thread-safe progress tracking

To test in a real AWS environment with FSx mounts, use `--dry-run` to validate configuration without uploading:

```bash
./s3archive.py --dry-run /mnt/fsx/testdata/
./s3recall.py --dry-run /mnt/fsx/testdata/
./ls-s3arc.py /mnt/fsx/testdata/
```

## S3 Lifecycle Policies

S3Arc is compatible with S3 lifecycle policies, but automatic storage class transitions change recall behavior.

**Example:** You archive files with `-online` (Glacier Instant Retrieval), then a lifecycle rule transitions them to Deep Archive after 90 days. S3Arc detects the current storage class at recall time, so:
- Recall becomes asynchronous (12–48 hours instead of milliseconds)
- Retrieval costs increase (~$0.02/GB for Standard tier, ~$0.0025/GB for Bulk)
- Users expecting instant recall will be surprised

**Recommendations:**
- If you use lifecycle transitions, inform users that older archives may require async restore
- Set the `RestoreTier` FSx tag to `Bulk` to reduce retrieval costs for large restores
- Consider whether the storage savings justify the recall tradeoff

The transition itself is free — S3 only charges for retrieval when you recall.

## Limitations

### Per-File Mode
- Symlinks are skipped
- Empty files are skipped

### Aggregate Mode
- All-or-nothing recall per directory level — cannot retrieve individual files without restoring entire directory archive
- Requires local disk space equal to 110% of each directory size during compression
- Maximum 5TB per directory archive (estimated 50% compression)
- Asynchronous recall for Deep Archive (12-48 hours)
- Binary data may not compress well
- Creates hierarchical stub structure that mirrors original directory tree

## Future Planned Features

### Vaulting (S3 Object Lock / WORM Protection)

Optional immutable archiving using S3 Object Lock to prevent deletion or modification of archived objects until a retention period expires.

**Scenario 1: Single bucket, per-object retention**
- The archive bucket is created with Object Lock and Versioning enabled but no default retention policy
- Both plain and vaulted archives coexist in the same bucket
- CLI flags control retention at upload time:
  - `--vault` — enable Object Lock with a default retention mode and period
  - `--retain-mode GOVERNANCE|COMPLIANCE` — Governance allows privileged override; Compliance is truly immutable
  - `--retain-days N` or `--retain-until YYYY-MM-DD` — retention duration or absolute date
- Example: `s3archive.py --vault --retain-mode GOVERNANCE --retain-days 365 /mnt/fsx/data`

**Scenario 2: Separate vault bucket**
- A dedicated bucket with Object Lock enabled and a bucket-level default retention policy
- All objects uploaded to this bucket inherit the default retention
- Useful when all archives in a bucket must be vaulted uniformly
- CLI flag: `--bucket <vault-bucket-name>` or configured via environment variable

**Scenario 3: Legal hold**
- Independent of retention periods — places an indefinite hold on an object
- Object cannot be deleted until the legal hold is explicitly removed
- CLI flag: `--legal-hold` applied per-object at upload time
- Useful for litigation or regulatory preservation where the release date is unknown

**Requirements**
- Object Lock must be enabled at bucket creation time (cannot be added retroactively)
- Versioning is automatically enabled with Object Lock
- Governance mode: users with `s3:BypassGovernanceRetention` can override retention
- Compliance mode: no one can delete or shorten retention, including the root account
- The CloudFormation template will need `ObjectLockEnabled: true` and `ObjectLockConfiguration` on the bucket resource

## Uninstalling S3Arc

### Test environment

Delete the CloudFormation stack — it automatically empties the S3 bucket and removes all resources (FSx filesystems, EC2, VPC, etc.):

```bash
aws cloudformation delete-stack --stack-name s3arc-test
```

### Production environment

Your archived data remains in S3, organized by filesystem path (e.g., `FSxONTAP/fs-xxx/path/to/file.dat`). To fully restore and stop using S3Arc:

1. **Recall everything:** Run `s3recall.py` on your FSx mount root to restore all archived files:
   ```bash
   ./s3recall.py /mnt/fsx/
   ```

2. **If you lack space:** Grow your FSx (or POSIX) filesystem first, or recall in batches by directory.

3. **Once all stubs are gone:** Your filesystem is back to its original state. You can delete the S3 bucket or keep it as a backup.

If you no longer have the stub files, your data is still recoverable directly from S3 — see [Recovering Without Stub Files](#recovering-without-stub-files).

## License

This project is licensed under the MIT-0 License. See [LICENSE](LICENSE).
