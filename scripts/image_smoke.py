#!/usr/bin/env python3
"""Execute a built image without provider connectivity, including graceful shutdown."""

import argparse
import os
import subprocess
import time
import uuid


def smoke(image, platform):
    name = "cahuita-smoke-" + uuid.uuid4().hex[:10]
    docker = ["docker", "--context", os.environ.get("CAHUITA_CONTEXT", "default")]

    def run(*args, check=True):
        return subprocess.run(docker + list(args), text=True, capture_output=True, check=check)

    try:
        run("run", "-d", "--name", name, "--platform", platform, "--network=none", image)
        code = """
import urllib.request,urllib.error
for path,expected in [('/',200),('/health/live',200),('/health/ready',503),('/metrics',200)]:
    try: response=urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=2)
    except urllib.error.HTTPError as error: response=error
    with response:
        assert response.status==expected,(path,response.status)
        if path=='/metrics': assert b'forecast_data_usable' in response.read()
print('Liveness, HTML and metrics respond; readiness correctly rejects an empty cache.')
"""
        for _ in range(30):
            result = run("exec", name, "python", "-c", code, check=False)
            if result.returncode == 0:
                print(result.stdout)
                break
            time.sleep(2)
        else:
            raise RuntimeError("Container endpoints did not reach the expected initial state")
        run("stop", "--time", "30", name)
        logs = run("logs", name).stdout
        exit_code = run("inspect", "--format", "{{.State.ExitCode}}", name).stdout.strip()
        if exit_code not in {"0", "143"} or "application_stopped" not in logs:
            raise RuntimeError("Container did not complete an orderly shutdown")
    finally:
        run("rm", "-f", "-v", name, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", choices=["linux/amd64", "linux/arm64"], required=True)
    args = parser.parse_args()
    smoke(args.image, args.platform)
