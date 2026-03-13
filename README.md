# S3Arc - Explicit HSM for Amazon FSx to S3

S3Arc provides file-level archival from Amazon FSx file systems to Amazon S3, replacing archived files with zero-byte stub files that preserve directory structure and metadata.

## Overview

S3Arc enables explicit, policy-driven tiering between FSx (ONTAP, Lustre) and S3 storage classes. Unlike automated HSM solutions, S3Arc gives users direct control over what gets archived and when.

**Key Features:**
- Archive files to S3 Glacier Instant Retrieval (millisecond recall)
- Zero-byte `.s3arc` stub files preserve directory structure
- Extended attributes (xattrs) make stubs self-describing
- Original metadata (mtime, permissions, ownership) preserved
- SHA-256 checksums for end-to-end data integrity
- Parallel uploads with configurable workers (default: 8)
- Progress indicator with transfer stats
- Cost estimates based on storage class
- SNS notifications on completion
- No vendor lock-in — data recoverable with standard AWS CLI

## Architecture

```
┌─────────────────┐     s3archive      ┌─────────────────┐
│   FSx Volume    │ ─────────────────► │   Amazon S3     │
│                 │                    │  (Glacier IR)   │
│  file.dat       │     s3recall       │                 │
│  file.dat.s3arc │ ◄───────────────── │  archived/...   │
└─────────────────┘                    └─────────────────┘
```

## Prerequisites

- Python 3.8+
- AWS CLI configured with appropriate credentials
- Amazon FSx file system (ONTAP or Lustre) mounted via NFS
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

## Configuration

S3Arc determines the archive bucket in this order:
1. `ArchiveBucket` tag on the FSx filesystem (ONTAP or Lustre)
2. `S3ARC_BUCKET` environment variable

To tag your FSx filesystem:
```bash
aws fsx tag-resource \
    --resource-arn arn:aws:fsx:us-east-1:123456789:file-system/fs-0123456789abcdef \
    --tags Key=ArchiveBucket,Value=my-archive-bucket
```

Additional environment variables:

```bash
export S3ARC_BUCKET="your-archive-bucket"      # Fallback if no FSx tag
export S3ARC_PREFIX="archived/"
export S3ARC_STORAGE_CLASS="GLACIER_IR"        # or DEEP_ARCHIVE
export S3ARC_SNS_TOPIC="arn:aws:sns:us-east-1:123456789:s3arc-notifications"  # optional
```

## Usage

### Archive files

```bash
# Archive a single file (default: online/GIR)
./s3archive.py /mnt/fsx/project/data.csv

# Archive to online tier (Glacier Instant Retrieval)
./s3archive.py -online /mnt/fsx/project/

# Archive to offline tier (Glacier Deep Archive)
./s3archive.py -offline /mnt/fsx/project/

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

1. **Archive**: `s3archive` uploads the file to S3 with SHA-256 checksum, creates a zero-byte stub with `.s3arc` extension, stores S3 location and checksum in xattrs, and removes the original
2. **Stub**: The stub preserves the original filename (with extension), mtime, and permissions. Extended attributes store the S3 bucket, key, and SHA-256 checksum
3. **Recall**: `s3recall` reads the S3 location from xattrs, downloads the file, verifies the checksum, restores metadata, and removes the stub

## Data Integrity

S3Arc uses SHA-256 checksums for end-to-end data integrity:
- Checksum computed during upload and verified by S3
- Checksum stored in stub file xattrs
- Checksum verified after download during recall
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

- Uses existing AWS credentials (IAM roles, profiles, or environment variables)
- No credentials stored in stub files
- S3 bucket policies control access to archived data

## Limitations

- Requires xattr support on the filesystem (NFSv4 recommended)
- Symlinks are skipped
- Empty files are skipped

## License

This project is licensed under the MIT-0 License. See [LICENSE](LICENSE).
