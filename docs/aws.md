# AWS and GitHub Actions setup

Everyday operation is **Actions → AWS laboratory → deploy / start / stop / destroy**.
GitHub runs the jobs; the application stays on EC2 after the workflow finishes. Your
computer does not need Docker, Kubernetes or a runner installed.

## Connect a provided laboratory account

The AWS account can be supplied by the laboratory owner; the repository owner does not
need a separate account. When an existing backend and repository-specific OIDC role are
provided, no Terraform bootstrap is required from the recipient.

Create the GitHub **laboratory** environment, restrict deployments to the default branch,
and configure the settings below using values supplied privately by the AWS owner:

| Name | Kind | Value |
| --- | --- | --- |
| `AWS_ACCOUNT_ID` | Secret | Approved laboratory account |
| `AWS_ROLE_ARN` | Secret | Role authorized for this repository's `laboratory` environment |
| `AWS_TF_STATE_BUCKET` | Secret | Existing private backend bucket |
| `AWS_REGION` | Variable | Region agreed with the AWS owner |

Adding Secrets does not create the role or its trust policy. The owner must authorize
the exact repository/environment before the first deployment. The workflow uses OIDC
to obtain temporary credentials; no account password, copied SSO profile or permanent
AWS access-key Secrets are required. See
[GitHub's AWS OIDC guide](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).

Using the same bucket, region and project continues managing the existing laboratory.
The state key is `<project>/terraform.tfstate` (`cahuita-lab/terraform.tfstate` by default).
This includes start, stop and destructive cleanup of that stack. Coordinate ownership:
workflow concurrency is repository-local, so another repository or administrator must
not run competing operations against the same state. Do not create a fresh backend
to operate infrastructure already managed by an existing state.

GitHub deployment access does not grant the recipient a personal SSM session. The AWS
owner arranges authorized monitoring access separately, as described below.

## One-time preparation by the AWS owner

These steps are administrative setup, not tasks for a recipient who already has the
four connection values above. Use a dedicated laboratory AWS account. From a trusted
administrator environment with Terraform and authenticated AWS access, supply the
account privately through `TF_VAR_allowed_account_id`. Use the same AWS region throughout
(default `us-east-1`). Keep bootstrap state outside Git.

1. Reuse the existing private backend when handing over the same laboratory. For a
   genuinely new lab without a backend, run `terraform init` and `terraform apply` in
   `infrastructure/state`, providing `region`, `project=cahuita-lab` and the allowed
   account ID. Its `bucket` output is the backend for later GitHub runs. Keep this
   bootstrap state privately.
2. Create your GitHub **laboratory** environment, restricted to deployment from the
   default branch. Obtain the exact OIDC subject for this repository/environment.
   New repositories can include immutable owner/repository IDs in the subject.
3. Apply `infrastructure/github` with your account, region and exact `github_subject`.
   If the account already has GitHub's OIDC provider, supply its
   `existing_oidc_provider_arn`. This module creates only the scoped deployment identity;
   it does not start the application. Read its sensitive `role_arn` output privately.
4. Supply the account ID, role ARN, backend bucket and region privately to the repository
   administrator, who adds the environment settings above. Do not commit these values,
   bootstrap state or credentials. The role ARN output is sensitive in Terraform.

See [GitHub's AWS OIDC guide](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
for exact subject formats. The supplied role trusts one exact repository/environment,
limits EC2 management to the selected region, and scopes bucket/instance-role management
to the laboratory prefix. It is a privileged infrastructure role for this dedicated
account; ordinary PR CI receives no deployment credentials. No permanent AWS access keys
are stored in the repository.

## Deploy and present

Run **AWS laboratory** on the default branch, selecting `deploy` and the architecture.
ARM64 is the default (`t4g.medium`, 2 CPUs/4 GiB); AMD64 uses `t3.medium`. Keep the selected
architecture consistent for subsequent operations on that lab.

The workflow runs CI, consumes that same run's tested Docker archive, creates infrastructure
with Terraform, and invokes Ansible through Systems Manager. Ansible configures Ubuntu,
Docker, K3s and the existing application/monitoring manifests. It verifies image identity,
public endpoints, Prometheus targets and provisioned Grafana dashboards before publishing.

Terraform creates a VPC, public subnet, internet gateway/route, security group, EC2
instance, encrypted 30 GiB gp3 root disk, instance role and private release-transfer bucket.
There is no Elastic IP, EKS, NAT Gateway, public SSH or public Kubernetes endpoint.
The auto-assigned IPv4 can change after stop/start; outputs are refreshed before updating
the README. Public IPv4 and EBS can incur charges independently of compute; see
[AWS VPC pricing](https://aws.amazon.com/vpc/pricing/).

Allow roughly 10–20 minutes for a cold deployment. The burstable instance uses standard
CPU credits; building or initializing databases can be slower after credits are depleted.
Grafana's AWS overlay allows five minutes for startup and ten for rollout.

The public application is a read-only **HTTP** demonstration on port 80. No user credentials
are entered there. Its APIs return forecasts, not activity-safety recommendations.

## Private monitoring access

Grafana, Prometheus and Alertmanager remain private. Install the
[Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
and use AWS Systems Manager port forwarding to the instance's loopback ports 3000, 9090
and 9093. Use your own authenticated administrative AWS profile. The generic helper
`scripts/aws_lab.py access --service grafana` can also be used with those AWS settings;
it operates the cloud lab and does not create a workstation cluster.

In an SSM shell, run `sudo -i`, then `cd /opt/cahuita/release`. Root login loads the
explicit `cahuita-server` context and private client kubeconfig. Retrieve the initial
Grafana password only when needed with `python3 scripts/grafana.py credentials`; keep
the output out of screenshots. Ansible refreshes the client config after every K3s start
and restarts its private forwarding services when their pod connection closes.

## Stop, resume and destroy

Use the same workflow with `stop`, `start` or `destroy`:

- Stop preserves EBS, SQLite, four PVCs and monitoring history; compute/collection/delivery stop.
- Start resumes and verifies the running release, refreshes its IP and republishes the link.
- Destroy deletes the lab VM, its disk/data, network, instance identity and release bucket.
  The private Terraform backend and GitHub identity are separate bootstrap resources.

To retire the bootstrap resources too, destroy the lab first, then remove the GitHub
identity with its bootstrap state. Empty the state bucket's object versions/delete markers
before intentionally destroying its separate Terraform state module. Never delete a backend
while its lab is still active.

## README and deployment tags

After successful verification, a separate publication job updates only the generated
README block and creates `deploy-<UTC timestamp>-<source SHA>` pointing at the tested
source commit. Its documentation commit does not claim to be a new application build.
Stop/destroy removes the old links. Out-of-band AWS console changes cannot automatically
update GitHub; rerun the appropriate operation.

The job refuses to overwrite newer default-branch work. Branch rules must permit the
bot's README commit/tag, or use a reviewed PR publication alternative. Failed or incomplete
operations do not publish a successful deployment. The badge reports CI results, while
the README's timestamp is a deployment snapshot rather than live uptime monitoring.
