# Reference image

This directory builds a convenience Docker image that you can point the
`netcore-lldb` client at — `lldb-20`, the SOS plugin, the .NET 8 and .NET 10
runtimes, and the DAC's transitive dependencies are all pre-installed.

The image is **not the product** — the product is the client tool in
`../client/`. The reference image exists for two reasons:

1. As a quick way to try `netcore-lldb` without modifying your existing
   container infrastructure.
2. As a documented example of what a "debugging-ready target" needs.

## Build

```bash
docker build -t netcore-lldb:dev ../reference-image
```

(Or use `tests/build-image.sh` from the repo root.)

## Use

Start a long-running container from the image:

```bash
docker run -d --name dump-analyzer --entrypoint sleep \
    netcore-lldb:dev infinity
```

Stage your dump and published app inside it:

```bash
docker cp /path/to/dump.core dump-analyzer:/work/dump.core
docker cp /path/to/publish/. dump-analyzer:/work/app/
```

Then drive the client against it:

```jsonc
{
  "mcpServers": {
    "netcore-lldb": {
      "command": "netcore-lldb",
      "args": [
        "--docker", "dump-analyzer",
        "--dump",   "/work/dump.core",
        "--exec",   "/work/app/MyApp"
      ]
    }
  }
}
```

## What's installed

| Component | Version | Why |
|---|---|---|
| Ubuntu | 24.04 (noble) | base |
| lldb | 20.1.x | debugger frontend; SOS plugs into this |
| .NET SDK | 10.0 | matches dumps from .NET 10 apps |
| .NET ASP.NET runtime | 8.0 | matches dumps from .NET 8 apps |
| `dotnet-debugger-extensions` | 10.0.x | provides `libsosplugin.so`, registers it in `.lldbinit` |
| `libunwind8`, `liblttng-ust1` | distro | DAC dlopens these at runtime |
| `bsdmainutils` (for `script`) | distro | gives lldb a remote pty when driven over docker exec / ssh |

`/usr/lib/dotnet` is symlinked to `/usr/share/dotnet` so dumps captured
against Ubuntu's apt-installed .NET (recorded paths under `/usr/lib/dotnet`)
resolve transparently inside this image (which uses the SDK image's
`/usr/share/dotnet` layout).

The image is ~500 MB compressed, ~1.9 GB uncompressed.
