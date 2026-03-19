# FSx Tag-Based Configuration

S3Arc retrieves configuration from FSx filesystem tags with environment variable fallback.

## Configuration Tags

| Tag Name | Description | Default | Example |
|----------|-------------|---------|---------|
| `ArchiveBucket` | S3 bucket for archived files | (required) | `my-archive-bucket` |
| `StorageClass` | S3 storage class | `GLACIER_IR` | `DEEP_ARCHIVE` |
| `SnsTopicArn` | SNS topic ARN for notifications | (none) | `arn:aws:sns:us-east-1:123:topic` |
| `RestoreDays` | Days to keep GDA restored copy | `7` | `14` |
| `RestoreTier` | GDA retrieval tier (Standard/Bulk) | `Standard` | `Bulk` |

## S3 Key Prefix

The S3 key prefix is auto-generated from the FSx filesystem type and ID:

```
FSxONTAP/fs-0123456789abcdef/path/to/file.dat
FSxLustre/fs-0123456789abcdef/path/to/file.dat
```

This ensures:
- Bucket contents mirror the original filesystem structure
- Different filesystems can archive to the same bucket without collision
- The filesystem path is fully reproducible from the S3 object name alone

## Lookup Order

1. **FSx filesystem tags** (if target is on FSx mount)
2. **Environment variables** (fallback)
3. **Hardcoded defaults** (last resort)

## Environment Variable Fallback

If FSx tags are not set, these environment variables are used:

```bash
export S3ARC_BUCKET="my-archive-bucket"
export S3ARC_STORAGE_CLASS="GLACIER_IR"
export S3ARC_SNS_TOPIC="arn:aws:sns:us-east-1:123456789:s3arc-notifications"
export S3ARC_RESTORE_DAYS="7"
export S3ARC_RESTORE_TIER="Standard"
```

## Setting FSx Tags

### Via AWS CLI

```bash
# Get FSx filesystem ARN
FSX_ARN=$(aws fsx describe-file-systems \
    --file-system-ids fs-0123456789abcdef \
    --query 'FileSystems[0].ResourceARN' \
    --output text)

# Set tags
aws fsx tag-resource \
    --resource-arn $FSX_ARN \
    --tags \
        Key=ArchiveBucket,Value=my-archive-bucket \
        Key=StorageClass,Value=GLACIER_IR \
        Key=SnsTopicArn,Value=arn:aws:sns:us-east-1:123:topic \
        Key=RestoreDays,Value=7 \
        Key=RestoreTier,Value=Standard
```

### Via CloudFormation

The test stack template includes configuration tags on both filesystems:

```yaml
FsxFileSystem:
  Type: AWS::FSx::FileSystem
  Properties:
    FileSystemType: ONTAP
    Tags:
      - Key: ArchiveBucket
        Value: !Sub ${ArchiveBucketName}-${AWS::AccountId}
      - Key: StorageClass
        Value: GLACIER_IR
      - Key: SnsTopicArn
        Value: !Ref RestoreNotificationTopic
      - Key: RestoreDays
        Value: "7"
      - Key: RestoreTier
        Value: Standard
```

## Two Dimensions of Control

1. **`ArchiveBucket` tag** — controls which S3 bucket receives archives (per-filesystem)
2. **Auto-generated prefix** — identifies the source filesystem within the bucket (`FSxONTAP/fs-xxx/` or `FSxLustre/fs-xxx/`)

This allows flexible topologies:
- All filesystems → one bucket (separated by prefix)
- Each filesystem → its own bucket
- Mixed: production → one bucket, dev → another

## Verification

```bash
# s3archive shows configuration on startup
./s3archive.py /mnt/fsx/myfile.txt

# Output shows:
# Using bucket: my-archive-bucket (from FSx tag (fs-0123...))
#   Prefix: FSxONTAP/fs-0123.../
#   Storage class: GLACIER_IR
```
