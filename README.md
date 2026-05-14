# netcore-lldb

Analyze .NET Linux memory and crash dumps from your IDE — without learning lldb or SOS.

You point this tool at a Linux container or remote server where the dump lives,
and ask Claude Code in plain English: *"Why did this crash?"* Claude drives
`lldb` + SOS on the target and explains what happened with file-and-line
citations into your source.

---

## Quick start — 5 minutes from dump to answer

> **Scenario:** your .NET app crashed in a Docker container. You have the
> dump file. You want Claude to explain what went wrong.

### Step 1 — Install the client (30 seconds, one time)

The "product" is a single Python script plus a thin shell wrapper. Drop them
on your `PATH`.

**Linux / macOS / WSL:**

```bash
# from the repo root
sudo cp client/netcore-lldb client/netcore_lldb.py /usr/local/bin/
sudo chmod +x /usr/local/bin/netcore-lldb /usr/local/bin/netcore_lldb.py
```

Verify:

```bash
netcore-lldb --help     # should print usage
```

**Windows:**

Copy `client\netcore-lldb.cmd` and `client\netcore_lldb.py` somewhere on `PATH`
(e.g. `%USERPROFILE%\bin\`). Make sure Python 3.10+ is installed.

Requirements on your laptop:
- Python **3.10+** (uses only the standard library — no `pip install`).
- `docker` on PATH if you'll connect via Docker; `ssh` + `scp` on PATH for SSH.
- Claude Code (or any MCP client that supports stdio servers).

### Step 2 — Locate your four inputs (1 minute)

Before configuring anything, write down four things. Then Step 3 is just
fill-in-the-blank.

#### 1. **The dump file** — where does it live?

| Where is the dump? | What you'll use |
|---|---|
| Inside a Docker container running on **this laptop** (`docker ps` shows it) | `--docker <container-name>` + `--dump </path/inside/container>` |
| On a Linux **server** you can `ssh` into | `--ssh user@host` + `--dump </path/on/server>` |
| On **your laptop**, want to copy it into a running container | `--docker <container>` + `--copy-dump --dump </local/path>` |
| On **your laptop**, want to copy it to an SSH server | `--ssh user@host` + `--copy-dump --dump </local/path>` |

If you haven't captured a dump yet, see *"How to capture a dump on crash"*
below.

#### 2. **The main executable** — where on the target is the app's native host?

For a normal `dotnet publish` of MyApp, this is the file called just `MyApp`
(no extension) next to `MyApp.dll`. Typical paths inside a container:

- `/app/MyApp` (Microsoft Docker images)
- `/opt/myapp/MyApp` (your own install)
- `/work/app/MyApp` (after `docker cp` of a publish dir)

You'll pass it as `--exec </path/inside/target>`. *Strictly optional* but
strongly recommended — it helps lldb populate its module list.

#### 3. **Symbols (PDB files)** — where are they?

PDBs are how Claude gets to say *"this crashed at Program.cs line 33"* instead
of just *"this crashed in Program.Main+0x21b"*.

| State | What happens |
|---|---|
| **PDBs next to your DLLs on the target** (the default `dotnet publish` layout) | ✅ Claude gets source file + line numbers |
| **PDBs missing on the target** | ⚠️ Claude still gets method names and IL offsets — useful, just less precise. No errors, no broken workflow. |
| **PDBs locally but not on target** | Copy them in: `docker cp ./publish/. my-container:/app/` (re-runs of `--bootstrap` won't touch them) |

You **don't need a CLI flag for symbols** — lldb/SOS auto-discovers them as
long as each `.pdb` sits next to its `.dll`.

#### 4. **Source code** — where on your laptop?

You don't tell the tool. You tell Claude Code: just open Claude Code in your
project's source directory. When SOS reports `Program.cs @ 33`, Claude Code
reads that file from your repo on your laptop. The source never leaves your
machine.

### Step 3 — Configure Claude Code (30 seconds)

Open the `.mcp.json` file in your project (create it if it doesn't exist) and
paste this. Replace the four `<...>` placeholders with the values from Step 2.

```jsonc
{
  "mcpServers": {
    "netcore-lldb": {
      "command": "netcore-lldb",
      "args": [
        "--docker",    "<your-container-name>",
        "--bootstrap",
        "--dump",      "<path-to-dump-inside-container>",
        "--exec",      "<path-to-your-app-binary-inside-container>"
      ]
    }
  }
}
```

For an SSH target, replace `--docker <container-name>` with
`--ssh user@server.example.com`. Everything else is identical.

`--bootstrap` is **safe to leave on every time**. On first run it installs
`lldb` + the SOS debugger extension on your target (1–2 min). On subsequent
runs it verifies in ~4 seconds and does nothing else.

**Filled-in example:**

```jsonc
{
  "mcpServers": {
    "netcore-lldb": {
      "command": "netcore-lldb",
      "args": [
        "--docker",    "checkout-service",
        "--bootstrap",
        "--dump",      "/var/dumps/crash.core",
        "--exec",      "/app/CheckoutService"
      ]
    }
  }
}
```

### Step 4 — Ask Claude (10 seconds)

Open Claude Code in the project where `.mcp.json` lives **and where your
source repo is**. Then just ask, in English:

> Analyze the .NET crash dump and tell me what went wrong.

That's it. You don't need to know lldb, SOS, or anything else.

Other prompts that work:

> Memory is growing in production. What's leaking in this dump?
>
> The app hangs intermittently. Does this dump show a deadlock?
>
> Why is CPU stuck at 100%? Look at the dump and tell me which thread is busy.

### Step 5 — What you'll see

Claude will (in about 10–20 seconds for a typical dump):

1. Verify it can reach the target (you may see *"connecting to docker container 'checkout-service'…"*).
2. Bootstrap lldb + SOS if this is the first run on a fresh target (logs to stderr; you see *"bootstrap: installing lldb + runtime deps…"*).
3. Orient itself: `eeversion`, `clrthreads` — silently, behind the scenes.
4. Follow the right playbook for your question (crash → `pe -nested` + `clrstack -a`; hang → `syncblk`; memory → `eeheap -gc` + `dumpheap -stat`; etc.).
5. Read your actual source files in the project Claude Code is open in.
6. Reply in chat with a plain-English explanation.

**Example reply for a real crash:**

> The crash is an unhandled `InvalidOperationException` thrown at
> `Services/OrderService.cs:142`:
>
> > *"mandatoryTag must not be null (customers.Count=3)"*
>
> Looking at `OrderService.GetEmailLength()`, you call
> `customer.Email.Length` but `customer.Email` is null when the order comes
> from a guest checkout (no email captured).
>
> Two of the three customers in the failing batch were guests. The third
> would have succeeded, but the exception aborts the whole batch.
>
> Fix: add a null check before line 142, or default `Email` to `string.Empty`
> on the `Customer` model.

**If symbols (PDBs) were missing**, the same reply still works — you just get
slightly less precise locations:

> The crash is an `InvalidOperationException` thrown in
> `OrderService.GetEmailLength` at IL offset `0x1a`. Based on the call site
> in `Main+0x21b` and the surrounding code I can read from your repo, the
> null check on `customer.Email` is missing — and `Email` is nullable in
> your model.

---

## How to capture a dump on crash

If your app isn't already configured to write a dump when it crashes, set
these environment variables on the .NET process **before** the crash:

```bash
export DOTNET_DbgEnableMiniDump=1
export DOTNET_DbgMiniDumpType=4               # 4 = Full (best for analysis)
export DOTNET_DbgMiniDumpName=/var/dumps/dump.%p
```

Reproduce the crash. The runtime writes a dump on unhandled exception, named
with the PID (e.g. `/var/dumps/dump.12345`). That path is what you pass as
`--dump` in Step 3.

For a Kubernetes-deployed app, add these to your `Deployment.spec.template.spec.containers[].env`.

---

## Flag reference

| Flag | Purpose | When to use |
|---|---|---|
| `--docker <CONTAINER>` | Connect via `docker exec` | When the dump is in a local container |
| `--ssh <[USER@]HOST>` | Connect via SSH | When the dump is on a remote server |
| `--dump <PATH>` | Path to the dump **inside the target** | Always required |
| `--exec <PATH>` | Path to the main executable **inside the target** | Strongly recommended — helps lldb populate modules |
| `--bootstrap` | Install lldb + SOS on the target if missing. Idempotent — safe to leave on. | First run on any new target |
| `--copy-dump` | Treat `--dump` as a path on **your laptop**; copy it into the target before debugging | When the dump isn't already on the target |
| `--target-dump-path <PATH>` | Where to put the copied dump (default `/tmp/netcore-lldb-dump-<pid>.core`) | With `--copy-dump`, if you want to control the destination |

---

## How it works

A small client tool (Python script, stdlib only) runs on your laptop as an
MCP server for Claude Code. Internally it spawns `lldb` on the target via
either `docker exec` or `ssh`, drives it for the duration of the session, and
exposes two tools to the LLM: `lldb_command` and `target_info`.

```
[Your laptop]                                  [Target — anywhere]
                                                ┌────────────────────────┐
Claude Code                                     │ lldb + SOS             │
   ↓ stdio MCP                                  │ + the dump file        │
[netcore-lldb client tool]                      │ + your app's binaries  │
   ↓ docker exec / ssh                          │   (with PDBs ideally)  │
   ─────────────────────────────────────────►   │                        │
                                                └────────────────────────┘
```

**What gets sent over the wire?** Only short lldb commands (`clrthreads`,
`pe`, `clrstack -a` etc.) and their text output. The dump file never leaves
the target unless you use `--copy-dump` to push a local dump *into* a target.
**Your source code never leaves your laptop** — Claude Code reads it locally.

**MCP tool surface:**

- `lldb_command` — run any single lldb / SOS command. The session is
  stateful, so `thread select N` carries to subsequent calls.
- `target_info` — describe the connected target (transport, lldb version,
  dump path, whether `.lldbinit` loads SOS).

That's the whole API the LLM sees. The MCP server's `initialize` response
includes a detailed playbook (distilled from
[Tess Ferrandez's .NET debugging labs](https://www.tessferrandez.com/postindex/))
so the LLM knows the right SOS command sequence for crashes, hangs, memory
issues, high CPU, finalizer problems, and async-stuck scenarios.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `error: target ... does not have lldb installed` | Target missing lldb and you didn't pass `--bootstrap` | Re-run with `--bootstrap` |
| `bootstrap: package install failed` | Target is non-root without passwordless sudo, or no internet | Install lldb manually (`apt install lldb`), or run the client from a privileged context, or use a target with internet |
| `bootstrap: ... dotnet not found on target` | Target lacks `dotnet` (rare — usually your .NET app's container has it) | Install the .NET runtime or SDK on the target before `--bootstrap` |
| `Failed to find runtime module (libcoreclr.so)` from SOS commands | Target's .NET install path doesn't match what the dump expects | Make sure the target IS the same environment where the crash happened, OR bind-mount the matching `/usr/lib/dotnet` into the target |
| `clrstack` shows method names but no file/line | PDBs aren't on the target next to the DLLs | Copy your `publish/` output into the target, e.g. `docker cp ./publish/. my-container:/app/` |
| `gcroot` returns "tool raised: target process exited" | Known .NET 10 DAC bug — `gcroot` segfaults `libmscordaccore.so` on some dumps | The session keeps working; tell Claude to use heap-stat-based reasoning (`dumpheap -stat`, `dumpheap -type X`) instead |

---

## Repository layout

```
.
├── README.md
├── .gitignore
├── client/                 # THE PRODUCT
│   ├── netcore_lldb.py     # MCP client (single file, stdlib only)
│   ├── netcore-lldb        # bash wrapper for PATH
│   └── netcore-lldb.cmd    # Windows wrapper
├── reference-image/        # optional sandbox image (only for testing this tool)
│   ├── Dockerfile
│   └── README.md
├── sample/                 # tiny crashing .NET 10 app for self-testing
│   ├── Crasher.csproj
│   └── Program.cs
└── tests/                  # comprehensive end-to-end test suite
    ├── run_all.py          # 57 MCP-protocol-level tests (docker + ssh transports)
    ├── fixtures/           # curated .NET fixture apps (memory leak, deadlock, event-handler leak)
    │   └── build-and-dump.sh
    ├── setup-sshd.sh       # one-time: enable sshd on WSL for SSH tests
    ├── build-image.sh      # one-time: docker build of the reference image
    └── install-docker.sh   # one-time: install Docker CE on Ubuntu WSL
```

## Building from source / development

If you're modifying the tool itself:

```bash
# build the reference image (a target you can develop against without
# touching production containers)
./tests/build-image.sh

# build the fixture dumps used by tests
./tests/fixtures/build-and-dump.sh

# run the full test suite (57 tests, docker + ssh transports)
python3 tests/run_all.py
```

The test suite covers: argument parsing, preflight, MCP protocol, every
shipped SOS command, stateful sessions, large output, the bash wrapper,
`--copy-dump`, `--bootstrap` (both fresh and idempotent), and three curated
fixture dumps that exercise the canonical memory-leak / deadlock /
event-handler-leak playbooks.
