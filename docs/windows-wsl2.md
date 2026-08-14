# Windows agents with CodeBadger in WSL2

This layout runs Docker Engine entirely inside an Ubuntu WSL2 distribution.
Windows clients connect to CodeBadger over WSL localhost forwarding:

    Windows agent -> http://127.0.0.1:4242/mcp -> Ubuntu WSL2 -> CodeBadger containers

Docker Desktop is not required and should not be used by these commands.

## Prerequisites

1. Enable systemd in the Ubuntu distribution.
2. Install Docker Engine and the Compose plugin inside Ubuntu using Docker's
   [Ubuntu installation guide](https://docs.docker.com/engine/install/ubuntu/).
3. Enable the daemon and allow the Linux user to access its socket:

       sudo systemctl enable --now docker
       sudo usermod -aG docker "$USER"

4. Start a fresh WSL login, then confirm both commands use the native engine:

       docker info
       docker compose version

If docker compose or docker buildx resolves to a broken path below
/mnt/wsl/docker-desktop, remove or disable that stale CLI-plugin symlink so
Docker can discover the native plugin under /usr/libexec/docker/cli-plugins.

## Configure CodeBadger

Run these commands inside Ubuntu:

    git clone https://github.com/lekssays/codebadger
    cd codebadger
    cp .env.example .env
    cp config.example.yaml config.yaml
    python scripts/recommend_config.py

Set these connectivity values in .env:

    MCP_HOST=0.0.0.0
    MCP_PORT=4242
    MCP_PUBLISH_HOST=127.0.0.1
    PLAYGROUND_HOST_PATH=/absolute/linux/path/to/codebadger/playground
    POSTGRES_DATA_PATH=/absolute/linux/path/to/codebadger/pgdata
    DOCKER_HOST=unix:///var/run/docker.sock
    ALLOWED_SOURCE_ROOTS=/app/playground:/mnt/c/Users/<windows-user>/path/to/chosen/repo

MCP_HOST=0.0.0.0 lets the service receive traffic through the container port.
MCP_PUBLISH_HOST=127.0.0.1 keeps the Windows-facing listener loopback-only.
ALLOWED_SOURCE_ROOTS is a colon-separated allowlist. Add only the Windows
directories that agents should be permitted to analyze.

Apply the memory values printed by recommend_config.py. On a 32 GiB WSL
instance, this tested allocation leaves room for the operating system and the
other services:

    JOERN_MEM_LIMIT=9g
    JOERN_MEMORY_BUDGET_MB=10240
    CPG_BUILD_WORKERS=1
    CPG_BUILD_HEAP_GB=6
    MAX_MCP_CONNECTIONS=12

The generation limit plus query-worker budget must fit within the memory
allocated to WSL. The build-worker count multiplied by its heap must not exceed
the generation limit.

## Start and verify manually

Inside Ubuntu:

    ./scripts/deploy.sh
    ./scripts/deploy.sh status

From Windows PowerShell:

    Invoke-RestMethod http://127.0.0.1:4242/health

The healthy response reports status: up and lists Joern, Postgres, Redis,
Docker, and the CPG queue as up.

## Connect Windows Codex on demand

WSL does not need to start at Windows login. This repository includes a
project-scoped STDIO bridge:

    [mcp_servers.codebadger]
    command = "pwsh.exe"
    args = [
      "-NoLogo",
      "-NoProfile",
      "-NonInteractive",
      "-ExecutionPolicy",
      "Bypass",
      "-Command",
      "$root = (& git rev-parse --show-toplevel); if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; & (Join-Path $root 'scripts/start_codebadger_mcp.ps1')",
    ]
    enabled = true
    required = false
    startup_timeout_sec = 300
    tool_timeout_sec = 600
    default_tools_approval_mode = "prompt"

    [mcp_servers.codebadger.env]
    CODEBADGER_WSL_DISTRO = "Ubuntu"
    CODEBADGER_WSL_PROJECT_DIR = "/home/<linux-user>/codebadger"
    CODEBADGER_COMPOSE_PROJECT = "codebadger"
    CODEBADGER_HEALTH_URL = "http://127.0.0.1:4242/health"
    CODEBADGER_DOCKER_NETWORK = "codebadger"
    CODEBADGER_PROXY_IMAGE = "codebadger-mcp:latest"
    CODEBADGER_UPSTREAM_URL = "http://codebadger-mcp:4242/mcp"
    CODEBADGER_TOOL_PROFILE = "controller"
    CODEBADGER_BOOT_TIMEOUT_SEC = "180"

When an agent loads this trusted repository, Codex starts
scripts/start_codebadger_mcp.ps1. The bridge:

1. Reuses CodeBadger immediately when its Windows loopback health endpoint is up.
2. Otherwise wakes only the configured Ubuntu distribution, starts its native
   Docker service, starts the existing CodeBadger Compose project, and waits for
   health. A named Windows mutex prevents simultaneous agents from racing startup.
3. Starts one disposable proxy container per agent. FastMCP translates Codex's
   STDIO connection to CodeBadger's internal HTTP endpoint.

`CODEBADGER_TOOL_PROFILE=controller` exposes the complete tool set to Polly. A
dispatcher for an Audit or review worker should launch the same bridge with
`CODEBADGER_TOOL_PROFILE=query-only`. That profile is enforced in the proxy as
an exact allowlist: it includes status and graph-query tools but omits
`generate_cpg` and `remove_cpg`. Newly added backend tools remain unavailable to
query-only workers until deliberately added to the allowlist.

The bridge layers docker-compose.codex.yml over the base Compose file. Pool-mode
Joern workers communicate over the internal codebadger network, so this override
removes the base file's 500-port host-debug publication. Avoid starting this
deployment later with the base file alone, which would restore those port
mappings and make the next Docker/WSL cold start slow again.

The proxy is read-only, capability-dropped, memory/PID limited, and has no Docker
socket or source-code mount. Its foreground wsl.exe process acts as a lifetime
lease: it keeps WSL available while that agent is connected. When Codex closes
stdin, the proxy exits and is removed. WSL may then stop naturally when no other
Windows or WSL work remains; the bridge never terminates the shared distribution.

CodeBadger remains scoped to this repository because the registration lives in
.codex/config.toml. The user-level Codex configuration only needs to mark the
repository trusted; it does not need a global CodeBadger MCP entry.

Verify discovery from that repository:

    codex mcp list
    codex mcp get codebadger

Test only the startup/health portion without opening an MCP session:

    pwsh -NoProfile -File scripts/start_codebadger_mcp.ps1 -BootstrapOnly

Restart the Codex client or open a new agent session after changing MCP
configuration. Codex's official
[MCP documentation](https://developers.openai.com/codex/mcp/) describes
project-scoped configuration and command-launched STDIO servers.

## Source paths

- GitHub URLs and pasted snippets need no additional path mapping.
- A Windows path such as C:\Users\me\repos\project is /mnt/c/Users/me/repos/project
  from Ubuntu and from CodeBadger's local-source API.
- Add the selected Windows directory, or a narrow parent of it, to
  ALLOWED_SOURCE_ROOTS. Recreate the MCP container after changing .env.
- Call generate_cpg with source_type="local" and the /mnt/c/... path. CodeBadger
  uses a short-lived helper container with networking disabled and the selected
  source mounted read-only, then copies a snapshot into the WSL playground. The
  Windows tree is not permanently mounted into the MCP or Joern containers.
- Each CPG hash is a point-in-time snapshot. Windows edits do not update an
  already-loaded graph. Call generate_cpg again after edits and use the newly
  returned hash for subsequent queries. File-content fingerprinting plus the
  effective graph-build specification reuses the cached CPG only when both the
  source and graph-shaping options match. `force=true` is not needed for
  refreshes; it controls only the large-project warning.
- Sources already stored in WSL can still be placed under the checkout's
  playground directory, which the MCP container sees below /app/playground.

Example from a Windows agent:

    generate_cpg(
        source_type="local",
        source_path="/mnt/c/Users/me/repos/project",
        language="python"
    )

Poll get_cpg_status with the returned hash until it reports ready, and repeat
generate_cpg after a batch of edits before asking graph-dependent questions.

## Cache identity and refresh safety

Local CPG cache identity uses:

- the verified source-tree fingerprint;
- language;
- ordered include paths and defines;
- include globs;
- effective automatic system-header behavior;
- explicit compilation-database path, or whether automatic database discovery
  is active (the source fingerprint covers the selected database contents);
- effective configured exclusion patterns.

The identity is namespaced by `cpg-cache-v2`. The first refresh after this
change therefore builds or selects a v2 entry instead of reusing an older entry
whose key omitted some of those inputs. Old artifacts are not deleted by the
namespace change.

If CodeBadger cannot fingerprint a local source, `generate_cpg` now fails before
cache lookup or source staging. It never falls back to a path-only hash, so an
explicit checkpoint refresh cannot silently return a graph from older source.

## Explicit checkpoint lifecycle

The disposable client-side pointer lives at
`.codebadger/active-cpgs.json` and is ignored by Git. Polly is its only writer;
query agents only receive the resolved language-to-hash mapping.

Before source writes resume, invalidate the target:

    python scripts/codebadger_checkpoint_state.py invalidate audit-baseline

At a checkpoint, hold source stable, call `generate_cpg` for every applicable
language, and poll `get_cpg_status` until every returned hash is ready. Then
replace the complete mapping atomically:

    python scripts/codebadger_checkpoint_state.py activate audit-baseline `
      --codebase python=0123456789abcdef `
      --codebase javascript=fedcba9876543210

Resolve the mapping supplied to an Audit or review worker:

    python scripts/codebadger_checkpoint_state.py resolve audit-baseline

For a single-language consumer:

    python scripts/codebadger_checkpoint_state.py resolve audit-baseline --language python

An absent or invalidated target fails closed instead of returning an older hash.
There is no watcher and no history in this state file: presence means the
mapping is active for the current stable-source interval; invalidation removes
it when that interval ends.

The Polly/Claude dispatcher repository must call these transitions at its
existing process checkpoints, set `CODEBADGER_TOOL_PROFILE=query-only` for
Audit/review launches, and pass the resolved mapping to those workers. This
CodeBadger repository provides the state and enforcement primitives but does not
contain that dispatcher.

Do not expose port 4242 beyond loopback without adding authentication and an
authenticated proxy. The MCP service has no built-in authentication, and its
container intentionally controls the WSL Docker socket.
