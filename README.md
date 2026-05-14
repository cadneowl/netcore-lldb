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

### Step 2 — Locate your three inputs (1 minute)

The headline case is: **your .NET app crashed in a container, and the dump
is right there in the same container**. In that case you only need to tell
us three things.

#### 1. **The dump file** — where does it live on the target?

| Where is the dump? | What you'll use |
|---|---|
| Inside a Docker container running on **this laptop** | `--docker <container-name>` + `--dump </path/inside/container>` |
| On a Linux **server** you can `ssh` into | `--ssh user@host` + `--dump </path/on/server>` |
| On **your laptop**, you'll copy it into a separate analysis target | See *"Analyzing a dump captured elsewhere"* below |

If you haven't captured a dump yet, see *"How to capture a dump on crash"*
below.

> **You do NOT need to tell us where the executable is.** When you point at
> the container/server where your app runs, the executable is already at the
> path the dump expects — lldb auto-loads it. The `--exec` flag exists for
> the unusual case where you're analyzing the dump on a *different* machine;
> see the flag reference.

#### 2. **Symbols (PDB files)** — where are they?

PDBs are how Claude gets to say *"this crashed at Program.cs line 33"*
instead of just *"this crashed in Program.Main+0x21b"*.

| State | What happens |
|---|---|
| **PDBs next to your DLLs on the target** (the default `dotnet publish` layout) | ✅ Claude gets source file + line numbers |
| **PDBs missing on the target** | ⚠️ Claude still gets method names and IL offsets — useful, just less precise. No errors, no broken workflow. |
| **PDBs locally but not on target** | Copy them in: `docker cp ./publish/. my-container:/app/` |

You **don't need a CLI flag for symbols** — lldb/SOS auto-discovers them as
long as each `.pdb` sits next to its `.dll`.

#### 3. **Source code** — where on your laptop?

You don't tell the tool. You tell Claude Code: just open Claude Code in your
project's source directory. When SOS reports `Program.cs @ 33`, Claude Code
reads that file from your repo on your laptop. The source never leaves your
machine.

### Step 3 — Configure Claude Code (30 seconds)

Open the `.mcp.json` file in your project (create it if it doesn't exist) and
paste this. Replace the two `<...>` placeholders with the values from Step 2.

```jsonc
{
  "mcpServers": {
    "netcore-lldb": {
      "command": "netcore-lldb",
      "args": [
        "--docker",    "<your-container-name>",
        "--bootstrap",
        "--dump",      "<path-to-dump-inside-container>"
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
        "--dump",      "/var/dumps/crash.core"
      ]
    }
  }
}
```

### Step 4 — Ask Claude (10 seconds)

Open Claude Code in the project where `.mcp.json` lives **and where your
source repo is**. You have two equally good ways to start:

**Option A — type a question in English:**

> Analyze the .NET crash dump and tell me what went wrong.

Other questions that work:

> Memory is growing in production. What's leaking in this dump?
>
> The app hangs intermittently. Does this dump show a deadlock?
>
> Why is CPU stuck at 100%? Look at the dump and tell me which thread is busy.

**Option B — use a slash-prompt for a one-click kickoff:**

In Claude Code, type `/` and pick one of:

| Prompt | When to use |
|---|---|
| `/analyze-crash` | Unhandled exception / crash dump |
| `/analyze-memory` | Suspected leak, OOM, RSS growing |
| `/analyze-hang` | App stops responding / deadlock |
| `/analyze-high-cpu` | CPU pegged, GC pressure suspected |
| `/analyze-finalizer` | Finalizer queue long / blocked finalizer |
| `/analyze-async` | Stuck `Task` / async state machine |
| `/overview` | "Just show me what's in this dump" |

These come from the MCP server's `prompts/list` — Claude Code surfaces them
as autocompletions. Each one primes Claude with the matching playbook so
you don't have to write the prompt yourself. Each also accepts a free-text
`user_description` if you want to add context.

Whichever option you pick, the rest is the same.

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

## Optional: try the tool against a reference container

Don't have a Docker container with your .NET app running, or just want to
kick the tires? Pull the prebuilt reference image and use it as a sandbox:

```bash
docker pull ghcr.io/cadneowl/netcore-lldb:latest
docker run -d --name dump-analyzer --entrypoint sleep \
    ghcr.io/cadneowl/netcore-lldb:latest infinity
# copy your dump in
docker cp /path/to/your/dump.core dump-analyzer:/work/dump.core
docker cp /path/to/your/publish/. dump-analyzer:/work/app/
```

Then in `.mcp.json` point `--docker dump-analyzer`. See `reference-image/README.md`
for what's inside.

## Analyzing a dump captured elsewhere

When the crash happened in container/host A but you want to analyze it on
container/host B (e.g., you can't add tools to your prod container), you
need to bring three things over to B, not just the dump:

```bash
# 1. The dump file
docker cp prod-container:/var/dumps/crash.core ./crash.core

# 2. Your app's published binaries (they were at /app on the source — lldb
#    on B will look at the same path the dump recorded)
docker cp prod-container:/app ./app

# 3. Push them into B
docker cp ./crash.core analysis-container:/work/dump.core
docker cp ./app/.       analysis-container:/work/app/
```

Then point the client at B with `--exec` so lldb knows where to find the
(now-relocated) main executable:

```jsonc
"args": [
  "--docker",    "analysis-container",
  "--bootstrap",
  "--dump",      "/work/dump.core",
  "--exec",      "/work/app/CheckoutService"
]
```

This is the only scenario where `--exec` is actually required.

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
| `--docker <CONTAINER>` | Connect via `docker exec` | Dump is in a local Docker container |
| `--ssh <[USER@]HOST>` | Connect via SSH | Dump is on a remote server |
| `--dump <PATH>` | Path to the dump **inside the target** | Always required |
| `--bootstrap` | Install lldb + SOS on the target if missing. Idempotent. | First run on any new target — safe to leave on always |
| `--copy-dump` | Treat `--dump` as a path on **your laptop**; copy it into the target before debugging | When the dump isn't already on the target |
| `--target-dump-path <PATH>` | Destination on the target when `--copy-dump` is set (default `/tmp/netcore-lldb-dump-<pid>.core`) | Optional companion to `--copy-dump` |
| `--exec <PATH>` | Path to the main executable **inside the target** | **Only needed when the target is *not* the original crash environment** — e.g. you copied the dump to a separate analysis container. In the natural flow (point at your app's container), lldb auto-loads the executable from the dump's recorded path. |

---

## How it works

A small client tool (Python script, stdlib only) runs on your laptop as an
MCP server for Claude Code. Internally it spawns `lldb` on the target via
either `docker exec` or `ssh`, drives it for the duration of the session, and
exposes two tools to the LLM: `lldb_command` and `target_info`.

### Why not use LLDB's built-in MCP server?

LLDB has [native MCP support](https://lldb.llvm.org/use/mcp.html) starting
in version **21** (`protocol-server start MCP listen://localhost:59999`). We
deliberately don't use it (yet) for two reasons:

1. **Compatibility.** Every default `apt install lldb` on Debian/Ubuntu LTS
   today gives you lldb 18–20, neither of which has `protocol-server`. To
   use the built-in server we'd require customers to add `apt.llvm.org` and
   install `lldb-21`+. The subprocess approach works against any lldb ≥ 18.
2. **Transport.** LLDB's native MCP is a TCP listener (`localhost:59999`).
   Claude Code speaks stdio MCP. Bridging from stdio to a TCP socket *inside
   a remote docker container* over `docker exec` or `ssh` is doable but adds
   moving parts. The subprocess approach hands stdio MCP to Claude Code
   directly.

When lldb 21+ becomes the default on common distros, we'll likely add an
alternative `--use-native-mcp` mode that does exactly that bridge for users
who prefer it.

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

### MCP surface (what Claude sees)

The server advertises three MCP capabilities — `tools`, `resources`, and
`prompts` (all with `listChanged: true`).

**Tools** (2) — actions Claude can take:

| Tool | Purpose |
|---|---|
| `lldb_command` | Run any single lldb / SOS command. The session is stateful, so `thread select N` carries to subsequent calls. |
| `target_info` | Describe the connected target (transport, lldb version, dump path, whether `.lldbinit` loads SOS). |

**Resources** (11) — state and playbooks Claude can read on demand:

| URI | What it is |
|---|---|
| `netcore-lldb://playbook/crash` | Crash / unhandled-exception playbook |
| `netcore-lldb://playbook/memory` | Memory / leak / OOM playbook |
| `netcore-lldb://playbook/hang` | Hang / deadlock playbook (Tess's `syncblk` approach) |
| `netcore-lldb://playbook/high-cpu` | High CPU / GC pressure playbook (the 80% rule, 100:10:1 ratio) |
| `netcore-lldb://playbook/finalizer` | Finalizer-thread-stuck playbook |
| `netcore-lldb://playbook/async` | Stuck async Task / state machine playbook |
| `netcore-lldb://session` | Live: transport, lldb version, dump path (JSON) |
| `netcore-lldb://modules` | Live: lazy `clrmodules -v` against the dump |
| `netcore-lldb://threads` | Live: lazy `clrthreads` against the dump |
| `netcore-lldb://heuristics` | Tess-named diagnostic tells (`MonitorHeld`, the 80% rule, etc.) |
| `netcore-lldb://known-issues` | Workarounds for upstream SOS bugs (e.g. .NET 10 `gcroot` segfault) |

**Prompts** (7) — `/`-completions for scenario kickoff (see Step 4 above).

The playbooks are distilled from
[Tess Ferrandez's .NET debugging labs](https://www.tessferrandez.com/postindex/) —
the canonical reference for managed-dump analysis.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `error: target ... does not have lldb installed` | Target missing lldb and you didn't pass `--bootstrap` | Re-run with `--bootstrap` |
| `bootstrap: package install failed` | Target is non-root without passwordless sudo, or no internet | Install lldb manually (`apt install lldb`), or run the client from a privileged context, or use a target with internet |
| `bootstrap: ... dotnet not found on target` | Target lacks `dotnet` (rare — usually your .NET app's container has it) | Install the .NET runtime or SDK on the target before `--bootstrap` |
| `the target has no associated executable images` (then SOS failures) | You're analyzing the dump on a target that doesn't have your app's binary at the dump-recorded path | Either run the analysis in the container where the app actually runs (the natural flow), or copy the binaries over and add `--exec /path/to/binary` |
| `Failed to find runtime module (libcoreclr.so)` from SOS commands | Target's .NET install path doesn't match what the dump expects | Make sure the target IS the same environment where the crash happened, OR bind-mount the matching `/usr/lib/dotnet` into the target |
| `clrstack` shows method names but no file/line | PDBs aren't on the target next to the DLLs | Copy your `publish/` output into the target, e.g. `docker cp ./publish/. my-container:/app/` |
| `gcroot` returns "tool raised: target process exited" | Known .NET 10 DAC bug — `gcroot` segfaults `libmscordaccore.so` on some dumps | The session keeps working; tell Claude to use heap-stat-based reasoning (`dumpheap -stat`, `dumpheap -type X`) instead |

---

## Repository layout

```
.
├── README.md
├── .gitignore
├── .github/
│   └── workflows/
│       └── publish-image.yml   # auto-publish reference image to ghcr.io on v* tags
├── client/                     # THE PRODUCT
│   ├── netcore_lldb.py         # MCP client (single file, stdlib only)
│   ├── netcore-lldb            # bash wrapper for PATH
│   └── netcore-lldb.cmd        # Windows wrapper
├── reference-image/            # optional sandbox image (published to ghcr.io)
│   ├── Dockerfile
│   └── README.md
├── sample/                     # tiny crashing .NET 10 app for self-testing
│   ├── Crasher.csproj
│   └── Program.cs
└── tests/                      # comprehensive end-to-end test suite
    ├── run_all.py              # 68 MCP-protocol-level tests (docker + ssh transports)
    ├── fixtures/               # curated .NET fixture apps (memory leak, deadlock, event-handler leak)
    │   └── build-and-dump.sh
    ├── setup-sshd.sh           # one-time: enable sshd on WSL for SSH tests
    ├── build-image.sh          # one-time: docker build of the reference image
    └── install-docker.sh       # one-time: install Docker CE on Ubuntu WSL
```

## Building from source / development

If you're modifying the tool itself:

```bash
# build the reference image (a target you can develop against without
# touching production containers)
./tests/build-image.sh

# build the fixture dumps used by tests
./tests/fixtures/build-and-dump.sh

# run the full test suite (68 tests, docker + ssh transports)
python3 tests/run_all.py
```

The test suite covers:

- **Argument parsing** — `--docker`/`--ssh` mutual exclusion, missing flags, `--help`, unknown args.
- **Preflight** — unreachable container/host, target missing lldb, missing dump.
- **MCP protocol** — `initialize` (with all three capabilities), `tools/list`, `tools/call`, `resources/list`, `resources/read` (static + dynamic + error paths), `prompts/list`, `prompts/get` (with and without `user_description`), `ping`, `shutdown`, unknown method.
- **SOS commands** — every shipped command (`eeversion`, `clrthreads`, `pe`, `clrstack`, `clrstack -a`, `clrstack -all`, `clrmodules`, `dumpheap -stat`, `dso`, `eeheap -gc`, `threadpool`, `dumpasync`, `gcheapstat`) — verifies coherent SOS responses.
- **Stateful session** — `thread select` carries to subsequent `clrstack`.
- **Large output** — `dumpheap -stat` (~15 KB) doesn't break prompt detection.
- **Concurrent calls** — 8 rapid tool calls roundtrip without tangling.
- **Wrapper invocation** — `client/netcore-lldb` launches the python script correctly.
- **`--copy-dump`** — local-to-target dump copy with default and explicit `--target-dump-path`.
- **`--bootstrap`** — fresh install on a bare target AND idempotent fast-path on an already-set-up target.
- **Fixture playbooks** — three curated dumps (memory leak, deadlock, event-handler leak) verify each Tess playbook end-to-end.

## Releases

Each `v*` git tag triggers
[`.github/workflows/publish-image.yml`](.github/workflows/publish-image.yml),
which builds `reference-image/Dockerfile` and pushes it to
`ghcr.io/cadneowl/netcore-lldb:<version>` and `:latest`. Anonymous pulls
work — the package is public.

Current release: **v0.3.0** (client tool v0.3.0, reference image
`ghcr.io/cadneowl/netcore-lldb:v0.3.0`).
