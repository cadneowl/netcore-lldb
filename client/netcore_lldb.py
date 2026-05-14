#!/usr/bin/env python3
"""
netcore-lldb — a small MCP client tool for analyzing .NET Linux core dumps.

Runs LOCALLY on the developer's machine as a stdio MCP server (so Claude Code
or any MCP client can drive it). Internally, it spawns lldb on a TARGET via
one of two transports:

  --docker <container>        local Docker container (uses `docker exec -i`)
  --ssh    <[user@]host>      remote machine    (uses `ssh -T`)

The target is expected to have:
  - lldb (>= 18)
  - dotnet-debugger-extensions installed (~/.lldbinit auto-loads libsosplugin.so)
  - the dump file at the path passed via --dump

Both target prerequisites are also satisfied by our reference image, so the
quick-start path is `docker run -d --name netcore-lldb-target <image>` then
`docker cp` the dump in.

Nothing about the target is mutated by this tool unless --copy-dump is set
(in which case the local dump is copied into the target at --target-dump-path).
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "netcore-lldb", "version": "0.3.0"}

# Slim initialize instructions. Detail lives in resources/playbooks/* and
# heuristics/known-issues resources — the LLM pulls only what's relevant.
INSTRUCTIONS = """\
You are connected to an lldb + SOS session against a .NET Linux core dump.
Use `lldb_command` to drive lldb; the session is stateful (`thread select N`
carries to subsequent calls).

Quick start:
  1. Read `netcore-lldb://session` for transport/lldb/dump details.
  2. Pick a playbook resource matching the symptom and read it:
       Crash / unhandled exception:  netcore-lldb://playbook/crash
       Memory / leak / OOM:          netcore-lldb://playbook/memory
       Hang / deadlock:              netcore-lldb://playbook/hang
       High CPU:                     netcore-lldb://playbook/high-cpu
       Finalizer issue:              netcore-lldb://playbook/finalizer
       Async / Task stuck:           netcore-lldb://playbook/async
  3. Follow its sequence with `lldb_command`, then explain findings in chat
     with file:line citations (from `clrstack`) where possible.

The playbooks distill techniques from Tess Ferrandez's .NET debugging labs.
Two extra reference resources:
  netcore-lldb://heuristics    — Tess-named tells (the 80% rule, 100:10:1 GC
                                 ratio, MonitorHeld, etc.)
  netcore-lldb://known-issues  — workarounds for .NET 10 SOS bugs (notably
                                 gcroot crashing libmscordaccore.so)

Prompts (prompts/list) offer one-shot scenario kickoffs (e.g. /analyze-memory).
"""


# === Per-scenario playbooks, exposed as resources ============================

PLAYBOOKS = {
    "crash": """\
# Crash / unhandled exception playbook

1. `clrthreads` — the rightmost "Exception" column flags the thread that
   has a pending exception (usually the faulting thread).
2. `thread select <DBG-id-with-Exception>`
3. `pe -nested` — exception type, message, and inner exceptions.
4. `clrstack -a` — managed stack with locals and parameters.
5. Read the source at the top frame's file:line and explain the failure path.

Cite findings as `Type.Method [File.cs @ line]` if `clrstack` showed source
info; otherwise as `Type.Method+0xOFFSET` (IL offset).
""",
    "memory": """\
# Memory issue / leak / OOM playbook

1. `eeheap -gc` — total managed memory + per-generation sizes.
   - Gen2 >> Gen0/Gen1   → premature aging (objects surviving collections).
   - LOH > 10% of heap   → check LOH (next step).
2. `dumpheap -stat` — top types by aggregate size. The winner usually IS the
   leak. Note its MethodTable address (first column).
3. `dumpheap -min 0n85000` — LOH-resident objects (≥85 KB). Many similar-sized
   strings here often means string concatenation in a loop (use StringBuilder).
4. `dumpheap -type <T>` — list instances of the suspect type. Sample a few.
5. `dumpobj <addr>` — inspect one. Look at its field sizes — large arrays /
   strings inside small objects are where the weight actually is.
6. `gcroot <addr>` — retention chain. Common roots:
   - Ends in a `static` field   → static-collection retention. NOT really a
                                  "leak" — it's by-design retention. Ask the
                                  user: should the collection be bounded /
                                  cleared on some event?
   - Chain through `EventHandler` / `MulticastDelegate` → event-handler leak.
                                  A long-lived publisher is pinning subscribers
                                  via its invocation list. Fix with `-=` on
                                  dispose.
   - Chain through `[ThreadStatic]` / thread-local → thread-pinned cache that
                                  survives the request lifetime.
7. `gchandles -perdomain` — handle counts. Spikes in `Strong` or `Pinned`
   often indicate interop or RCW leaks.

CAVEAT: on .NET 10, `gcroot` may segfault libmscordaccore.so. See
`netcore-lldb://known-issues` for workarounds.
""",
    "hang": """\
# Hang / deadlock playbook

1. `clrthreads` — many threads in similar states is the first tell. Count
   threads in "Cooperative" GC mode vs "Preemptive".
2. `syncblk` — Tess's #1 hang diagnostic. Look at the `MonitorHeld` column:
   a row with N > 1 means N-1 threads are blocked on that lock; the owning
   OSID is in the row's "Owning Thread Info" column.
3. `parallelstacks` — merged thread stacks; surfaces clusters of threads
   stuck at the same frame instantly.
4. For each contended lock: `thread select <owner-DBG-id>` then `clrstack -a`.
   - Owner in `Thread.SleepInternal`, `WaitOne`, or blocking I/O while
     holding the lock → the critical section is too coarse.
5. Look for `System.Threading.Monitor.ReliableEnter` in the waiters'
   `clrstack` output — that's the lock acquisition site. `lock(this)`,
   `lock(typeof(T))`, and `lock("string literal")` on a shared object are
   the typical culprits.
""",
    "high-cpu": """\
# High CPU playbook

1. `threadpool` — worker/IOCP usage. Tess's "80% rule": the .NET threadpool
   stops creating new threads at 80% CPU. If you see 81–100% reported,
   suspect **GC pressure**, not real work.
2. `clrthreads` — pick threads that look busy (Cooperative + not waiting).
   In lldb itself: `thread list` shows per-thread CPU time when available.
3. For each suspect thread: `thread select <N>`, then `clrstack -a` and `bt`.
4. **GC pressure check** — look for these in native stacks (`bt`):
   - `coreclr!SVR::GCHeap::GarbageCollectGeneration` → in GC.
   - `gc_heap::allocate_large_object` → LOH allocation triggering GC.
   If GC dominates, switch to the memory playbook — LOH allocations + Gen2
   collection rate are the usual cause.

Tess's optimal Gen0:Gen1:Gen2 collection ratio is **100:10:1**. A **1:1:1**
ratio means every collection is a full GC — pathological.
""",
    "finalizer": """\
# Finalizer issue playbook

1. `finalizequeue` — objects awaiting finalization. A long queue means the
   finalizer thread is falling behind or blocked.
2. `clrthreads -special` — find the `(Finalizer)` thread (typically DBG id 8
   in .NET Core / .NET 5+).
3. `thread select <finalizer-DBG-id>` then `clrstack` and `bt`.
   - If the finalizer is in a critical section, waiting on a lock, or
     blocked on I/O → the object it's trying to finalize is stuck and
     memory grows. This is Tess's "unblock my finalizer" pattern.

Common fixes:
- Wrap finalizer body in `try/catch` so one bad finalizer doesn't kill the
  thread.
- Don't take locks in finalizers — they run on a single, serialized thread.
""",
    "async": """\
# Async / Task stuck playbook

1. `dumpasync` — async state machines on the heap; shows what each is
   awaiting and the continuation chain.
2. `dumpasync -waiting` — only state machines still awaiting (not completed).
   Useful for "where did my `Task` get stuck?"
3. For accumulating unawaited tasks: `dumpheap -stat` and look for high
   counts of `System.Threading.Tasks.Task` and `Task<...>`. Fire-and-forget
   patterns are the usual cause.
""",
}

HEURISTICS_TEXT = """\
# Tess-named patterns

- **MonitorHeld with many waiters** → serialized critical section. Drill
  into the owning thread's `clrstack` to find what's holding it.
- **gcroot ending at a static field** → "not a leak, retention by design."
  Ask whether the collection should be bounded or cleared.
- **Many large `System.String[]` or `System.Char[]` on LOH** → string
  concatenation in a loop. Switch to `StringBuilder`.
- **`EventHandler` / `MulticastDelegate` in a gcroot chain** → event-handler
  leak. The subscriber can't be GC'd because the publisher still references
  it through the delegate's invocation list. Fix with `-=` on dispose.
- **Many `System.Threading.Tasks.Task` instances growing over time** →
  unawaited / fire-and-forget tasks accumulating.
- **`RuntimeCallableWrapper` accumulation** → COM interop leak.
- **80% threadpool utilization** → threadpool stops creating threads at 80%;
  anything above suggests GC pressure, not real work.
- **Gen0:Gen1:Gen2 collection ratio = 1:1:1** → every collection is a full
  GC. Optimal is 100:10:1.
"""

KNOWN_ISSUES_TEXT = """\
# Known SOS limitations (.NET 10)

## `gcroot` segfaults libmscordaccore.so on some .NET 10 dumps

If `gcroot <addr>` returns `tool raised: target process exited`, lldb
crashed inside the DAC. Our client surfaces this as `isError: true`; the
session is unaffected — the next `lldb_command` call spawns a fresh lldb.

**Workarounds** (heap-statistics-based reasoning):
- `dumpheap -type System.EventHandler` — counts delegate instances. Many
  EventHandlers + matching subscriber count = event-handler leak.
- `dumpheap -type System.Object[]` then `dumpobj <addr>` on each — look for
  backing arrays of static collections.
- Reason about retention from `dumpheap -stat` type-vs-count ratios.
"""


# === MCP resources ===========================================================

RESOURCES = [
    *[
        {
            "uri": f"netcore-lldb://playbook/{name}",
            "name": f"Playbook: {name}",
            "description": text.splitlines()[0].lstrip("# ").strip(),
            "mimeType": "text/markdown",
        }
        for name, text in PLAYBOOKS.items()
    ],
    {
        "uri": "netcore-lldb://session",
        "name": "Session info",
        "description": "Transport, lldb version, dump path, and SOS-load status of the connected target.",
        "mimeType": "application/json",
    },
    {
        "uri": "netcore-lldb://modules",
        "name": "Loaded managed modules",
        "description": "Output of SOS `clrmodules -v` against the dump. Use to discover assemblies and check for missing PDBs.",
        "mimeType": "text/plain",
    },
    {
        "uri": "netcore-lldb://threads",
        "name": "Managed threads",
        "description": "Output of SOS `clrthreads` against the dump. The rightmost Exception column flags the faulting thread.",
        "mimeType": "text/plain",
    },
    {
        "uri": "netcore-lldb://heuristics",
        "name": "Tess-named diagnostic patterns",
        "description": "Quick-reference tells (MonitorHeld, gcroot-static, 80% threadpool rule, etc.)",
        "mimeType": "text/markdown",
    },
    {
        "uri": "netcore-lldb://known-issues",
        "name": "Known SOS limitations",
        "description": "Workarounds for upstream SOS bugs that affect analysis (e.g. .NET 10 gcroot crash).",
        "mimeType": "text/markdown",
    },
]


# === MCP prompts =============================================================

# Per-scenario one-shot kickoffs. Claude Code surfaces these as /-completions.
# Each prompt returns a single user-role message that primes the LLM with the
# scenario, optionally including the user's free-text description.

PROMPT_DEFS = {
    "analyze-crash":     ("crash",     "Diagnose an unhandled exception / crash captured in the dump."),
    "analyze-memory":    ("memory",    "Investigate a memory issue (high RSS, suspected leak, OOM)."),
    "analyze-hang":      ("hang",      "Investigate a hang or deadlock."),
    "analyze-high-cpu":  ("high-cpu",  "Investigate high CPU usage / GC pressure."),
    "analyze-finalizer": ("finalizer", "Investigate a finalizer-thread issue (long queue, blocked finalizer)."),
    "analyze-async":     ("async",     "Investigate stuck async Tasks / state machines."),
    "overview":          (None,        "Give a quick overview of what's in the dump (runtime, threads, top heap types) with no specific hypothesis."),
}

PROMPTS = [
    {
        "name": name,
        "description": desc,
        "arguments": [{
            "name": "user_description",
            "description": "What the user knows or has observed about the issue (optional).",
            "required": False,
        }],
    }
    for name, (_, desc) in PROMPT_DEFS.items()
]

TOOLS = [
    {
        "name": "lldb_command",
        "description": (
            "Run a single lldb or SOS command on the target and return its full text output. "
            "Session is stateful: `thread select N` affects subsequent `clrstack`, etc.\n\n"
            "Common SOS commands: clrthreads, clrstack, clrstack -a, clrstack -all, "
            "pe, pe -nested, dso, dumpheap -stat, dumpheap -type <T>, dumpobj <addr>, "
            "gcroot <addr>, eeheap -gc, analyzeoom, dumpasync, syncblk, threadpool, "
            "parallelstacks, clrmodules, eeversion. Native lldb works too: thread list, bt, "
            "image list, register read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Single command, e.g. 'clrthreads'."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "target_info",
        "description": (
            "Report info about the connected target: transport, hostname/container, lldb version, "
            ".lldbinit contents (SOS load), and the dump file path. Useful when SOS commands fail "
            "and you want to verify the target is set up correctly."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
PROMPT = b"(lldb) "


def log(msg: str) -> None:
    sys.stderr.write("[netcore-lldb] " + msg + "\n")
    sys.stderr.flush()


# ----- Transports -----------------------------------------------------------

@dataclass
class Transport:
    """A way to spawn a command on the target and to copy files into it."""
    kind: str            # "docker" or "ssh"
    address: str         # container name/id, or [user@]host
    description: str     # human-readable, for logs and target_info

    def cmd_for_remote(self, remote_cmd: str, allocate_pty: bool) -> list[str]:
        """Argv that runs `remote_cmd` on the target.

        `docker exec` takes each arg separately (we can use `sh -c <cmd>`).
        `ssh`, in contrast, JOINS every arg after the host with spaces into a
        single string that the remote login shell parses — so we have to send
        the whole command as one argument and let the remote shell run it."""
        if self.kind == "docker":
            args = ["docker", "exec", "-i"]
            if allocate_pty:
                args.append("-t")
            return args + [self.address, "sh", "-c", remote_cmd]
        if self.kind == "ssh":
            args = ["ssh",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "ConnectTimeout=10",
                    "-o", "ServerAliveInterval=30"]
            if allocate_pty:
                args += ["-tt"]
            else:
                args += ["-T"]
            # Single string after the host — the remote login shell parses it.
            return args + [self.address, remote_cmd]
        raise ValueError(f"unknown transport: {self.kind}")

    def run(self, remote_cmd: str, timeout: float = 30.0) -> tuple[int, str, str]:
        """Run a one-shot remote command, return (rc, stdout, stderr).
        Crucially, stdin is /dev/null: `docker exec -i` and `ssh -i` would
        otherwise consume from our parent stdin (the MCP message stream)."""
        argv = self.cmd_for_remote(remote_cmd, allocate_pty=False)
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            return 124, exc.stdout or "", (exc.stderr or "") + f"\n[timed out after {timeout}s]"
        except FileNotFoundError as exc:
            return 127, "", str(exc)

    def copy_to_target(self, local_path: str, remote_path: str) -> None:
        """Copy a local file to the target.

        Capture stdout/stderr explicitly — our own stdout is the MCP message
        stream to Claude Code, so even a 'Successfully copied...' line from
        `docker cp` would corrupt the JSON-RPC framing."""
        if self.kind == "docker":
            argv = ["docker", "cp", local_path, f"{self.address}:{remote_path}"]
        elif self.kind == "ssh":
            argv = ["scp",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=accept-new",
                    local_path, f"{self.address}:{remote_path}"]
        else:
            raise ValueError(f"unknown transport: {self.kind}")
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit(f"file copy timed out after 600s: {' '.join(argv[:3])}")
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                f"file copy failed (rc={exc.returncode}): {' '.join(argv[:3])}\n"
                f"stderr: {(exc.stderr or '').strip()}"
            )
        # Tools like docker cp can print "Successfully copied..." even on success;
        # log it to stderr so it doesn't end up in the MCP stream.
        if result.stdout.strip():
            log(f"copy: {result.stdout.strip()}")


def make_transport(args) -> Transport:
    if args.docker and args.ssh:
        raise SystemExit("error: pass either --docker or --ssh, not both")
    if args.docker:
        return Transport(kind="docker", address=args.docker,
                         description=f"docker container '{args.docker}'")
    if args.ssh:
        return Transport(kind="ssh", address=args.ssh,
                         description=f"ssh target '{args.ssh}'")
    raise SystemExit("error: --docker <container> or --ssh <[user@]host> is required")


# ----- Remote lldb session --------------------------------------------------

class LldbSession:
    """A long-running lldb on the target, driven via a pipe through the transport.

    Uses `script -q -c '<lldb cmd>' /dev/null` on the target to give lldb a
    pty (so the (lldb) prompt and command echoes work the same as interactive).
    This is more portable than relying on `docker exec -t` or `ssh -tt` to do
    the right thing with our local stdin (which is the MCP transport, not a tty).
    """

    def __init__(self, transport: Transport, dump_path: str, exec_path: str | None) -> None:
        self.transport = transport
        self.dump_path = dump_path
        self.exec_path = exec_path
        self._lock = threading.Lock()

        # Build the lldb invocation that runs ON THE TARGET.
        lldb_args = ["lldb", "--no-use-colors"]
        if exec_path:
            lldb_args.append(_sh_quote(exec_path))
        lldb_args += ["-c", _sh_quote(dump_path)]
        lldb_invocation = " ".join(lldb_args)
        # Wrap with `script` for a pty on the target side.
        remote_cmd = (
            f"if command -v script >/dev/null 2>&1; then "
            f"  script -q -c {_sh_quote(lldb_invocation)} /dev/null; "
            f"else "
            f"  exec {lldb_invocation}; "
            f"fi"
        )
        # For ssh, force a pty on the remote side (-tt) — lldb's prompt-detection
        # is unreliable through ssh -T even with the `script` wrapper. Docker exec
        # works fine either way; we always pass -i (interactive stdin) but not -t
        # because some Docker setups complain when stdin isn't a real tty.
        want_remote_pty = transport.kind == "ssh"
        argv = transport.cmd_for_remote(remote_cmd, allocate_pty=want_remote_pty)
        log(f"spawn: {' '.join(argv[:8])}{'...' if len(argv) > 8 else ''}")

        # Use a local pty so our subprocess thinks it has a terminal too —
        # avoids docker/ssh complaining about "input is not a terminal".
        master, slave = pty.openpty()
        self.master = master
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        self.proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            env=env,
            start_new_session=True,
        )
        os.close(slave)

        startup = self._read_until_prompt(timeout=60.0)
        log("lldb startup output:\n" + _indent(startup))

        # Quiet output to make prompt detection reliable.
        for setup in (
            "settings set use-color false",
            "settings set interpreter.echo-commands false",
            "settings set interpreter.echo-comment-commands false",
            "settings set stop-line-count-after 0",
            "settings set stop-line-count-before 0",
        ):
            self._run_unlocked(setup, timeout=10.0)

    def run(self, command: str, timeout: float = 120.0) -> str:
        with self._lock:
            return self._run_unlocked(command, timeout=timeout)

    def _run_unlocked(self, command: str, timeout: float) -> str:
        if "\n" in command:
            raise ValueError("multi-line commands are not supported; one command per call")
        os.write(self.master, command.encode("utf-8") + b"\n")
        raw = self._read_until_prompt(timeout=timeout)
        return _strip_echo(raw, command)

    def _read_until_prompt(self, timeout: float) -> str:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"lldb did not return prompt within {timeout}s. Partial output (tail):\n"
                    + buf.decode("utf-8", "replace")[-2000:]
                )
            r, _, _ = select.select([self.master], [], [], min(remaining, 1.0))
            if not r:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"target process exited with code {self.proc.returncode} before producing a prompt.\n"
                        f"Output so far:\n{buf.decode('utf-8', 'replace')}"
                    )
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"target process exited with code {self.proc.returncode}")
                continue
            buf.extend(chunk)
            cleaned = ANSI_RE.sub(b"", bytes(buf))
            if cleaned.endswith(PROMPT):
                idx = cleaned.rfind(PROMPT)
                # Two ptys in the chain (our local one + `script` on the target)
                # both apply ONLCR, so a single \n leaves the remote shell as
                # \r\n and arrives here as \r\r\n. Normalize by dropping all \r.
                text = cleaned[:idx].decode("utf-8", "replace").replace("\r", "")
                return text

    def close(self) -> None:
        """Tear down the lldb subprocess. Logs each cleanup step so a Ctrl-C
        path or a session-died path leaves a trail rather than silently
        dropping the remote lldb (which would keep running and holding the
        dump file open under `ssh` — `docker exec` cleans up naturally)."""
        try:
            os.write(self.master, b"quit\n")
        except OSError as exc:
            log(f"close: sending 'quit' to lldb failed: {exc}")
        try:
            rc = self.proc.wait(timeout=5)
            log(f"close: lldb exited cleanly with code {rc}")
        except subprocess.TimeoutExpired:
            log("close: lldb did not exit within 5s; sending SIGKILL")
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log("close: lldb still alive after SIGKILL — orphaned process likely")
                if self.transport.kind == "ssh":
                    log("close: NOTE: with --ssh, killing the local ssh client doesn't "
                        "always reap the remote lldb. If this becomes a habit, "
                        "ssh in and `pkill lldb`.")
        try:
            os.close(self.master)
        except OSError as exc:
            log(f"close: pty master close failed (likely already closed): {exc}")


# ----- Bootstrap & preflight ------------------------------------------------

# Per-distro package install commands. Run as: `<sudo-prefix> <pkg-cmd>`.
# The lldb package on each distro is whatever version the distro ships; lldb 18+
# all work with the dotnet-debugger-extensions SOS plugin we install on top.
PACKAGE_CMDS = {
    "ubuntu": (
        "apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "
        "lldb python3-lldb ca-certificates curl libunwind8 liblttng-ust1 binutils"
    ),
    "debian": (
        "apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "
        "lldb python3-lldb ca-certificates curl libunwind8 liblttng-ust1 binutils"
    ),
    "fedora": "dnf install -y lldb python3-lldb libunwind binutils ca-certificates",
    "rhel":   "dnf install -y lldb python3-lldb libunwind binutils ca-certificates",
    "centos": "dnf install -y lldb python3-lldb libunwind binutils ca-certificates",
    "alpine": "apk add --no-cache lldb llvm libunwind binutils ca-certificates",
}


def detect_distro(transport: Transport) -> str:
    """Best-effort distro ID from /etc/os-release. Returns the lowercase ID or ''."""
    rc, out, _ = transport.run("cat /etc/os-release 2>/dev/null || true", timeout=10)
    if rc != 0:
        return ""
    for line in out.splitlines():
        if line.startswith("ID="):
            return line.split("=", 1)[1].strip().strip('"').lower()
    return ""


def needs_sudo(transport: Transport) -> str:
    """Return 'sudo -n ' if the target isn't already root and passwordless
    sudo works; '' if the target is root; '' (with a clear log warning) if
    sudo is needed but unavailable — the subsequent apt/dnf call will then
    surface a permission error to the caller."""
    rc, out, _ = transport.run("id -u", timeout=5)
    if rc == 0 and out.strip() == "0":
        return ""
    rc, _, _ = transport.run("sudo -n true 2>/dev/null", timeout=5)
    if rc == 0:
        return "sudo -n "
    log("bootstrap: target is non-root and passwordless sudo is unavailable; "
        "package install will likely fail with a permission error.")
    return ""


def bootstrap_target(transport: Transport) -> None:
    """Install lldb + the SOS debugger extension on the target via the transport.
    Idempotent: skips installs that are already present. Best-effort across
    apt-based, dnf-based, and apk-based distros.

    The customer's container is mutated (apt-get install, dotnet tool install,
    ~/.lldbinit edit). The customer must explicitly pass --bootstrap to opt in."""
    log("bootstrap: detecting target distro...")
    distro = detect_distro(transport)
    if not distro:
        raise SystemExit("bootstrap: could not detect target distro from /etc/os-release")
    log(f"bootstrap: detected distro={distro!r}")

    pkg_cmd = PACKAGE_CMDS.get(distro)
    if not pkg_cmd:
        # Try the 'ID_LIKE' fallback (e.g. some derivative distros).
        rc, out, _ = transport.run("cat /etc/os-release 2>/dev/null", timeout=5)
        for line in out.splitlines():
            if line.startswith("ID_LIKE="):
                for like in line.split("=", 1)[1].strip().strip('"').lower().split():
                    if like in PACKAGE_CMDS:
                        pkg_cmd = PACKAGE_CMDS[like]
                        log(f"bootstrap: using {like!r} package recipe (via ID_LIKE)")
                        break
        if not pkg_cmd:
            raise SystemExit(
                f"bootstrap: no install recipe for distro {distro!r}. "
                f"Supported: {', '.join(sorted(PACKAGE_CMDS))}"
            )

    # Install lldb if missing.
    rc, out, _ = transport.run("command -v lldb >/dev/null && echo ok || echo missing", timeout=5)
    if "missing" in out:
        sudo = needs_sudo(transport)
        log(f"bootstrap: installing lldb + runtime deps (this can take a minute)...")
        rc, out, err = transport.run(f"{sudo}sh -c {_sh_quote(pkg_cmd)} 2>&1", timeout=600)
        if rc != 0:
            raise SystemExit(
                f"bootstrap: package install failed (rc={rc}). Output:\n{out[-2000:]}\n"
                f"Hint: if the target isn't root and you don't have passwordless sudo, "
                f"install lldb manually first or run the client from a privileged context."
            )
        # Verify
        rc, out, _ = transport.run("lldb --version 2>&1", timeout=10)
        if "lldb version" not in out:
            raise SystemExit(f"bootstrap: lldb still missing after install:\n{out}")
        log(f"bootstrap: lldb installed — {out.strip().splitlines()[0]}")
    else:
        rc, out, _ = transport.run("lldb --version 2>&1", timeout=10)
        log(f"bootstrap: lldb already present — {out.strip().splitlines()[0]}")

    # Install dotnet-debugger-extensions (SOS) if missing.
    # We need `dotnet` on the target — usually true because it's running a .NET app.
    rc, out, _ = transport.run("command -v dotnet >/dev/null && echo ok || echo missing", timeout=5)
    if "missing" in out:
        log("bootstrap: WARNING: `dotnet` not found on target; cannot install the SOS extension. "
            "If your dump is from a self-contained .NET app, install the .NET SDK or runtime "
            "on the target first.")
        return

    rc, out, _ = transport.run(
        'test -f $HOME/.dotnet/sos/libsosplugin.so && '
        'grep -q libsosplugin $HOME/.lldbinit 2>/dev/null && echo ok || echo missing',
        timeout=5,
    )
    if "ok" in out:
        log("bootstrap: SOS plugin already installed and configured in ~/.lldbinit")
        return

    log("bootstrap: installing dotnet-debugger-extensions...")
    # `dotnet tool install` errors if already installed — fall back to `update`.
    rc, out, _ = transport.run(
        "dotnet tool install --global dotnet-debugger-extensions 2>&1 || "
        "dotnet tool update  --global dotnet-debugger-extensions 2>&1",
        timeout=180,
    )
    if "successfully" not in out.lower() and "already installed" not in out.lower():
        log(f"bootstrap: WARNING: tool install reported: {out.strip().splitlines()[-1] if out else '(empty)'}")

    log("bootstrap: running `dotnet-debugger-extensions install` to wire up ~/.lldbinit...")
    rc, out, _ = transport.run(
        '$HOME/.dotnet/tools/dotnet-debugger-extensions install --accept-license-agreement 2>&1',
        timeout=120,
    )
    if "SOS install succeeded" not in out and "succeeded" not in out.lower():
        log(f"bootstrap: WARNING: SOS install reported:\n{out[-800:]}")

    # Verify by direct rc check on `test -f` (not by substring-matching stdout —
    # that pattern would misdiagnose a transport timeout as "SOS missing").
    rc, _, err = transport.run("test -f $HOME/.dotnet/sos/libsosplugin.so", timeout=5)
    if rc == 0:
        log("bootstrap: SOS plugin installed at ~/.dotnet/sos/libsosplugin.so")
    elif rc == 1:
        raise SystemExit("bootstrap: SOS plugin file is missing after install. See log above.")
    else:
        raise SystemExit(
            f"bootstrap: could not verify SOS plugin (rc={rc}, transport error?). "
            f"stderr: {err.strip()}"
        )


def preflight(transport: Transport, dump_path: str, bootstrap: bool) -> dict:
    """Verify the target is reachable, has lldb, and the dump exists. If
    bootstrap is requested, run the install step before the lldb check. Return
    a dict of info we'll surface via the `target_info` tool."""
    info: dict = {"transport": transport.description}

    rc, out, _ = transport.run("uname -a", timeout=10)
    if rc != 0:
        raise SystemExit(f"could not reach target ({transport.description}). "
                         f"Check your --docker/--ssh argument is correct.")
    info["uname"] = out.strip()

    if bootstrap:
        bootstrap_target(transport)

    rc, out, err = transport.run("lldb --version 2>&1 || true", timeout=10)
    info["lldb_version"] = out.strip() or err.strip() or "(not found)"
    if "lldb version" not in info["lldb_version"]:
        raise SystemExit(
            f"target {transport.description} does not have `lldb` installed.\n"
            f"Run with --bootstrap to install it automatically, or install it "
            f"manually (e.g. `apt install lldb` on Debian/Ubuntu)."
        )

    rc, out, _ = transport.run(f"test -f {_sh_quote(dump_path)} && echo ok || echo missing", timeout=10)
    if "ok" not in out:
        raise SystemExit(
            f"dump file does not exist on target at: {dump_path}\n"
            f"Either copy it in first or pass --copy-dump to have me copy a local file."
        )
    info["dump_path"] = dump_path

    rc, out, _ = transport.run("cat ~/.lldbinit 2>/dev/null || true", timeout=5)
    info["lldbinit_loads_sos"] = "libsosplugin" in out
    if not info["lldbinit_loads_sos"]:
        log("WARNING: target ~/.lldbinit does not appear to load SOS. SOS commands "
            "(clrthreads, clrstack, pe, ...) will not be available. Run with "
            "--bootstrap to install, or run `dotnet-debugger-extensions install` "
            "on the target manually.")
    return info


# ----- helpers --------------------------------------------------------------

def _sh_quote(s: str) -> str:
    """POSIX-shell-quote a string."""
    if not s or any(c in s for c in " '\"\\$`*?[]<>|&;()#\n\t"):
        return "'" + s.replace("'", "'\"'\"'") + "'"
    return s


def _indent(s: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in s.splitlines())


def _strip_echo(raw: str, command: str) -> str:
    """lldb echoes the command back — and with two ptys in the chain it gets
    echoed TWICE. Find the LAST line within the first few that matches the
    command, drop everything up to and including it, and return the rest."""
    lines = raw.splitlines()
    last_echo_idx = -1
    # Only look at the first few lines — real output won't repeat the literal
    # command verbatim that early.
    for i, line in enumerate(lines[:6]):
        if line.rstrip().endswith(command):
            last_echo_idx = i
    if last_echo_idx >= 0:
        return "\n".join(lines[last_echo_idx + 1:]).rstrip() + "\n"
    return raw.rstrip() + "\n"


# ----- MCP plumbing ---------------------------------------------------------

def write_message(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def read_message() -> dict | None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return None  # EOF
        line = line.strip()
        if line:
            return json.loads(line)


def _error(msg_id, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_result(msg_id, text: str, is_error: bool) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {
            "content": [{"type": "text", "text": text or "(no output)"}],
            "isError": is_error,
        },
    }


def _dispatch_tool(session: LldbSession, target_info: dict, name: str, args: dict) -> tuple[str, bool]:
    if name == "lldb_command":
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return ("missing or empty 'command' argument", True)
        return (session.run(cmd), False)
    if name == "target_info":
        lines = [f"  {k}: {v}" for k, v in target_info.items()]
        return ("target info:\n" + "\n".join(lines), False)
    return (f"unknown tool: {name!r}", True)


def _resource_contents(uri: str, session: LldbSession, target_info: dict) -> list[dict]:
    """Materialize the contents for a given resource URI. Raises ValueError on
    unknown URIs so the caller can map to JSON-RPC -32602 (invalid params)."""
    if uri.startswith("netcore-lldb://playbook/"):
        scenario = uri.split("/", 3)[-1]
        text = PLAYBOOKS.get(scenario)
        if text is None:
            raise ValueError(f"unknown playbook: {scenario}. "
                             f"Available: {', '.join(sorted(PLAYBOOKS))}.")
        return [{"uri": uri, "mimeType": "text/markdown", "text": text}]
    if uri == "netcore-lldb://session":
        return [{"uri": uri, "mimeType": "application/json",
                 "text": json.dumps(target_info, indent=2, default=str)}]
    if uri == "netcore-lldb://modules":
        return [{"uri": uri, "mimeType": "text/plain", "text": session.run("clrmodules -v")}]
    if uri == "netcore-lldb://threads":
        return [{"uri": uri, "mimeType": "text/plain", "text": session.run("clrthreads")}]
    if uri == "netcore-lldb://heuristics":
        return [{"uri": uri, "mimeType": "text/markdown", "text": HEURISTICS_TEXT}]
    if uri == "netcore-lldb://known-issues":
        return [{"uri": uri, "mimeType": "text/markdown", "text": KNOWN_ISSUES_TEXT}]
    raise ValueError(f"unknown resource: {uri!r}")


def _resolve_prompt(name: str, args: dict) -> dict:
    """Materialize a prompt by name into MCP {description, messages}. Raises
    ValueError on unknown prompts."""
    entry = PROMPT_DEFS.get(name)
    if entry is None:
        raise ValueError(f"unknown prompt: {name!r}. "
                         f"Available: {', '.join(sorted(PROMPT_DEFS))}.")
    scenario, description = entry
    user_description = (args.get("user_description") or "").strip()
    user_note = f"\n\nThe user says: {user_description}" if user_description else ""
    if scenario:
        playbook_uri = f"netcore-lldb://playbook/{scenario}"
        message = (
            f"I'm analyzing a .NET Linux core dump. Help me with the following: "
            f"{description.lower().rstrip('.')}.{user_note}\n\n"
            f"Step 1: read the playbook at `{playbook_uri}` via `resources/read`.\n"
            f"Step 2: also read `netcore-lldb://session` so you know which target "
            f"and dump you're connected to.\n"
            f"Step 3: follow the playbook using the `lldb_command` tool. Cite "
            f"findings with file:line where `clrstack` shows them; otherwise "
            f"with IL offset. Explain in plain English what's happening and what "
            f"to fix."
        )
    else:
        # Generic overview (no specific playbook).
        message = (
            f"I'm analyzing a .NET Linux core dump. Give me a quick overview.{user_note}\n\n"
            f"Read `netcore-lldb://session` and `netcore-lldb://threads`, then run "
            f"`eeversion`, `clrthreads`, `clrmodules` via `lldb_command`, and "
            f"summarize: runtime version, top heap types from `dumpheap -stat`, "
            f"any thread with a pending exception, and any obvious smell (LOH "
            f"dominated by strings, MonitorHeld > 1, finalizer queue long, etc.)."
        )
    return {
        "description": description,
        "messages": [
            {"role": "user", "content": {"type": "text", "text": message}}
        ],
    }


_CAPABILITIES = {
    "tools":     {"listChanged": True},
    "resources": {"listChanged": True},
    "prompts":   {"listChanged": True},
}

# Set by the `shutdown` MCP method and by SIGTERM/SIGINT; checked by main loop.
_SHUTDOWN_FLAG: dict = {"flag": False}


def handle(session: LldbSession, target_info: dict, msg: dict) -> dict | None:
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": _CAPABILITIES,
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            text, is_error = _dispatch_tool(session, target_info, params.get("name"), params.get("arguments") or {})
        except Exception as exc:
            log(f"tool error: {exc}\n{traceback.format_exc()}")
            return _tool_result(msg_id, f"tool raised: {exc}", is_error=True)
        return _tool_result(msg_id, text, is_error)
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": RESOURCES}}
    if method == "resources/read":
        uri = (msg.get("params") or {}).get("uri", "")
        try:
            contents = _resource_contents(uri, session, target_info)
        except ValueError as exc:
            return _error(msg_id, -32602, str(exc))
        except Exception as exc:
            log(f"resource read error for {uri!r}: {exc}\n{traceback.format_exc()}")
            return _error(msg_id, -32603, f"resource read failed: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"contents": contents}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": PROMPTS}}
    if method == "prompts/get":
        params = msg.get("params") or {}
        try:
            result = _resolve_prompt(params.get("name", ""), params.get("arguments") or {})
        except ValueError as exc:
            return _error(msg_id, -32602, str(exc))
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "shutdown":
        # MCP spec: after `shutdown`, the server should stop accepting new
        # requests; the client follows up with `exit`. Set the loop-exit flag
        # via the same signal-shutdown path so the main loop tears down cleanly.
        _SHUTDOWN_FLAG["flag"] = True
        return {"jsonrpc": "2.0", "id": msg_id, "result": None}
    if msg_id is not None:
        return _error(msg_id, -32601, f"method not found: {method}")
    return None


# ----- main -----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="netcore-lldb",
        description="MCP server that drives lldb on a remote target (docker or ssh) for .NET dump analysis.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--docker", metavar="CONTAINER", help="Docker container name or ID to docker-exec into.")
    group.add_argument("--ssh", metavar="[USER@]HOST", help="SSH target.")
    parser.add_argument("--dump", required=True,
                        help="Path to the .NET core dump file. By default this is interpreted as "
                             "a path INSIDE the target. Add --copy-dump to push a LOCAL file instead.")
    parser.add_argument("--copy-dump", action="store_true",
                        help="Treat --dump as a path on the local machine; copy it to the target "
                             "before opening lldb.")
    parser.add_argument("--target-dump-path", metavar="PATH",
                        help="When --copy-dump is set, the destination path on the target. "
                             "Defaults to /tmp/netcore-lldb-dump-<pid>.core")
    parser.add_argument("--exec", dest="exec_path", metavar="PATH",
                        help="Path (inside the target) to the main executable for the dump. "
                             "Helps lldb populate its module list. Optional.")
    parser.add_argument("--bootstrap", action="store_true",
                        help="If the target is missing lldb or the SOS debugger extension, "
                             "install them automatically before debugging. Mutates the "
                             "target (apt-get/dnf install, dotnet tool install, ~/.lldbinit edit). "
                             "Idempotent. Supports apt-based and dnf-based distros.")
    args = parser.parse_args()

    transport = make_transport(args)

    if args.copy_dump:
        if not os.path.isfile(args.dump):
            raise SystemExit(f"--dump points at a missing local file: {args.dump}")
        target_path = args.target_dump_path or f"/tmp/netcore-lldb-dump-{os.getpid()}.core"
        log(f"copying local dump {args.dump} -> target {target_path}")
        # ensure parent dir exists on target
        transport.run(f"mkdir -p {_sh_quote(os.path.dirname(target_path) or '/')}", timeout=10)
        transport.copy_to_target(args.dump, target_path)
        dump_on_target = target_path
    else:
        dump_on_target = args.dump

    target_info = preflight(transport, dump_on_target, bootstrap=args.bootstrap)
    session = LldbSession(transport, dump_on_target, exec_path=args.exec_path)
    log("MCP server ready (stdio)")

    def request_shutdown(*_):
        _SHUTDOWN_FLAG["flag"] = True

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    while not _SHUTDOWN_FLAG["flag"]:
        try:
            msg = read_message()
        except json.JSONDecodeError as exc:
            log(f"ignoring malformed message: {exc}")
            continue
        if msg is None:
            log("stdin closed, exiting")
            break
        try:
            resp = handle(session, target_info, msg)
        except Exception as exc:
            log(f"handler error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            resp = _error(msg.get("id"), -32603,
                          f"internal error ({type(exc).__name__}): {exc}")
        if resp is not None:
            write_message(resp)

    if shutdown_requested["flag"]:
        log("shutdown requested by signal")
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
