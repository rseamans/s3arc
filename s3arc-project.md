# Enabling Explicit Transparent HSM for Efficient Storage in Amazon FSx Environments

## 1. Background — A Brief History of Hierarchical Storage Management and File Stubbing

The concept of hierarchical storage management (HSM) emerged in the 1970s on mainframe systems, where the cost disparity between fast disk and high-capacity tape made it impractical to keep all data online. IBM's Data Facility Hierarchical Storage Manager (DFHSM), introduced for MVS in 1979, established the foundational pattern: migrate infrequently accessed data from disk to tape, leave a stub so the file remains visible, and recall the data when accessed.

This model moved into supercomputing in the late 1980s with the Cray Data Migration Facility (DMF), which became the backbone of data management at national laboratories and HPC centers worldwide. DMF integrated deeply with XFS through the DMAPI kernel interface, enabling transparent recall on file access as well as explicit staging via dmget and dmput. The product evolved through SGI and HPE acquisitions into DMF7, still in use today.

Through the 1990s and 2000s, HSM proliferated across platforms — IBM's Tivoli Storage Manager (now IBM Storage Protect) on AIX and Linux, Sun's SAM-QFS on Solaris, NTFS reparse points enabling products like Microsoft Remote Storage and Azure File Sync on Windows, and Quantum StorNext combining SAN filesystems with tape tiering for media and entertainment. More recently, FUSE-based solutions and products like Komprise offer alternatives that avoid kernel modifications, trading some transparency for broader compatibility.

### The Case for Explicit Tiering

While automated HSM — where data migrates and recalls automatically based on watermarks and access patterns — works well for general-purpose file serving, many workloads benefit from explicit, policy-driven tiering instead.

Project-based workflows are a prime example. In media and entertainment, a film or episodic project has a clear lifecycle: active production, post-production, review, and archive. During active work, all project assets need to be on fast storage. Once a project wraps, the entire dataset can be moved to a cheaper tier in one deliberate operation. Transparent HSM can actually be counterproductive here — an automated scan or thumbnail generator can inadvertently recall terabytes of archived footage, thrashing the tape library and consuming expensive disk capacity.

The same pattern applies across many industries. In electronic design automation (EDA), a chip design project may consume hundreds of terabytes during active verification runs, then sit untouched for months until a respin. In computational fluid dynamics, seismic modelling, and other simulation-heavy disciplines, datasets are generated in bursts, analyzed intensively, and then become cold. In life sciences, genomic sequencing runs produce massive outputs that are processed once and then archived for regulatory retention.

In all of these cases, the humans and workflows involved know exactly when data transitions between hot and cold states. Explicit tiering — where a project manager, pipeline tool, or automation script deliberately moves data between tiers — aligns storage costs with project phases without the risk of unintended recalls or migrations that automated HSM introduces. The storage system becomes a tool the team controls rather than an opaque automaton they must work around.

### Cloud Provider HSM Capabilities

Azure offers a comparable capability through Azure File Sync, which syncs Windows file servers with Azure Files and provides transparent cloud tiering. Infrequently accessed files are replaced with NTFS reparse point stubs and recalled automatically on access. However, Azure File Sync is Windows-only, tightly coupled to Azure Files, and operates on automatic policies rather than explicit user control.

GCP lacks a native HSM solution. Filestore (managed NFS) has no tiering, and while NetApp Cloud Volumes on GCP supports automatic tiering, there is no stub-based recall model. HSM on GCP requires third-party solutions or custom tooling.


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 2. Overview

S3Arc applies the explicit tiering principle to AWS cloud storage. Rather than relying solely on automatic tiering mechanisms like FabricPool, S3Arc gives users direct, intentional control over what gets archived, when, and to which S3 storage class — while preserving the directory structure and file metadata that users and workflows depend on. The result is a lightweight, vendor-independent HSM capability built entirely on existing filesystem and Amazon S3 technology stacks.

S3Arc is a set of command-line utilities (`s3archive`, `s3recall`, and `ls-s3arc`) that enable file-level and directory-level archival from Amazon FSx file systems to Amazon S3. The primary targets are FSx for NetApp ONTAP and FSx for Lustre. Archived files are replaced with lightweight `.s3arc` stub files containing JSON metadata, preserving the directory structure and file metadata while freeing storage on the primary file system.

S3Arc supports two archival modes:

- **Per-file mode (default):** Individual files are uploaded to S3 and replaced with per-file stubs. Supports both Glacier Instant Retrieval (GIR) for millisecond-latency recall and Glacier Deep Archive (GDA) for lowest-cost storage.
- **Aggregate mode (`--aggregate`):** An entire directory tree is compressed into a single `.tar.gz` archive, uploaded to S3, and the directory is replaced with a single stub file. Supports both GIR and GDA storage classes.

This approach provides a simple, vendor-independent hierarchical storage management (HSM) capability without requiring third-party software such as Komprise or Hammerspace. S3Arc is not designed to replace automatic tiering to capacity storage (e.g., FabricPool), but rather to complement it — providing explicit, transparent, and self-documenting archival that leverages existing file system and AWS S3 technology stacks. In project-based environments, projects are frequently completed, put on hold, cancelled, or restarted. These are explicit events, and by explicitly tiering the data off to S3 (or back again) the user can gain additional savings — for example, by leveraging Glacier Instant Retrieval and Glacier Deep Archive — without disturbing the file system structure the users are familiar with.

```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

### Architecture Diagram

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                                 AWS Cloud                                     │
│                                                                               │
│  ┌────────────────────┐            ┌───────────────────────────────────────┐  │
│  │    Amazon EC2      │            │           Amazon S3                   │  │
│  │                    │            │                                       │  │
│  │  ┌──────────────┐  │            │  ┌─────────────────────────────────┐  │  │
│  │  │  S3Arc CLI   │  │            │  │      Archive Bucket             │  │  │
│  │  │              │  │            │  │                                 │  │  │
│  │  │ s3archive.py ├──┼── PUT ────>│  │  FSxONTAP/fs-xxx/               │  │  │
│  │  │ s3recall.py  <──┼── GET ────-│  │    path/file.dat                │  │  │
│  │  │ ls-s3arc.py  ├──┼── HEAD ───>│  │                                 │  │  │
│  │  │              │  │            │  │  FSxLustre/fs-xxx/              │  │  │
│  │  │ s3arc_common │  │            │  │    path/dir_files.tar.gz        │  │  │
│  │  └──────┬───────┘  │            │  │                                 │  │  │
│  │         │          │            │  │  Storage Classes:               │  │  │
│  │         │          │            │  │   -online  = Glacier IR         │  │  │
│  │         │          │            │  │   -offline = Deep Archive       │  │  │
│  └─────────┼──────────┘            │  └─────────────────────────────────┘  │  │
│            │                       └───────────────────────────────────────┘  │
│            │                                                                  │
│  ┌─────────┴──────────┐            ┌──────────────────────┐                   │
│  │   FSx Mounts       │            │    Amazon SNS        │                   │
│  │                    │            │                      │                   │
│  │  ┌──────────────┐  │            │  Completion alerts   │                   │
│  │  │ FSx ONTAP    │  │            │  (email / SMS)       │                   │
│  │  │ /mnt/fsx     │  │            └──────────────────────┘                   │
│  │  │              │  │                                                       │
│  │  │ Tags:        │  │            ┌──────────────────────┐                   │
│  │  │ ArchiveBucket│  │            │    IAM Role          │                   │
│  │  │ StorageClass │  │            │                      │                   │
│  │  │ SnsTopicArn  │  │            │  s3:Put/Get/Delete   │                   │
│  │  └──────────────┘  │            │  fsx:Describe/Tags   │                   │
│  │  ┌──────────────┐  │            │  sns:Publish         │                   │
│  │  │ FSx Lustre   │  │            └──────────────────────┘                   │
│  │  │ /mnt/lustre  │  │                                                       │
│  │  └──────────────┘  │                                                       │
│  └────────────────────┘                                                       │
│                                                                               │
│  On filesystem:                                                               │
│   archive: file.dat (5MB) --> file.dat.s3arc (186 bytes)                      │
│   recall:  file.dat.s3arc --> file.dat (5MB restored)                         │
│                                                                               │
└───────────────────────────────────────────────────────────────────────────────┘
```

```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 3. Design Goals

- Files archived to S3 as native objects (no proprietary format)
- Stubs are immediately visible in directory listings via `.s3arc` extension
- Each stub is self-describing via embedded JSON metadata
- Original file metadata (mtime, permissions, ownership) preserved in S3 object metadata and restored on recall
- Ownership enforcement: non-root users can only archive and recall their own files; root restores original uid/gid
- SHA-256 checksums for end-to-end data integrity verification
- Parallel uploads with configurable workers for optimal throughput
- Progress indicator with real-time transfer stats
- Cost estimates based on storage class and data size
- Directory structure remains intact after per-file archival
- Single command to archive or recall a file, directory, or aggregate archive
- Per-file mode uses Glacier Instant Retrieval for millisecond-latency recall
- Aggregate mode uses Glacier Deep Archive for lowest-cost long-term retention
- SNS notifications on archive/recall completion
- No vendor lock-in — data recoverable with standard AWS CLI if scripts are lost
- Inherently self-documenting — stub files preserve original file metadata, directory structure, and S3 location via embedded JSON, making archived data discoverable and recoverable without proprietary tools


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 4. Target Environment

- File system: Amazon FSx for NetApp ONTAP (NFS mount) or Amazon FSx for Lustre
- S3 region: Any AWS region with FSx and S3 Glacier support
- S3 storage classes: Glacier Instant Retrieval (per-file), Glacier Deep Archive (aggregate)
- Python 3.9+
- Dependencies: boto3


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 5. How It Works

**Before archival:**
```
/mnt/fsx/project-alpha/
├── data.csv           (100 MB)
├── report.pdf         (5 MB)
└── results/
    ├── output1.txt    (2 MB)
    └── output2.txt    (3 MB)
```

**After per-file archival (s3archive):**
```
/mnt/fsx/project-alpha/
├── data.csv.s3arc     (JSON metadata pointing to S3)
├── report.pdf.s3arc   (JSON metadata pointing to S3)
└── results/
    ├── output1.txt.s3arc    (JSON metadata pointing to S3)
    └── output2.txt.s3arc    (JSON metadata pointing to S3)

S3 bucket: s3://archive-bucket/FSxONTAP/fs-xxx/
├── project-alpha/data.csv
├── project-alpha/report.pdf
└── project-alpha/results/
    ├── output1.txt
    └── output2.txt
```

**After aggregate archival (s3archive --aggregate):**
```
/mnt/fsx/project-alpha/
├── project-alpha_files.tar.gz.s3arc     (JSON metadata pointing to S3)
├── project-alpha_files.tar.gz.manifest  (text file listing archive contents)
└── results/
    ├── results_files.tar.gz.s3arc       (JSON metadata pointing to S3)
    └── results_files.tar.gz.manifest    (text file listing archive contents)

S3 bucket: s3://archive-bucket/FSxONTAP/fs-xxx/
├── project-alpha/project-alpha_files.tar.gz    (compressed archive of data.csv + report.pdf)
└── project-alpha/results/results_files.tar.gz  (compressed archive of output1.txt + output2.txt)
```

### 5.1 Per-File Archive Flow (s3archive — default)

```
User runs: s3archive /mnt/fsx/project-alpha/old-reports/

For each file in the directory:
  1. Upload file to S3 bucket with GLACIER_IR storage class and SHA-256 checksum
  2. Store original metadata (mtime, size, mode, uid, gid) as S3 object metadata
  3. Verify upload (size check and checksum via HeadObject)
  4. Create .s3arc stub file containing JSON metadata
  5. JSON includes: s3key, bucket, storage_class, checksum, type="file"
  6. Preserve original mtime and permissions on the stub
  7. Delete original file

Directories are never modified — only files within them are processed.
```

### 5.2 Aggregate Archive Flow (s3archive --aggregate)

```
User runs: s3archive --aggregate /mnt/fsx/project-alpha/old-simulations/

  1. Walk the directory tree, collect file inventory (count, total size)
  2. Create tar.gz archive of the entire directory tree
     - Preserves permissions, ownership, symlinks, and directory structure
     - Streams via multipart upload to S3 when possible to minimize local scratch
  3. Upload tar.gz to S3 with DEEP_ARCHIVE storage class
  4. Store manifest metadata on S3 object (file count, original size, archive size)
  5. Verify upload (size check via HeadObject)
  6. Remove the entire directory tree
  7. Create a single .s3arc stub file containing JSON metadata
  8. JSON includes:
     - s3key           = S3 key of the tar.gz
     - bucket          = S3 bucket name
     - type            = "aggregate"
     - storage_class   = storage class used
     - manifest        = {files, file_list, original_bytes,
                          compressed_bytes, compression_ratio, created}
  9. Preserve original directory mtime on the stub
```

### 5.3 Per-File Recall Flow (s3recall — auto-detected)

```
User runs: s3recall /mnt/fsx/project-alpha/old-reports/

For each .s3arc file where type = "file" (or absent):
  1. Read S3 key, bucket, and checksum from stub JSON
  2. Check S3 object storage class and restore status
  3. Ownership check: read original-uid from S3 object metadata
     - If caller is root: proceed (will chown to original owner after restore)
     - If caller uid matches original-uid: proceed
     - If caller uid differs: REFUSE to restore, leave stub intact
  4. Download file from S3 to original filename (strip .s3arc)
  5. Verify download size against S3 metadata
  6. Restore original mtime, permissions, and ownership (uid/gid)
  7. Delete the .s3arc stub
```

### 5.4 Aggregate Recall Flow (s3recall — auto-detected)

```
User runs: s3recall /mnt/fsx/project-alpha/old-simulations.s3arc

  1. Read stub JSON → detect type = "aggregate"
  2. Read S3 key and bucket from JSON
  3. Ownership check: read original-uid from S3 object metadata
     - If caller is root: proceed (will chown to original owner after restore)
     - If caller uid matches original-uid: proceed
     - If caller uid differs: REFUSE to restore, leave stub intact
  4. Check S3 object storage class → DEEP_ARCHIVE
  5. Check restore status via HeadObject:

     If no restore in progress:
       a. Initiate RestoreObject request (Standard: 12h, or Bulk: 48h)
       b. Print: "Restore initiated. Object will be available in ~12 hours
          (Standard) or ~48 hours (Bulk). Run this command again to check."
       c. Exit

     If restore in progress:
       a. Print: "Restore in progress. Check back later."
       b. Exit

     If restore complete (temporary copy available):
       a. Download tar.gz to temp location
       b. Extract to original directory path
       c. Verify file count and total size against manifest in stub JSON
       d. Remove the .s3arc stub
       e. Print summary: files restored, total size, compression ratio
```

**Automatic Restore Notifications:**

The CloudFormation template configures S3 to send SNS notifications when Glacier Deep Archive restores complete. This eliminates the need to manually check restore status.

**How it works:**
1. Run `s3recall` on a GDA stub → initiates restore request
2. S3 begins restore process (12-48 hours)
3. **S3 automatically sends SNS notification** when restore completes
4. You receive email/notification
5. Run `s3recall` again to download the restored archive

**Setup (already configured in CloudFormation):**
- SNS topic created: `<stack-name>-restore-notifications`
- S3 bucket configured to send `s3:ObjectRestore:Completed` events
- FSx filesystems tagged with SNS topic ARN

**Subscribe to notifications:**
```bash
# Get the topic ARN from CloudFormation outputs
aws sns subscribe \
    --topic-arn arn:aws:sns:us-east-1:123456789:s3arc-test-restore-notifications \
    --protocol email \
    --notification-endpoint your-email@example.com
```

**Without notifications:** You must periodically run `s3recall` to check if the restore is complete.

### 5.5 Per-File Stub Anatomy

After archiving `/mnt/fsx/reports/q1-summary.pdf`:

```
Filesystem:
  /mnt/fsx/reports/q1-summary.pdf.s3arc
    Contents (JSON):
      {"type": "file",
       "s3key": "FSxONTAP/fs-xxx/reports/q1-summary.pdf",
       "bucket": "my-archive-bucket",
       "storage_class": "GLACIER_IR",
       "checksum": "abc123...base64..."}
    mtime: (preserved from original)
    mode:  (preserved from original)

S3:
  s3://my-archive-bucket/FSxONTAP/fs-xxx/reports/q1-summary.pdf
    StorageClass: GLACIER_IR
    ChecksumSHA256: abc123...base64...
    Metadata:
      original-mtime: 1705334400.0
      original-size:  5242880
      original-mode:  0o100644
      original-uid:   1000
      original-gid:   1000
```

### 5.6 Aggregate Stub Anatomy

After archiving `/mnt/fsx/project-alpha/old-simulations/`:

```
Filesystem:
  /mnt/fsx/project-alpha/old-simulations/old-simulations_files.tar.gz.s3arc
    Contents (JSON):
      {"type": "aggregate",
       "s3key": "FSxONTAP/fs-xxx/project-alpha/old-simulations/old-simulations_files.tar.gz",
       "bucket": "my-archive-bucket",
       "storage_class": "DEEP_ARCHIVE",
       "manifest": {"directory": "old-simulations",
                     "files": 847,
                     "file_list": ["file1.dat", "file2.dat", ...],
                     "original_bytes": 53687091200,
                     "compressed_bytes": 16106127360,
                     "compression_ratio": 3.33,
                     "created": "2026-03-10T14:30:00Z"}}
    mtime: (preserved from original directory)

S3:
  s3://my-archive-bucket/FSxONTAP/fs-xxx/project-alpha/old-simulations/old-simulations_files.tar.gz
    StorageClass: DEEP_ARCHIVE
    Metadata:
      directory:       old-simulations
      file-count:      847
      original-size:   53687091200
      compressed-size: 16106127360
```


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 6. Aggregate Archive — Use Case and Rationale

### 6.1 When to Use Aggregate Mode

Aggregate mode targets data that is:

- **Completed projects** — closed contracts, finished simulations, delivered reports
- **Regulatory/compliance retention** — ITAR, FedRAMP, or contract-mandated 30-year retention
- **Rarely or never accessed** — "we'll probably never look at this again, but we legally can't delete it"
- **Large directory trees with many small files** — configs, logs, intermediate outputs, source code

Per-file stubbing doesn't make sense for this data. Nobody is going to recall `run001/logs/run.log` from a simulation that finished 3 years ago. If they need anything, they need the whole project back.

### 6.2 Cost Advantage

| Storage Tier | Cost (commercial, approx.) |
|---|---|
| FSx for ONTAP SSD | ~$125/TB/month |
| FSx for Lustre Persistent | ~$145/TB/month |
| S3 Standard | ~$23/TB/month |
| Glacier Instant Retrieval | ~$4/TB/month |
| **Glacier Deep Archive** | **~$1/TB/month** |

For a government contractor with 500 TB of completed project data retained for 30 years:

- GIR: ~$24,000/year → $720,000 over 30 years
- GDA: ~$6,000/year → $180,000 over 30 years
- **Savings: ~$540,000 over 30 years**

With compression (typical 2–3x on text-heavy project directories), the GDA cost drops further.

### 6.3 Object Count Reduction

A directory with 100,000 small files archived per-file means:

- 100,000 S3 objects
- 100,000 PUT requests (~$5 in GovCloud)
- 100,000 stub files on the filesystem
- 100,000 inodes consumed on the file system

One tar.gz aggregate means:

- 1 S3 object
- 1 PUT request
- 1 stub file
- 1 inode

This dramatically reduces S3 request costs, file system metadata overhead, and operational complexity.

### 6.4 Compression Benefit

Text-heavy project directories (logs, configs, CSVs, source code, XML, JSON) compress well under gzip. A 50 GB directory may compress to 15–20 GB. Binary data (images, compiled binaries, HDF5) compresses less, but the aggregate approach still reduces object count overhead.

### 6.5 Why tar.gz Over zip

- `tar.gz` preserves Unix permissions, ownership, symlinks, and xattrs natively. `zip` does not reliably preserve Unix metadata.
- `tar` can stream — pipe `tar cz` directly into an S3 multipart upload without writing the full archive to local disk. This matters when the directory is larger than available scratch space.
- `tar.gz` is the standard archive format in Linux/NFS environments.
- Tradeoff: tar.gz requires full extraction to retrieve a single file. For this use case (recall the whole project), that's acceptable.

### 6.6 Glacier Deep Archive Retrieval

GDA does not support instant retrieval. Restoring data requires an asynchronous process:

| Retrieval Tier | Time | Cost (commercial, approx.) |
|---|---|---|
| Standard | Within 12 hours | ~$0.02/GB |
| Bulk | Within 48 hours | ~$0.0025/GB |

There is no Expedited tier for GDA.

**How RestoreObject works:**

1. You call `RestoreObject` on the S3 object, specifying the retrieval tier and the number of days to keep the temporary copy available.
2. S3 creates a temporary copy of the object in S3 Standard (or One Zone-IA) that is accessible for the specified duration.
3. The original object remains in GDA — the temporary copy is an additional, time-limited copy.
4. After the specified days, the temporary copy expires automatically.

For s3recall, the default restore window is 7 days, giving the user time to download and extract.

### 6.7 GDA Minimum Storage Duration

Glacier Deep Archive has a 180-day minimum storage charge. If you archive an object and delete it within 180 days, you are still billed for the full 180 days. This reinforces that aggregate mode is for data you are confident is truly cold and will remain archived for months or years.


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 7. Usage

### 7.1 CloudFormation Test Stack

A CloudFormation template (`cloudformation/s3arc-test-stack.yaml`) is provided to quickly deploy a complete test environment:

**What it creates:**
- VPC with public and private subnets
- FSx for NetApp ONTAP filesystem (1TB, 512 MB/s throughput)
- FSx for Lustre filesystem (1.2TB Persistent_2)
- EC2 instance (c5n.4xlarge with 25 Gbps network, Amazon Linux 2023)
- S3 archive bucket with proper access policies
- IAM roles with FSx and S3 permissions
- Both filesystems mounted at `/mnt/fsx` (ONTAP) and `/mnt/lustre`
- S3Arc scripts installed in `/usr/local/bin`
- All FSx configuration tags pre-configured

**Deploy the stack:**
```bash
aws cloudformation create-stack \
    --stack-name s3arc-test \
    --template-body file://cloudformation/s3arc-test-stack.yaml \
    --parameters ParameterKey=KeyPairName,ParameterValue=my-keypair \
    --capabilities CAPABILITY_IAM
```

Once deployed, SSH to the EC2 instance and the scripts are ready to use.

### 7.2 Configuration

S3Arc retrieves configuration from **FSx filesystem tags** (preferred) or **environment variables** (fallback).

**Configuration lookup order:**
1. FSx filesystem tags (if target is on FSx mount)
2. Environment variables
3. Hardcoded defaults

**FSx filesystem tags** (set via CloudFormation or AWS CLI):

| Tag Name | Description | Default |
|----------|-------------|---------|
| `ArchiveBucket` | S3 bucket for archived files | (required) |
| `StorageClass` | Storage class (GLACIER_IR, DEEP_ARCHIVE) | `GLACIER_IR` |
| `SnsTopicArn` | SNS topic for notifications | (none) |
| `RestoreDays` | Days to keep GDA restored copy | `7` |
| `RestoreTier` | GDA retrieval tier (Standard, Bulk) | `Standard` |

The CloudFormation template sets these tags automatically. To modify them:
```bash
aws fsx tag-resource \
    --resource-arn arn:aws:fsx:us-east-1:123456789:file-system/fs-0123456789abcdef \
    --tags Key=StorageClass,Value=DEEP_ARCHIVE
```

**Environment variables** (fallback if FSx tags not set):

| Variable | Description |
|----------|-------------|
| `S3ARC_BUCKET` | S3 bucket name |
| `S3ARC_STORAGE_CLASS` | Storage class |
| `S3ARC_SNS_TOPIC` | SNS topic ARN |
| `S3ARC_RESTORE_DAYS` | Days for GDA restore |
| `S3ARC_RESTORE_TIER` | GDA retrieval tier |

The S3 key prefix is auto-generated from the FSx filesystem type and ID (e.g., `FSxONTAP/fs-xxx/` or `FSxLustre/fs-xxx/`). The path after the prefix is relative to the mount point, so the bucket contents mirror the original filesystem structure.

Example:
```bash
export S3ARC_BUCKET="my-archive-bucket"
export S3ARC_STORAGE_CLASS="GLACIER_IR"
export S3ARC_SNS_TOPIC="arn:aws:sns:us-east-1:123456789:s3arc-notifications"
```

### 7.3 Commands

**Per-file archival (default):**

Archive a single file:
```bash
s3archive /mnt/fsx/project-alpha/old-reports/q1-summary.pdf
```

Archive all files in a directory (recursive):
```bash
s3archive /mnt/fsx/project-alpha/old-reports/
```

Archive to online tier (Glacier Instant Retrieval — millisecond access):
```bash
s3archive -online /mnt/fsx/project-alpha/old-reports/
```

Archive to offline tier (Glacier Deep Archive — lowest cost, 12-48h restore):
```bash
s3archive -offline /mnt/fsx/project-alpha/old-reports/
```

Dry run — show what would be archived without doing it:
```bash
s3archive --dry-run /mnt/fsx/project-alpha/old-reports/
```

Disable progress indicator:
```bash
s3archive --no-progress /mnt/fsx/project-alpha/old-reports/
```

Use more parallel workers for many small files (default: 8):
```bash
s3archive --workers 16 /mnt/fsx/project-alpha/logs/
```

Use fewer workers for large files or limited bandwidth:
```bash
s3archive --workers 4 /mnt/fsx/project-alpha/datasets/
```

**Aggregate archival:**

Archive an entire directory tree as a single compressed archive:
```bash
s3archive --aggregate /mnt/fsx/project-alpha/old-simulations/
```

**Recall (auto-detects mode from stub JSON):**

Recall a per-file stub:
```bash
s3recall /mnt/fsx/project-alpha/old-reports/q1-summary.pdf.s3arc
```

Recall all per-file stubs in a directory:
```bash
s3recall /mnt/fsx/project-alpha/old-reports/
```

Recall an aggregate stub (initiates GDA restore on first run):
```bash
s3recall /mnt/fsx/project-alpha/old-simulations.s3arc
```

Dry run — show what would be recalled:
```bash
s3recall --dry-run /mnt/fsx/project-alpha/old-reports/
```

Use more parallel workers for many small files (default: 8):
```bash
s3recall --workers 16 /mnt/fsx/project-alpha/logs/
```

Use fewer workers for large files or limited bandwidth:
```bash
s3recall --workers 4 /mnt/fsx/project-alpha/datasets/
```

**List archived stubs:**

List all stubs in a directory with metadata:
```bash
ls-s3arc /mnt/fsx/project-alpha/
```

Include live S3 storage class and restore status:
```bash
ls-s3arc --check-status /mnt/fsx/project-alpha/
```

Output as JSON (for scripting):
```bash
ls-s3arc --json /mnt/fsx/project-alpha/
```

### 7.4 SNS Notifications

S3Arc can send SNS notifications on archive/recall completion. This is useful for automation, monitoring, and alerting.

To set up notifications:

1. Create an SNS topic (see [Getting started with Amazon SNS](https://docs.aws.amazon.com/sns/latest/dg/sns-getting-started.html))
2. Subscribe to the topic (email, Lambda, SQS, etc.)
3. Set the `S3ARC_SNS_TOPIC` environment variable

Example:
```bash
# Create topic
aws sns create-topic --name s3arc-notifications

# Subscribe your email
aws sns subscribe \
    --topic-arn arn:aws:sns:us-east-1:123456789:s3arc-notifications \
    --protocol email \
    --notification-endpoint your-email@example.com

# Configure S3Arc
export S3ARC_SNS_TOPIC="arn:aws:sns:us-east-1:123456789:s3arc-notifications"
```

Notification messages include:
- Operation type (archive or recall)
- Target path
- Number of files processed
- Total bytes transferred
- Failure count

### 7.5 Per-File Example Session

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
-rw-r--r--  1 user group   186  Jan 15 2024  q1-summary.pdf.s3arc
-rw-r--r--  1 user group   184  Mar 22 2024  q2-summary.pdf.s3arc
-rw-r--r--  1 user group   186  Dec 01 2025  q4-summary.pdf.s3arc

$ s3recall /mnt/fsx/project-alpha/old-reports/
  recalled: .../q1-summary.pdf
  recalled: .../q2-summary.pdf
  recalled: .../q4-summary.pdf

Complete: 3 recalled, 0 failed
```

### 7.5 Aggregate Example Session

```
$ du -sh /mnt/fsx/project-alpha/old-simulations/
47G     /mnt/fsx/project-alpha/old-simulations/

$ find /mnt/fsx/project-alpha/old-simulations/ -type f | wc -l
847

$ s3archive --aggregate /mnt/fsx/project-alpha/old-simulations/
  compressing: /mnt/fsx/project-alpha/old-simulations/ (847 files, 23 dirs)
  compressed:  47.0 GB -> 15.2 GB (3.1x ratio)
  uploading:   archived/project-alpha/old-simulations.tar.gz (15.2 GB)
  uploaded:    verified (15.2 GB)
  stubbed:     /mnt/fsx/project-alpha/old-simulations.s3arc

Complete: 1 aggregate archived (847 files, 23 dirs, 3.1x compression)

$ ls -la /mnt/fsx/project-alpha/
-rw-r--r--  1 user group   512  Mar 10 2026  old-simulations_files.tar.gz.s3arc
drwxr-xr-x  4 user group   128  Mar 10 2026  active-work/

# First recall attempt — initiates GDA restore
$ s3recall /mnt/fsx/project-alpha/old-simulations.s3arc
  aggregate restore: initiating Standard retrieval for
    s3://my-project-archive/archived/project-alpha/old-simulations.tar.gz
  Restore initiated. Object will be available in ~12 hours.
  Run this command again to check status and complete recall.

# 12 hours later — restore complete, download and extract
$ s3recall /mnt/fsx/project-alpha/old-simulations.s3arc
  aggregate restore: temporary copy available (expires in 7 days)
  downloading:  archived/project-alpha/old-simulations.tar.gz (15.2 GB)
  extracting:   /mnt/fsx/project-alpha/old-simulations/ (847 files, 23 dirs)
  verified:     847 files, 47.0 GB matches manifest

Complete: 1 aggregate recalled (847 files, 23 dirs)
```


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 8. Security Model

### Ownership Enforcement on Archive

s3archive enforces file ownership to prevent users from archiving (and deleting) other users' files:

- **Per-file mode**: non-root users can only archive files they own. If a file's uid doesn't match the caller's uid, the file is skipped with an error.
- **Aggregate mode**: before any archiving begins, the entire directory tree is scanned. If any file is owned by a different uid, the operation is refused upfront with a list of foreign-owned files. No files are archived or deleted.
- **Root** can archive any file in either mode.

### Ownership Enforcement on Recall

s3recall enforces file ownership during restore to prevent users from restoring other users' files:

- **Per-file mode**: the caller's uid is compared against `original-uid` stored in the S3 object's metadata. If they don't match, the recall is refused and the stub is left intact.
- **Aggregate mode**: the caller's uid is compared against the archiver's uid stored in the S3 object's metadata. If they don't match, the recall is refused and the stub is left intact.
- **Root** can restore any file or archive. Per-file recall chowns the restored file to the original uid/gid. Aggregate recall extracts with tar, which natively preserves per-file uid/gid when run as root.

This ensures that even if a user can read another user's `.s3arc` stub file, they cannot use it to archive or restore the file.

### Trust Model — S3 as Authoritative Source

The ownership check reads `original-uid` from **S3 object metadata**, not from the local stub file. This is intentional:

- **Stub files** on the filesystem are untrusted pointers — they contain only the S3 bucket, key, storage class, and checksum. No ownership or permission data is stored in the stub.
- **S3 object metadata** is the authoritative source for file attributes (uid, gid, mode, mtime). It is protected by IAM policies and bucket permissions.
- Even if a stub file is tampered with on disk, the ownership check still comes from S3.

### Metadata Preserved

| Attribute | Stored In | Restored On Recall |
|-----------|-----------|-------------------|
| mtime | S3 object metadata | ✅ Always |
| mode (permissions) | S3 object metadata | ✅ Always |
| uid (owner) | S3 object metadata | ✅ If root, or if caller matches |
| gid (group) | S3 object metadata | ✅ If root, or if caller matches |

### Per-File vs Aggregate Ownership in S3

- **Per-file**: S3 object metadata stores the original file owner's uid/gid (from `os.stat` on the file).
- **Aggregate**: S3 object metadata stores the archiver's uid/gid (from `os.getuid()`/`os.getgid()`). Individual file ownership is preserved inside the tar.gz archive and restored by tar on extraction.


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 9. Error Handling

Both utilities handle the following error conditions:

| Condition | Behavior |
|---|---|
| S3 credentials invalid | Exit with error before processing |
| S3 bucket not accessible | Exit with error before processing |
| File not readable | Skip file, log error, continue |
| S3 upload failure | Skip file, log error, continue |
| Upload size mismatch | Skip file, log error, do not create stub |
| xattr not supported | N/A — stubs use JSON files, no xattr dependency |
| Cannot create stub file | Skip file, log error, continue |
| Cannot remove original | Log error (data safe in S3 and stub exists) |
| S3 object not found on recall | Skip file, log error, continue |
| Object in Glacier/Deep Archive | Initiate async restore, log status, skip |
| Download failure | Clean up partial file, log error, continue |
| Download size mismatch | Log warning, file still restored |
| Cannot restore metadata | Log warning, file still restored |
| Ownership mismatch (per-file) | Skip file, log error, stub left intact |
| Original path already exists on recall | Skip to prevent overwrite |
| Already stubbed file | Skip silently |
| Empty file (0 bytes) | Skip (nothing to archive) |
| Symlinks | Skipped (not followed) |

**Additional error handling for aggregate mode:**

| Condition | Behavior |
|---|---|
| Mixed ownership (archive) | Refuse upfront, list foreign-owned files, no files archived |
| Ownership mismatch (recall) | Refuse, stub left intact |
| Compression failure | Exit with error, do not upload or remove directory |
| Insufficient scratch space for tar.gz | Exit with error before starting |
| tar.gz upload failure | Clean up local tar.gz, exit with error |
| Directory removal failure after upload | Log error (data safe in S3, stub exists) |
| Extraction failure on recall | Clean up partial extraction, log error |
| Manifest mismatch on recall | Log warning (file count or size differs) |
| GDA restore not yet initiated | Initiate restore, print status, exit |
| GDA restore in progress | Print status, exit |
| GDA temporary copy expired | Re-initiate restore, print status, exit |

Both commands print a summary on completion and exit with code 1 if any files failed.


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 10. Data Recovery Without Scripts

If the s3archive/s3recall scripts are lost, data is still recoverable:

### Per-file stubs — read JSON metadata:
```bash
cat /mnt/fsx/path/file.s3arc
# Shows: {"type": "file", "s3key": "FSxONTAP/fs-xxx/path/file.pdf", "bucket": "my-archive-bucket", ...}
```

### Per-file stubs — download directly with AWS CLI:
```bash
aws s3 cp s3://my-archive-bucket/FSxONTAP/fs-xxx/path/file.pdf ./file.pdf
```

### Aggregate stubs — read manifest:
```bash
cat /mnt/fsx/path/dir_files.tar.gz.s3arc
# Shows JSON with s3key, bucket, and manifest containing file list
```

### Aggregate stubs — restore from GDA and extract:
```bash
# Initiate restore (wait 12-48 hours)
aws s3api restore-object \
  --bucket my-archive-bucket \
  --key "FSxONTAP/fs-xxx/path/dir_files.tar.gz" \
  --restore-request '{"Days": 7, "GlacierJobParameters": {"Tier": "Standard"}}'

# Check restore status
aws s3api head-object \
  --bucket my-archive-bucket \
  --key "FSxONTAP/fs-xxx/path/dir_files.tar.gz"

# Download and extract once restore completes
aws s3 cp "s3://my-archive-bucket/FSxONTAP/fs-xxx/path/dir_files.tar.gz" ./dir_files.tar.gz
tar xzf dir_files.tar.gz
```

### Browse all archived objects:
```bash
aws s3 ls s3://my-archive-bucket/ --recursive
```

The S3 key structure mirrors the original filesystem path, making manual recovery straightforward for both per-file and aggregate archives.


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 11. Stub File Storage Considerations

### Per-file stubs

Each per-file stub is a small JSON file (~150-200 bytes) containing:
- `type` — "file"
- `s3key` — S3 object key (~80-120 bytes)
- `bucket` — S3 bucket name (~20-80 bytes)
- `storage_class` — storage class used
- `checksum` — SHA-256 checksum (base64, 44 bytes)

### Aggregate stubs

Each aggregate stub is a JSON file (~500-2000 bytes depending on file list) containing all per-file fields plus:
- `manifest` — embedded JSON with directory name, file count, file list, sizes, compression ratio, and timestamp

### Filesystem Compatibility

Stub files use standard JSON in regular files — no extended attributes (xattrs) required. This ensures compatibility across all filesystems:
- FSx for ONTAP (NFS) — works (xattrs not supported on FSx ONTAP)
- FSx for Lustre — works (user.* xattrs not supported)
- Local EBS/EFS — works
- Any POSIX filesystem — works

At extreme scale (1 billion+ stubs), the aggregate metadata consumption from stub file content is minimal (~200 bytes per stub vs 0 bytes for the old zero-byte approach). Aggregate stubs dramatically reduce this by replacing thousands of stubs with a single stub.


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 12. Prerequisites and Limitations

### Prerequisites
- Python 3.9+
- boto3 Python package
- AWS credentials with s3:PutObject, s3:GetObject, s3:HeadObject, s3:HeadBucket, s3:RestoreObject permissions
- Amazon FSx file system (ONTAP via NFS, or Lustre) mounted
- S3 bucket with appropriate permissions and optional Object Lock configuration
- Sufficient local scratch space for aggregate mode (tar.gz creation and extraction)

### Limitations — Per-File Mode
- Not transparent — users must know to use s3recall to restore files
- No automatic policy-based archival (manual invocation only)
- No parallel processing (files processed sequentially)
- Renaming a .s3arc stub does not break the association — the S3 key is stored inside the stub JSON, not derived from the filename
- Copying stubs preserves the S3 pointer since metadata is in the file content (no xattr preservation needed)
- No integration with S3 Object Lock — must be configured separately on the bucket
- Glacier Instant Retrieval has a 90-day minimum storage duration charge
- NFS permissions only — S3Arc preserves POSIX metadata (uid, gid, mode, mtime) which is sufficient for NFS environments. CIFS/SMB NTFS ACLs, SID-based ownership, alternate data streams, and DOS attributes are not captured. A future revision may add NTFS ACL preservation via `smbcacls` or the ONTAP REST API for environments that require full SMB permission fidelity.

### Limitations — Aggregate Mode
- All-or-nothing recall — cannot retrieve a single file from an aggregate archive without downloading and extracting the entire tar.gz
- Asynchronous recall — 12 to 48 hours before data is available (GDA limitation)
- Scratch space required — must have enough local disk to hold the tar.gz during creation and extraction
- 180-day minimum storage charge on GDA — do not use for data that may need to be deleted within 6 months
- Compression ratio varies — binary data (images, HDF5, compiled binaries) may not compress well
- No incremental updates — if you need to add files to an archived directory, you must recall, modify, and re-archive
- S3 5 TB object size limit — Amazon S3 enforces a maximum object size of 5 TB. If a compressed tar.gz archive exceeds this limit, the upload will fail. A future revision will address this by automatically splitting the archive into multiple S3 objects and tracking them in the stub manifest. For now, the workaround is to archive subdirectories separately so that each resulting tar.gz stays under 5 TB.

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


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 12. Comparison to FSx Built-in Tiering

This section compares s3arc to the built-in tiering capabilities of FSx for NetApp ONTAP (FabricPool) and FSx for Lustre (S3 data repository associations).

### Advantages of s3arc

- **File/directory granularity** — you choose exactly what gets archived. FabricPool operates on cold data blocks automatically; you cannot say "archive this directory now."
- **Glacier storage classes** — FabricPool only tiers to S3 Standard or S3 Standard-IA. It cannot target Glacier classes directly. s3arc puts data into GIR (per-file) or GDA (aggregate), and S3 lifecycle policies can transition objects further.
- **Visible stubs** — the `.s3arc` extension makes it immediately obvious which files or directories are archived. FabricPool is invisible to users, and reads of cold blocks silently incur latency with no indication the data was tiered.
- **S3 lifecycle compatibility** — archived objects are native S3 objects, so standard S3 lifecycle policies can transition them across storage classes over time. FabricPool stores data in a proprietary block format that cannot be lifecycled.
- **Object Lock / WORM** — S3 Object Lock (Compliance mode) can be applied to archived objects for government retention requirements. FabricPool's cloud tier does not support Object Lock.
- **No surprise retrieval costs** — with FabricPool, any read of a cold file silently pulls blocks from S3, incurring retrieval costs that are difficult to predict. With s3arc, recall is explicit and intentional.
- **Vendor independence** — data in S3 is stored as plain objects with human-readable keys and self-describing metadata. No dependency on ONTAP or Lustre to access it. Data survives a file system migration or decommission.
- **Cross-filesystem portability** — the same tool works on FSx for NetApp ONTAP, FSx for Lustre, EFS, or any NFS mount. It is not tied to one file system's tiering implementation.
- **Aggregate mode enables GDA pricing** — FabricPool cannot tier to Glacier Deep Archive. Aggregate mode unlocks the lowest S3 storage tier (~$1/TB/month) for bulk cold data.

### Disadvantages of s3arc

- **Manual operation** — someone must decide what to archive and run the command. FabricPool is fully automatic based on data temperature.
- **Not transparent to users** — users see `.s3arc` stub files and must explicitly recall them. FabricPool recall is seamless and invisible to applications.
- **No automatic re-tiering** — if a recalled file goes cold again, it remains on the file system unless someone runs s3archive again. FabricPool continuously manages hot/cold placement.
- **Operational overhead** — custom tooling must be maintained, documented, and supported. FabricPool is built-in and fully managed by AWS/NetApp.

### Summary

s3arc and built-in tiering solve different problems. s3arc is designed for intentional, policy-driven archival with full control over storage class, retention, and compliance. Built-in tiering is designed for automatic, transparent hot/cold data management. The two approaches can be used together: FabricPool for day-to-day automatic tiering, s3arc per-file mode for explicit archival to GIR, and s3arc aggregate mode for bulk long-term archival to GDA with Object Lock compliance.


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 13. Future Enhancements

- `--dry-run` flag to preview operations without executing
- `--mtime-older-than` filter for policy-based archival
- Parallel file processing (multiprocessing pool)
- Transaction log for resumable operations
- SNS notification on GDA restore completion
- Bash/AWS CLI version with zero Python dependencies
- Integration with cron or EventBridge for scheduled archival
- Streaming tar.gz creation with direct S3 multipart upload (no local scratch needed)
- `--list-contents` flag to display aggregate archive manifest without recalling
- Selective file extraction from aggregate archives (tar index-based)
- Support for S3 Glacier Flexible Retrieval as an intermediate tier option

---


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## 14. Example Use Cases

| Industry | Use Case | Data Pattern | Typical Retention |
|---|---|---|---|
| Government / Defense | Completed contracts, ITAR/FedRAMP compliance retention | Projects close, data legally retained but rarely accessed | 7–30+ years |
| National Labs / HPC | Simulation campaigns, instrument output | Massive burst output, analyzed, then archived | 5–20 years |
| Media & Entertainment | Film/episodic production, post-production assets | Active during production, entire project archived at wrap | 5–10 years |
| Electronic Design Automation (EDA) | Chip design verification runs | Hundreds of TB active during verification, cold between respins | 5–15 years |
| Life Sciences / Genomics | Sequencing runs, clinical trial data | Processed once, archived for regulatory retention | 15–30 years |
| Financial Services | Trading data, risk model outputs, audit trails | Active during reporting period, then compliance archive | 7–10 years |
| Oil & Gas | Seismic surveys, well logs, reservoir simulations | Campaign-based, massive datasets with clear project boundaries | 10–30 years |
| Architecture / Engineering / Construction | Building models, drawings, project deliverables | Active during construction, archived for liability retention | 10–20 years |
| Legal / eDiscovery | Case files, litigation holds | Active during case, archived at close, may need rapid recall | 5–10 years |
| Pharma / Clinical Trials | Trial data, regulatory submissions | Active during study, archived for FDA/EMA retention | 15–25 years |
| CFD / Seismic Modelling | Simulation output, intermediate results | Generated in bursts, analyzed intensively, then cold | 5–15 years |
| Autonomous Vehicles / Robotics | Sensor data from test campaigns | Petabytes per campaign, kept for reproducibility | 3–10 years |
| AI/ML Training | Training datasets, model checkpoints, experiment logs | Active during training, archived for reproducibility | 1–5 years |

---


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## Appendix A: s3arc_common.py

Shared module containing constants, utility functions, and the `ProgressTracker` class used by all three scripts.


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## Appendix B: s3archive.py

See `s3archive.py` in this project directory.


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## Appendix C: s3recall.py

See `s3recall.py` in this project directory.


```{=openxml}
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
```

## Appendix D: ls-s3arc.py

See `ls-s3arc.py` in this project directory.

---

*Generated: 2026-03-11 — v2 with aggregate archive capability*
