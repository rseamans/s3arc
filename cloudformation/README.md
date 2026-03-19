# S3Arc Test Environment

CloudFormation template to create a test environment for S3Arc.

## Resources Created

- S3 bucket for archive (tagged on FSx, auto-emptied on stack delete)
- FSx for NetApp ONTAP (Single-AZ, 1TB, 512 MB/s)
- FSx for Lustre (Persistent_2, 1.2TB, 125 MB/s)
- SVM and volume mounted at `/mnt/fsx`
- Lustre mounted at `/mnt/lustre`
- EC2 instance (Amazon Linux 2023, c5n.4xlarge with 25Gbps network)
- VPC, subnets, security groups
- IAM role with S3, FSx, and SNS permissions
- SNS topic for restore notifications
- Lambda function to empty S3 bucket on stack delete

## Prerequisites

- AWS CLI configured (with default region set via `aws configure`)
- EC2 key pair in your target region

## Deploy

```bash
aws cloudformation create-stack \
    --stack-name s3arc-test \
    --template-body file://s3arc-test-stack.yaml \
    --parameters \
        ParameterKey=KeyPairName,ParameterValue=YOUR_KEY_PAIR \
        ParameterKey=FsxAdminPassword,ParameterValue=YOUR_PASSWORD \
    --capabilities CAPABILITY_IAM
```

## Check Status

The stack takes approximately 30 minutes to complete, primarily waiting for FSx filesystem creation.

```bash
aws cloudformation describe-stacks --stack-name s3arc-test \
    --query 'Stacks[0].StackStatus' --output text
```

Wait for `CREATE_COMPLETE` before connecting.

## Get Outputs

```bash
aws cloudformation describe-stacks --stack-name s3arc-test \
    --query 'Stacks[0].Outputs' --output table
```

## Connect to EC2

```bash
ssh -i your-key.pem ec2-user@<Ec2PublicIp>
```

## Test S3Arc

```bash
# On EC2 instance
cd /mnt/fsx
echo "test data" > testfile.txt

# Run s3archive
python3 /path/to/s3archive.py testfile.txt
```

## Delete Stack

The stack includes a Lambda function that automatically empties the S3 bucket on delete.

```bash
aws cloudformation delete-stack --stack-name s3arc-test
```

## Estimated Costs

⚠️ The test stack runs ~$1.38/hour (~$33/day). A few days of testing costs roughly $75–150. Delete the stack when done to avoid ongoing charges.

## Future Planned Features

### Vaulting (S3 Object Lock / WORM Protection)

To support immutable archiving, the CloudFormation template will need the following changes:

**Scenario 1: Single bucket with optional per-object retention**
- Add `ObjectLockEnabled: true` to the `ArchiveBucket` resource (requires bucket recreation)
- Versioning is automatically enabled with Object Lock
- No default retention policy on the bucket — retention is applied per-object by `s3archive.py` via `--vault`, `--retain-mode`, `--retain-days` flags
- Allows plain and vaulted archives in the same bucket

**Scenario 2: Separate vault bucket**
- Add a second S3 bucket resource with Object Lock enabled and a default `ObjectLockConfiguration` specifying mode and retention period
- Add a parameter for vault retention mode (`GOVERNANCE` or `COMPLIANCE`) and retention days
- IAM policy updated to grant `s3:PutObject` on both buckets

**Scenario 3: Legal hold support**
- No template changes required beyond Object Lock being enabled on the bucket
- Legal holds are applied per-object at upload time by `s3archive.py` via `--legal-hold`
- IAM policy must include `s3:PutObjectLegalHold`

**IAM Policy Additions for Vaulting**
- `s3:PutObjectRetention` — required for setting retention on upload
- `s3:GetObjectRetention` — required for reading retention status
- `s3:PutObjectLegalHold` — required for legal hold scenario
- `s3:BypassGovernanceRetention` — optional, for admin override in Governance mode
