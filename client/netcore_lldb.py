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
SERVER_INFO = {"name": "netcore-lldb", "version": "0.2.0"}

INSTRUCTIONS = """\
You are connected to an lldb + SOS session inside a remote Linux container or
machine, debugging a .NET core dump. Use the `lldb_command` tool to issue
commands; the session is stateful (`thread select N` carries to subsequent
calls). Playbooks below distill techniques from Tess Ferrandez's .NET
debugging labs (the canonical reference for managed dump analysis).

== ORIENTATION (always run these first) ==
  1. `eeversion`     — confirm runtime version. If "Failed to find runtime
                        module" → the target's .NET install doesn't match the
                        dump. Use `target_info` to diagnose; ask the user.
  2. `clrthreads`    — managed thread list. The rightmost "Exception" column
                        flags any thread with a pending exception (usually the
                        faulting thread for crashes). Note the DBG id.
  3. Decide the scenario from clrthreads + the user's complaint, then pick
     the matching playbook below.

== PLAYBOOK: unhandled exception / crash ==
  - `thread select <DBG-id-with-Exception>`
  - `pe -nested`             — exception type + message + inner exceptions
  - `clrstack -a`            — managed stack with locals/parameters
  - Read the source at the top frame's file:line and explain the failure path.

== PLAYBOOK: memory issue (high RSS / suspected leak) ==
  - `eeheap -gc`             — total managed memory + per-generation sizes.
                                Gen2 >> Gen0/Gen1 → premature aging.
                                LOH > 10% of heap → check LOH.
  - `dumpheap -stat`         — top types by aggregate size. The winner usually
                                IS the leak. Note its MethodTable address.
  - `dumpheap -min 0n85000`  — LOH-resident objects (>85 KB). Many similar-sized
                                strings here often means string concatenation
                                in a loop (use StringBuilder).
  - `dumpheap -type <T>`     — list instances of the suspect type. Sample a
                                few addresses.
  - `dumpobj <addr>`         — inspect one. Look at its field sizes — large
                                arrays/strings inside small objects are the
                                actual weight.
  - `gcroot <addr>`          — retention chain. Common roots:
                                · `Root:` ending in a `static` field → static
                                  collection retention. NOT a "leak" per se;
                                  it's by-design retention. Ask: should the
                                  collection be bounded / cleared?
                                · Chain through `EventHandler` / delegates →
                                  event-handler leak. A long-lived publisher
                                  is pinning subscribers via its invocation
                                  list. Unsubscribe (-=) on dispose.
                                · Chain through a thread-local / `[ThreadStatic]`
                                  → thread-pinned cache that survives the
                                  request lifetime.
  - `gchandles -perdomain`   — handle counts. Spikes in `Strong` or `Pinned`
                                handles often indicate interop or RCW leaks.

== PLAYBOOK: hang / deadlock ==
  - `clrthreads`             — many threads in similar states is the first
                                tell. Count threads in "Cooperative" GC mode
                                vs "Preemptive".
  - `syncblk`                — sync block table. The `MonitorHeld` column on
                                a row with N waiters means N threads are
                                blocked on that lock; the owning thread id is
                                in the row. (Tess's #1 hang diagnostic.)
  - `parallelstacks`         — merged thread stacks; surfaces clusters of
                                threads stuck at the same frame instantly.
  - For each contended lock: `thread select <owner-id>` then `clrstack -a`.
                              If the owner is in `Thread.SleepInternal`,
                              `WaitOne`, or blocking I/O while holding the
                              lock → critical section is too coarse.
  - Look for `Monitor.ReliableEnter` in waiters' stacks — that's the lock
    acquisition site. A `lock(this)` or `lock(typeof(T))` or `lock("literal")`
    on a shared object is the typical culprit.

== PLAYBOOK: high CPU ==
  - `threadpool`             — worker/IOCP usage. Tess's rule: "the
                                threadpool stops creating new threads at 80%
                                CPU." If you see 81–100% reported, suspect GC
                                pressure, not real work.
  - `clrthreads` then pick threads that look busy (Cooperative + not waiting).
                              In lldb: `thread list` shows native time per
                              thread if the dump was captured live.
  - For each suspect thread: `thread select N`, then `clrstack -a` and `bt`.
  - GC pressure check: look for `coreclr!SVR::GCHeap::GarbageCollectGeneration`
    or `gc_heap::allocate_large_object` in native stacks. If GC dominates,
    drop to the memory playbook (LOH allocations + Gen2 collection rate are
    the usual cause). Tess's rule: optimal Gen0:Gen1:Gen2 collection ratio is
    100:10:1; a 1:1:1 ratio means every collection is a full GC.

== PLAYBOOK: finalizer issue (large finalizer queue) ==
  - `finalizequeue`          — objects awaiting finalization. A long queue
                                means the finalizer thread is falling behind
                                or blocked.
  - `clrthreads -special`    — find the (Finalizer) thread.
  - `thread select <finalizer-DBG-id>` then `clrstack` and `bt`.
                              If the finalizer is in a critical section,
                              waiting on a lock, or blocked on I/O → the
                              objects it's trying to finalize are stuck and
                              memory grows. Tess's classic "unblock my
                              finalizer" pattern.

== PLAYBOOK: async / task issue ==
  - `dumpasync`              — async state machines on the heap; shows what
                                each is awaiting and continuation chain.
  - `dumpasync -waiting`     — only states still awaiting (not completed).
                                Useful for "where did my Task get stuck?"

== KNOWN SOS LIMITATIONS (.NET 10) ==
  · `gcroot` may segfault libmscordaccore.so on .NET 10 dumps. If you see
    "tool raised: target process exited" from a `gcroot` call, fall back to:
        - `dumpheap -type System.EventHandler` (count delegates)
        - `dumpheap -type System.Object[]` then `dumpobj` each (look for
          backing arrays of static collections)
        - Reason about retention from `dumpheap -stat` ratios instead.
    The session is unaffected — the next MCP call gets a fresh lldb.

== TELLS / HEURISTICS (Tess-named patterns) ==
  · MonitorHeld with many waiters → serialized critical section.
  · gcroot ending at a static field → "not a leak, retention by design."
  · Many large `System.String[]` or `System.Char[]` instances on LOH → string
    concatenation in a loop.
  · `EventHandler` / `MulticastDelegate` in a gcroot chain → event handler
    leak; the subscriber can't be GC'd because the publisher still references
    it through the invocation list.
  · Many `System.Threading.Tasks.Task` in `dumpheap -stat` with growing count
    over a real-time series → unawaited tasks accumulating; check for
    fire-and-forget patterns.
  · `RuntimeCallableWrapper` accumulation → COM interop leak.

== SYMBOLS ==
  Source-line resolution in `clrstack` needs PDBs adjacent to the DLLs on
  the target. If method names show but file:line doesn't, the PDBs aren't
  findable. Ask the user to copy their `publish/` output into the target.

== CITING FINDINGS ==
  When you reference a frame, include file:line if shown by `clrstack`,
  otherwise the IL offset. When you cite a heap object, include its
  MethodTable address (so the user can reproduce `dumpheap -mt <addr>`).
"""

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
        """Copy a local file to the target."""
        if self.kind == "docker":
            subprocess.check_call(
                ["docker", "cp", local_path, f"{self.address}:{remote_path}"],
                stdin=subprocess.DEVNULL,
            )
        elif self.kind == "ssh":
            subprocess.check_call(
                ["scp", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=accept-new",
                 local_path, f"{self.address}:{remote_path}"],
                stdin=subprocess.DEVNULL,
            )
        else:
            raise ValueError(f"unknown transport: {self.kind}")


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
        try:
            os.write(self.master, b"quit\n")
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            os.close(self.master)
        except OSError:
            pass


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
    """Return 'sudo -n ' if the target isn't already root, else ''.
    If sudo is needed but unavailable, return None and let the caller surface the error."""
    rc, out, _ = transport.run("id -u", timeout=5)
    if out.strip() == "0":
        return ""
    rc, _, _ = transport.run("sudo -n true 2>/dev/null", timeout=5)
    if rc == 0:
        return "sudo -n "
    return ""  # try anyway; will fail with a clear apt/dnf permission error


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

    rc, out, _ = transport.run(
        "test -f $HOME/.dotnet/sos/libsosplugin.so && echo ok || echo missing",
        timeout=5,
    )
    if "ok" in out:
        log("bootstrap: SOS plugin installed at ~/.dotnet/sos/libsosplugin.so")
    else:
        raise SystemExit("bootstrap: SOS plugin file is missing after install. See log above.")


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


def handle(session: LldbSession, target_info: dict, msg: dict) -> dict | None:
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
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
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "shutdown":
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

    shutdown_requested = {"flag": False}

    def request_shutdown(*_):
        shutdown_requested["flag"] = True

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    while not shutdown_requested["flag"]:
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
            log(f"handler error: {exc}\n{traceback.format_exc()}")
            resp = _error(msg.get("id"), -32603, f"internal error: {exc}")
        if resp is not None:
            write_message(resp)

    if shutdown_requested["flag"]:
        log("shutdown requested by signal")
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
