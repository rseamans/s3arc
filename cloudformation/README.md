# S3Arc Test Environment

CloudFormation template to create a test environment for S3Arc.

## Resources Created

- S3 bucket for archive (tagged on FSx)
- FSx for NetApp ONTAP (Single-AZ, 1TB, 512 MB/s)
- SVM and volume mounted at `/mnt/fsx`
- EC2 instance (RHEL, c5n.9xlarge with 50Gbps network)
- VPC, subnets, security groups
- IAM role with S3 and FSx permissions

## Prerequisites

- AWS CLI configured
- EC2 key pair in us-east-1

## Deploy

```bash
aws cloudformation create-stack \
    --stack-name s3arc-test \
    --template-body file://s3arc-test-stack.yaml \
    --parameters ParameterKey=KeyPairName,ParameterValue=YOUR_KEY_PAIR \
    --capabilities CAPABILITY_IAM \
    --region us-east-1
```

## Check Status

```bash
aws cloudformation describe-stacks --stack-name s3arc-test --region us-east-1
```

Wait for `CREATE_COMPLETE` (~30-40 minutes for FSx).

## Get Outputs

```bash
aws cloudformation describe-stacks --stack-name s3arc-test \
    --query 'Stacks[0].Outputs' --output table --region us-east-1
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

**Important:** Empty the S3 bucket first, then delete the stack.

```bash
# Empty bucket
aws s3 rm s3://s3arc-test-archive-ACCOUNT_ID --recursive

# Delete stack
aws cloudformation delete-stack --stack-name s3arc-test --region us-east-1
```

## Estimated Costs

- FSx ONTAP 1TB: ~$0.35/hour (~$250/month)
- EC2 c5n.9xlarge: ~$1.94/hour
- S3 GIR: ~$0.004/GB/month

For a few days of testing, expect ~$50-100 total.
