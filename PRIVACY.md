# Privacy & data flow

> A .NET memory dump contains the **entire process memory** at the moment of
> the crash. That includes user data, secrets, tokens, cached request/response
> bodies, environment variables — anything the process touched. This tool's
> job is to make slices of that memory readable, so by definition some of
> what's in the dump can reach the LLM. Read this page before pointing
> `netcore-lldb` at a **production** dump.

## How data flows when you use this tool

```
[your dump on the target]                  [your laptop]                [Anthropic]
       │                                         │                            │
       │   lldb command output                   │   prompts + context        │
       └──────────► MCP server ──────────────────► Claude Code ───────────────►
                  (netcore-lldb)                  (your machine)        (API + LLM)
```

What crosses each arrow:

| Arrow | Crosses | Sensitivity |
|---|---|---|
| **dump → MCP server** | Whatever `lldb` + SOS produce for the commands Claude runs | Whatever's in the dump |
| **MCP server → Claude Code** | The same text, framed as MCP `tools/call` results | Same as above |
| **Claude Code → Anthropic API** | The MCP results, plus any source files Claude opened from your repo, plus your conversation | Per Anthropic's privacy policy |

The dump file itself **never leaves the target.** Source code stays on your
laptop, but **files Claude opens during the conversation get sent to
Anthropic** as part of its context.

## Which SOS commands leak what

| Command | What it can expose | Risk |
|---|---|---|
| `pe`, `pe -nested` | Exception messages often embed user data (`"Failed to validate user 'alice@example.com'"`, `"Invalid card 4111-1111-1111-1111"`) | 🔴 high |
| `clrstack -a` | Managed stack with **locals + parameters** — anything a thread was processing at crash | 🔴 high |
| `dso` (dump stack objects) | Heap objects referenced from the stack — usually whatever the failing call was operating on | 🔴 high |
| `dumpobj <addr>` | Every field of one object (`User`: name, email, hashed password, JWT, …) | 🔴 high |
| `dumpheap -type System.String` | **Every string on the heap.** Cached rows, tokens, request bodies, connection strings — everything | 🔴 highest |
| `du <addr>`, `memory read` | Raw memory contents at an address | 🔴 high |
| `clrthreads` | Per-thread state including pending exception address + type name (combine with `pe` for the value) | 🟡 moderate |
| `dumpheap -stat` | Type names + aggregate counts/sizes only — no field values | 🟢 low |
| `eeheap -gc`, `eeversion`, `clrmodules`, `threadpool`, `gcheapstat` | Structural / version info, no user data | 🟢 low |
| `target_info`, `netcore-lldb://session`, `netcore-lldb://modules` | Container name, hostname (`uname`), lldb version, dump path, module list | 🟡 moderate (hostnames/paths are identifying) |

The default playbooks shipped in `netcore-lldb://playbook/*` instruct Claude
to run `pe -nested`, `clrstack -a`, and `dumpheap -stat`/`-type` early — so
**on a prod dump, customer data will reach Anthropic's API by default**.

## Anthropic-side considerations

- **Retention.** Anthropic's policy (as of 2026-05): paid-API requests
  aren't used for training, but logs may be retained for safety review (up
  to ~30 days for normal usage; longer for trust-and-safety incidents).
- **Zero Data Retention (ZDR)** is available on Enterprise plans — that
  eliminates the 30-day window.
- **Workspace / team features.** Conversations in a shared workspace may
  be visible to workspace admins per the workspace's configuration.
- **Claude Code chat history.** Stored locally in Claude Code on the
  developer's machine.

If you're under HIPAA, PCI-DSS, or GDPR for data subjects whose info might
be in the dump, talk to Anthropic about a ZDR agreement (or pick a
mitigation below that doesn't involve sending dump contents off-machine).

## Mitigations — best to most expensive

### 1. Don't analyze prod dumps directly

Repro the crash in staging with synthetic data, capture that dump, point
`netcore-lldb` at it. Best signal-to-noise; zero PII risk. The friction is
the repro work.

### 2. Capture a smaller dump type

[`DOTNET_DbgMiniDumpType`](https://learn.microsoft.com/en-us/dotnet/core/diagnostics/dumps)
controls what's included:

| Value | Type | Contents | PII surface |
|---|---|---|---|
| `1` | Mini | Threads + stacks, **minimal** heap | Stack locals only |
| `2` | Heap | Threads + GC heap | High — full managed heap |
| `3` | Triage | Threads + minimal heap, **scrubbed of typical PII paths** | Reduced |
| `4` | Full | Everything | Highest |

For a prod incident where you want analysis help but not full PII exposure:
**capture type 1 or type 3**. You lose `dumpheap -stat` insight (no full
heap to walk), but `pe`, `clrstack`, and `clrthreads` still work — those
cover most crash analyses.

### 3. Prompt Claude to be conservative

Add a project-level note in Claude Code's `CLAUDE.md` or the conversation:

> When analyzing this dump, **do not** run `dumpheap -type System.String`
> or commands that dump raw string contents. Summarize structural findings
> (types, counts, retention chains) without quoting field values. Treat
> any string you happen to see as potentially-PII and do not echo it back
> in your reply.

The LLM follows instructions and our tool doesn't override them. This is
the cheapest, fastest, *partial* mitigation.

### 4. Zero Data Retention agreement with Anthropic

For Enterprise customers: contact Anthropic to enable ZDR for your
workspace. Requests and responses aren't retained server-side. Pricing /
contracting is enterprise-tier.

### 5. Run a local LLM

The MCP server is LLM-agnostic. Any MCP-capable client works (Cursor,
Continue, custom MCP clients, etc.) — including ones connected to a
self-hosted local model. Claude Code won't apply; the rest of the
architecture does.

### 6. Air-gapped analysis

Run the MCP server, the MCP client, and the LLM on a network-isolated host
that never reaches the Anthropic API. Operationally the heaviest, but the
only mitigation that strictly guarantees no off-machine data flow.

## What we deliberately don't do (and why)

We don't redact or filter the output of `lldb_command` before sending it
back. Reasons:

- **Authenticity matters.** If we silently scrubbed a string, the LLM might
  misdiagnose a bug whose tell is a specific value. False confidence is
  worse than honest signal.
- **No reliable PII detector.** A pattern matcher would miss customer
  data that doesn't match its regexes and over-trigger on harmless text.
- **The user is in the loop.** Claude Code shows every tool response in
  the chat. The customer can see what's about to be sent and stop the
  session if it's too sensitive.

If you want filtering anyway, you can wrap our tool in your own MCP server
that intercepts `tools/call` responses and redacts them before passing
along. We'd happily review a PR that adds an optional `--redact-strings`
flag for the common case.

## Reporting a privacy concern

If you think this tool's behavior creates a privacy risk we haven't
considered, please open an issue at
https://github.com/cadneowl/netcore-lldb/issues or email the repo owner.
