#!/bin/sh
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv ca-certificates curl unzip
python3 -m venv /opt/cahuita-ansible
/opt/cahuita-ansible/bin/pip install 'ansible-core==2.21.6' 'boto3==1.42.0'
# The official Ubuntu EC2 image supplies the SSM agent. No inbound SSH is used.
systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service
