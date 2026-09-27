"""컨테이너 기반 대안 backend (PRD §4.5, AC-10).

fixture 실행 전용이다. 네트워크가 완전히 없으므로 후보의 모든 요청은
stdin/stdout JSONL IPC 로 Zone T 의 broker 가 해소한다.

이 backend 의 결과는 **절대 OpenShell 증거로 표시하지 않는다** (AC-15).
모든 denial 에 `backend_id=container` 가 붙는다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .base import (
    BackendId,
    BoundaryCase,
    BoundaryReport,
    BoundaryResult,
    BrokerRespond,
    Denial,
    SandboxLimits,
    SandboxOutcome,
    utc_now,
)

_IMAGE = "python:3.12-alpine"
_HARNESS = Path(__file__).with_name("harness.py")

# 경계 자가검사 스크립트. 각 항목은 "차단되면 BLOCKED, 뚫리면 LEAKED" 를 출력한다.
_PROBES: dict[BoundaryCase, str] = {
    BoundaryCase.NETWORK_EGRESS: """
import socket
try:
    socket.setdefaulttimeout(3)
    socket.create_connection(("1.1.1.1", 53))
    print("LEAKED:tcp 1.1.1.1:53 connected")
except OSError as e:
    print(f"BLOCKED:{type(e).__name__}")
""",
    BoundaryCase.WRITE_OUTSIDE_WORKDIR: """
import pathlib
leaks = []
for p in ("/etc/apidoctor_probe", "/usr/apidoctor_probe", "/work/apidoctor_probe"):
    try:
        pathlib.Path(p).write_text("x")
        leaks.append(p)
    except OSError:
        pass
print(f"LEAKED:{leaks}" if leaks else "BLOCKED:read-only rootfs and ro workdir")
""",
    # 두 가지를 본다.
    #  (1) 호스트 경로가 **내용과 함께** 보이는가 (빈 이미지 디렉터리는 유출이 아니다)
    #  (2) 호스트 환경변수가 컨테이너로 전달되는가 — API key 격리의 실제 판정 지점
    BoundaryCase.READ_HOST_SECRET: """
import os, pathlib
HOST_PATHS = ["/root/.ssh", "/host", "/var/run/docker.sock", "/Users", "/home",
              "/etc/shadow", "/run/secrets"]
leaked, blocked = [], []
for t in HOST_PATHS:
    p = pathlib.Path(t)
    try:
        if p.is_dir():
            entries = os.listdir(t)
            if entries:
                leaked.append(f"{t}(listed {len(entries)})")
            else:
                blocked.append(f"{t}=empty")
        else:
            with open(t, "rb") as fh:
                data = fh.read(1)
            leaked.append(f"{t}(read)") if data else blocked.append(f"{t}=empty")
    except PermissionError:
        blocked.append(f"{t}=EACCES")
    except (FileNotFoundError, NotADirectoryError):
        blocked.append(f"{t}=absent")
    except OSError as e:
        blocked.append(f"{t}={type(e).__name__}")

# 호스트 env 유출 검사: CANARY 는 호스트 docker 프로세스에만 설정돼 있다.
env = dict(os.environ)
try:
    with open("/proc/1/environ", "rb") as fh:
        for chunk in fh.read().split(b"\\0"):
            if b"=" in chunk:
                k, _, v = chunk.partition(b"=")
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
except OSError:
    pass
# 이미지가 원래 갖고 있는 env 는 유출이 아니다. 호스트에서 추가된 것만 본다.
try:
    import json as _json
    with open("/work/baseline_env.json", encoding="utf-8") as fh:
        baseline = set(_json.load(fh))
except OSError:
    baseline = set()
if "APIDOCTOR_CANARY" in env:
    leaked.append("env:APIDOCTOR_CANARY forwarded")
extra = sorted(set(env) - baseline - {"HOSTNAME", "HOME", "PWD", "SHLVL", "_"})
if extra:
    leaked.append(f"env_added:{extra}")
blocked.append(f"env_total={len(env)},added=0")

print(f"LEAKED:{leaked}" if leaked else f"BLOCKED:{','.join(blocked)}")
""",
    BoundaryCase.PROCESS_LIMIT: """
import os, sys
spawned = 0
try:
    for _ in range(200):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        spawned += 1
except OSError as e:
    print(f"BLOCKED:pids capped after {spawned} ({type(e).__name__})")
    sys.exit(0)
print(f"LEAKED:forked {spawned}")
""",
    BoundaryCase.PRIVILEGE_ESCALATION: """
import os, subprocess
if os.geteuid() == 0:
    print("LEAKED:running as root")
else:
    try:
        r = subprocess.run(["su", "-c", "id"], capture_output=True, timeout=5)
        print("LEAKED:su succeeded" if r.returncode == 0 else f"BLOCKED:uid={os.geteuid()} su denied")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"BLOCKED:uid={os.geteuid()} ({type(e).__name__})")
""",
}


def _docker() -> str | None:
    return shutil.which("docker")


class ContainerBackend:
    """docker 기반 fixture 전용 실행기."""

    backend_id = BackendId.CONTAINER

    def __init__(self, image: str = _IMAGE) -> None:
        self.image = image

    # ---------------------------------------------------------------- 정책

    def _policy_args(self, limits: SandboxLimits) -> list[str]:
        """PRD §4.5 의 격리 정책. 모든 실행이 동일한 인자를 쓴다."""
        return [
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            "--pids-limit", str(limits.pids),
            "--memory", f"{limits.memory_mib}m",
            "--memory-swap", f"{limits.memory_mib}m",
            "--cpus", str(limits.cpus),
            "--tmpfs", f"/tmp:rw,noexec,nosuid,size={limits.tmpfs_mib}m",
            "--workdir", "/work",
        ]

    # ------------------------------------------------------------ 자가검사

    def boundary_selftest(self, limits: SandboxLimits) -> BoundaryReport:
        docker = _docker()
        if docker is None:
            return BoundaryReport(
                backend_id=self.backend_id,
                available=False,
                unavailable_reason="docker 실행 파일을 찾을 수 없습니다.",
            )
        probe = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            return BoundaryReport(
                backend_id=self.backend_id,
                available=False,
                unavailable_reason=f"docker daemon 에 연결할 수 없습니다: {probe.stderr.strip()[:200]}",
            )

        results: list[BoundaryResult] = []
        for case, script in _PROBES.items():
            results.append(self._run_probe(docker, case, script, limits))
        return BoundaryReport(
            backend_id=self.backend_id, available=True, results=tuple(results)
        )

    def _image_env(self, docker: str) -> list[str]:
        """이미지가 선언한 환경변수 **이름** 목록. 값은 읽지 않는다."""
        proc = subprocess.run(
            [docker, "image", "inspect", self.image, "--format", "{{json .Config.Env}}"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return []
        try:
            return [str(e).split("=", 1)[0] for e in json.loads(proc.stdout)]
        except (json.JSONDecodeError, TypeError):
            return []

    def _run_probe(
        self, docker: str, case: BoundaryCase, script: str, limits: SandboxLimits
    ) -> BoundaryResult:
        with tempfile.TemporaryDirectory() as tmp:
            probe_file = Path(tmp) / "probe.py"
            probe_file.write_text(script, encoding="utf-8")
            (Path(tmp) / "baseline_env.json").write_text(
                json.dumps(self._image_env(docker)), encoding="utf-8"
            )
            cmd = [
                docker, "run", "--rm",
                *self._policy_args(limits),
                "-v", f"{tmp}:/work:ro",
                self.image, "python", "/work/probe.py",
            ]
            # canary: 호스트 docker 프로세스에만 존재한다. 컨테이너 안에서 보이면 env 유출이다.
            env = {**os.environ, "APIDOCTOR_CANARY": "must-not-cross-the-boundary"}
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=limits.wall_seconds + 45, env=env,
                )
            except subprocess.TimeoutExpired:
                return BoundaryResult(case, blocked=False, detail="자가검사 시간 초과")

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if out.startswith("BLOCKED:"):
            return BoundaryResult(case, blocked=True, detail=out[len("BLOCKED:"):])
        if out.startswith("LEAKED:"):
            return BoundaryResult(case, blocked=False, detail=out[len("LEAKED:"):])
        # 컨테이너가 아예 못 뜬 경우는 "차단됨"이 아니라 "검사 불가"로 본다.
        return BoundaryResult(
            case, blocked=False, detail=f"판정 불가 rc={proc.returncode} out={out[:120]!r} err={err[:160]!r}"
        )

    # -------------------------------------------------------------- 후보 실행

    def run_candidate(
        self,
        candidate_path: Path,
        query: dict[str, Any],
        respond: BrokerRespond,
        limits: SandboxLimits,
    ) -> SandboxOutcome:
        docker = _docker()
        if docker is None:
            raise RuntimeError("docker 를 찾을 수 없습니다. 경계 자가검사를 먼저 실행하세요.")

        started = time.monotonic()
        denials: list[Denial] = []

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "candidate.py").write_text(
                candidate_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (work / "query.json").write_text(
                json.dumps(query, ensure_ascii=False), encoding="utf-8"
            )
            (work / "harness.py").write_text(
                _HARNESS.read_text(encoding="utf-8"), encoding="utf-8"
            )
            cmd = [
                docker, "run", "--rm", "-i",
                *self._policy_args(limits),
                "-v", f"{work}:/work:ro",
                self.image,
                "python", "/work/harness.py", "/work/candidate.py", "/work/query.json",
            ]
            outcome = self._pump(cmd, respond, limits, denials)

        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        return outcome

    def _pump(
        self,
        cmd: list[str],
        respond: BrokerRespond,
        limits: SandboxLimits,
        denials: list[Denial],
    ) -> SandboxOutcome:
        """컨테이너와 JSONL 로 대화하며 broker 요청을 중계한다."""
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        records: list[dict[str, Any]] | None = None
        error: str | None = None
        requests_made = 0
        emitted = 0
        truncated = False
        deadline = time.monotonic() + limits.wall_seconds
        stderr_buf: list[str] = []

        def drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_buf.append(line)

        t = threading.Thread(target=drain_stderr, daemon=True)
        t.start()

        assert proc.stdin is not None and proc.stdout is not None
        try:
            while True:
                if time.monotonic() > deadline:
                    proc.kill()
                    error = f"격리 실행 시간 초과 ({limits.wall_seconds}초)"
                    break
                line = proc.stdout.readline()
                if not line:
                    break
                emitted += len(line)
                if emitted > limits.max_stdout_bytes:
                    proc.kill()
                    truncated = True
                    error = "표준 출력 상한을 초과했습니다."
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 후보의 일반 print 는 무시한다
                kind = msg.get("t")
                if kind == "req":
                    requests_made += 1
                    reply = respond(str(msg.get("url", "")), dict(msg.get("params") or {}))
                    if reply.get("denied"):
                        denials.append(
                            Denial(
                                backend_id=self.backend_id,
                                destination=str(msg.get("url", ""))[:300],
                                binary="python",
                                reason=str(reply.get("reason", "broker 거절")),
                                occurred_at=utc_now(),
                            )
                        )
                    proc.stdin.write(json.dumps({"t": "res", **reply}, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                elif kind == "done":
                    records = list(msg.get("records") or [])
                    requests_made = int(msg.get("requests", requests_made))
                    break
                elif kind == "fatal":
                    error = str(msg.get("error", "후보 실행 실패"))
                    requests_made = int(msg.get("requests", requests_made))
                    # 후보가 직접 소켓을 시도해 network none 에 막힌 경우도
                    # 차단 기록으로 남겨야 한다 (AC-15).
                    denials.extend(
                        _denials_from_text(
                            f"{error}\n{msg.get('traceback', '')}", self.backend_id
                        )
                    )
                    break
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            t.join(timeout=2)

        stderr_text = "".join(stderr_buf)
        if error is None and records is None:
            error = f"후보가 결과를 반환하지 않았습니다. stderr={stderr_text.strip()[:300]!r}"
        if stderr_text.strip():
            denials.extend(_denials_from_text(stderr_text, self.backend_id))

        return SandboxOutcome(
            backend_id=self.backend_id,
            exit_code=proc.returncode,
            records=records,
            error=error,
            duration_ms=0,
            requests_made=requests_made,
            denials=denials,
            stdout_truncated=truncated,
        )


# network none 환경에서 나타나는 차단 시그니처.
# 네트워크 자체가 없으므로 이 중 무엇이 보이든 "외부 연결 시도가 막힌 것"이다.
#
# 두 단계로 나눈다. traceback 에는 에러 메시지뿐 아니라 `raise URLError(err)` 같은
# **소스 코드 줄**도 섞이므로, 예외 이름만 보고 판정하면 오탐이 난다.
_MESSAGE_HINTS = (
    "Network is unreachable",
    "Name or service not known",
    "Temporary failure in name resolution",
    "Connection refused",
    "No route to host",
    "Try again",  # EAI_AGAIN: alpine 의 DNS 실패
)
_EXCEPTION_HINTS = ("URLError", "gaierror", "ConnectionError", "socket.timeout")
_BLOCKED_ERRNOS = {"-2", "-3", "101", "111", "113"}

# 후보 코드에 박힌 목적지를 추출해 denial 에 남긴다 (AC-15: 목적지 필드).
_URL_RE = re.compile(r"https?://[^\s'\")]+")
_ERRNO_RE = re.compile(r"Errno (-?\d+)")


def _denials_from_text(text: str, backend_id: BackendId) -> list[Denial]:
    """후보가 직접 소켓을 시도해 실패한 흔적을 denial 로 승격한다.

    하나의 traceback 은 여러 줄에 같은 오류를 반복하므로
    (목적지, errno) 로 묶어 **시도 1회 = 기록 1건**이 되게 한다.
    """
    urls = _URL_RE.findall(text)
    destination = urls[0][:300] if urls else None

    groups: dict[tuple[str | None, str], str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        errno_match = _ERRNO_RE.search(stripped)
        errno = errno_match.group(1) if errno_match else None
        qualifies = any(h in stripped for h in _MESSAGE_HINTS) or (
            errno in _BLOCKED_ERRNOS and any(h in stripped for h in _EXCEPTION_HINTS)
        )
        if not qualifies:
            continue
        signature = errno or "unknown"
        key = (destination, signature)
        # 가장 구체적인(긴) 줄을 대표 사유로 남긴다.
        if key not in groups or len(stripped) > len(groups[key]):
            groups[key] = stripped

    return [
        Denial(
            backend_id=backend_id,
            destination=dest,
            binary="python",
            reason=f"network egress blocked (errno {sig}): {line[:200]}",
            occurred_at=utc_now(),
        )
        for (dest, sig), line in groups.items()
    ]
