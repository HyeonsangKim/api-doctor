import shutil
import subprocess

import pytest


def _docker_ready() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        return subprocess.run(
            [docker, "info"], capture_output=True, timeout=30
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_ready(), reason="docker daemon 이 필요합니다"
)
