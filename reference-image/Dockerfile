# netcore-lldb reference image — a convenience target you can point the
# netcore-lldb client at.
#
# This image is NOT the product (the product is the client tool in ../client).
# It's just a debug-friendly Ubuntu container with everything pre-installed
# so customers who don't already have lldb + SOS in their app's container
# can spin one of these up and use it as the --docker target.
#
# Typical usage:
#   docker run -d --name dump-analyzer --entrypoint sleep netcore-lldb infinity
#   docker cp /path/to/dump.core dump-analyzer:/work/dump.core
#   docker cp /path/to/publish/.  dump-analyzer:/work/app/
#   # then in your .mcp.json: --docker dump-analyzer --dump /work/dump.core --exec /work/app/<host>
#
# Base: Microsoft .NET 10 SDK on Ubuntu 24.04 (noble). We add the .NET 8
# runtime alongside so SOS finds a matching DAC when analyzing dumps from
# .NET 8 apps too.

FROM mcr.microsoft.com/dotnet/sdk:10.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    DOTNET_NOLOGO=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    NETCORE_LLDB_REFERENCE_IMAGE=1

# ---- system packages --------------------------------------------------------
# lldb-20 + the SOS DAC's runtime dependencies (libunwind, liblttng-ust).
# binutils gives `strings`/`readelf` for the rare case the customer wants
# to inspect a dump by hand. `script` (from bsdmainutils) is what we use to
# give lldb a pty when driven over docker exec / ssh.
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        lldb-20 \
        python3-lldb-20 \
        python3 \
        ca-certificates \
        curl \
        file \
        binutils \
        libunwind8 \
        liblttng-ust1 \
        bsdmainutils && \
    rm -rf /var/lib/apt/lists/*

# `lldb` on PATH → lldb-20.
RUN ln -sf /usr/bin/lldb-20 /usr/local/bin/lldb

# python3-lldb-20 on Ubuntu ships a broken liblldb.so symlink. Fix it so
# `import lldb` works (we don't use the Python API ourselves but it's part
# of a well-formed install).
RUN ln -sf /lib/x86_64-linux-gnu/liblldb-20.so.1 /usr/lib/llvm-20/lib/liblldb.so

# ---- .NET 8 runtime alongside the SDK image's .NET 10 ----------------------
# Use dotnet-install.sh to land it in the same DOTNET_ROOT as the base
# image. Installing via apt would bring a second `dotnet` host that fights
# with the SDK from the base image (we hit this in development).
RUN curl -sSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh && \
    chmod +x /tmp/dotnet-install.sh && \
    /tmp/dotnet-install.sh --runtime aspnetcore --channel 8.0 --install-dir /usr/share/dotnet && \
    rm /tmp/dotnet-install.sh

# ---- SOS plugin ------------------------------------------------------------
# `dotnet-debugger-extensions install` writes /root/.lldbinit so lldb
# auto-loads libsosplugin.so at startup. That .lldbinit is then used by
# every `docker exec ... lldb` invocation against this container.
RUN dotnet tool install --global dotnet-debugger-extensions && \
    ln -sf /root/.dotnet/tools/dotnet-debugger-extensions /usr/local/bin/ && \
    /root/.dotnet/tools/dotnet-debugger-extensions install --accept-license-agreement

# Symlink /usr/lib/dotnet -> /usr/share/dotnet so dumps captured against
# Ubuntu's apt-installed .NET (which lives at /usr/lib/dotnet) resolve their
# recorded paths inside this container.
RUN ln -sf /usr/share/dotnet /usr/lib/dotnet

# Workdir customers can stage dump + binaries under via `docker cp`.
RUN mkdir -p /work /work/app

# ---- build-time self-check -------------------------------------------------
# Catches a broken image at build time rather than in production.
RUN lldb --version && \
    dotnet --list-runtimes | grep -qE '^Microsoft\.AspNetCore\.App 8\.' && \
    dotnet --list-sdks      | grep -qE '^10\.'                            && \
    test -f /root/.dotnet/sos/libsosplugin.so                              && \
    grep -q libsosplugin /root/.lldbinit                                   && \
    echo 'plugin load /root/.dotnet/sos/libsosplugin.so' | lldb --batch 2>&1 | grep -q "Loading extension"

# No ENTRYPOINT: customers do `docker run -d --entrypoint sleep <image> infinity`
# (or whatever keeps the container alive), then drive lldb via docker exec
# through the netcore-lldb client.
