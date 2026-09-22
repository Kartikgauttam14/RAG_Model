# AWS infrastructure (Terraform)

The ECS Fargate stack described in [`docs/DEPLOY-AWS.md`](../../docs/DEPLOY-AWS.md), Option B. The
step-by-step narrative — including the commands for building the image, running migrations and
verifying a deployment — lives in that document; this file is the map of the Terraform itself.

## Files

| File | Contents |
| --- | --- |
| `versions.tf` | Terraform/provider versions, the optional S3 backend (commented), the `us_east_1` provider alias for CloudFront certificates, and the common tags |
| `variables.tf` | every input, with defaults and descriptions |
| `terraform.tfvars.example` | a filled-in set of values to copy |
| `network.tf` | VPC, two AZs, public/private subnets, NAT, route tables, and the five security groups (ALB, tasks, RDS, Redis, EFS) |
| `ecr.tf` | the single image repository plus a retention policy |
| `rds.tf` | PostgreSQL 16 (`pgvector` capable), TLS-enforcing parameter group, backups, deletion protection |
| `elasticache.tf` | single-node Redis replication group, at-rest encryption, no in-transit encryption (see the note below) |
| `efs.tf` | the filesystem, per-AZ mount targets and the `/uploads` access point |
| `secrets.tf` | `JWT_SECRET`, `HF_TOKEN`, `DATABASE_URL`, `REDIS_URL`, and the bootstrap admin credentials |
| `ecs.tf` | cluster, IAM roles, the `api` / `worker` / `migrate` task definitions, the two services, and API autoscaling |
| `alb.tf` | the ALB, the `/health/live` target group, HTTP(S) listeners, and the optional Route53 record |
| `frontend.tf` | private S3 bucket, CloudFront with OAC, the SPA fallback, and the optional UI domain |
| `observability.tf` | log groups with retention, three alarms, optional SNS email |
| `outputs.tf` | everything the deployment commands need (ECR URL, cluster and service names, task definition families, subnet and security group ids, bucket, distribution) |

## Apply order

```bash
cd infra/aws
cp terraform.tfvars.example terraform.tfvars   # edit; never commit (gitignored)
terraform init
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
terraform output
```

Then, from the repository root: build and push the image
(`docker buildx build --platform linux/amd64 -f docker/backend.Dockerfile -t "$(terraform -chdir=infra/aws output -raw ecr_repository_url):$(git rev-parse --short HEAD)" --push .`),
run the migration task, create the first administrator, and deploy the frontend — all four command
blocks are in `docs/DEPLOY-AWS.md` sections B2, B8 and B9.

## Two decisions that are deliberate, not oversights

- **ElastiCache has in-transit encryption and AUTH disabled.** ElastiCache only accepts an AUTH token
  when in-transit encryption is enabled, and the application builds its Redis client with
  `Redis.from_url()` while redis-py 5 defaults `ssl_cert_reqs` to `required` — so a `rediss://` URL
  fails certificate verification against the ElastiCache CA. Change `app/dependencies.py` and the
  Celery broker URL to pass `ssl_ca_certs` first, then enable both here.
- **`DATABASE_URL` uses the RDS master user.** `pgvector` is not a trusted extension on RDS, so
  `CREATE EXTENSION vector` in migration `0001` needs `rds_superuser`, which only the master user has.

## Not included

WAF, VPC endpoints (the NAT gateway covers egress for now), multi-AZ RDS, ElastiCache failover, ALB
access logs, and an `/api/*` CloudFront behavior. Each is a deliberate omission noted in
`docs/DEPLOY-AWS.md`, and the last one needs the streaming behaviour of `/chat/stream` verified
through a CDN before it is safe to enable.
