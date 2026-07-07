# build123d-mcp

[![PyPI version](https://img.shields.io/pypi/v/build123d-mcp)](https://pypi.org/project/build123d-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/build123d-mcp)](https://pypi.org/project/build123d-mcp/)
[![CI](https://github.com/pzfreo/build123d-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/pzfreo/build123d-mcp/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![build123d-mcp MCP server](https://glama.ai/mcp/servers/pzfreo/build123d-mcp/badges/score.svg)](https://glama.ai/mcp/servers/pzfreo/build123d-mcp)

Give your AI CAD eyes.

build123d-mcp is not a standalone chatbot or CAD program. It is a CAD toolbox
that an AI/LLM app can use through MCP.

With an LLM app such as Claude, Cursor, VS Code, Continue, Cline, or Codex CLI,
build123d-mcp lets the assistant create build123d CAD models, render previews,
measure geometry, fix mistakes, and export files such as STEP, STL, SVG, and
DXF. Instead of writing a whole CAD script blindly, the assistant can build a
part in small steps and check the result as it goes.

On the public [CADGenBench](https://huggingface.co/spaces/HuggingAI4Engineering/CADGenBench)
leaderboard in June 2026, using build123d-mcp raised the same model's score from
0.360 to 0.457 and CAD validity from 88% to 100%.

## Pick Your Setup

Most users should start with the local MCP server setup:

- You use Claude, Cursor, VS Code, Continue, Cline, or Codex CLI on your own
  machine.
- Your AI app starts `build123d-mcp` as a local subprocess.
- You do not need to clone this repository.

Use GitHub Codespaces instead if you want a browser-only trial or a ready-made
development workspace with Copilot Chat, Python, uv, and the CAD dependencies
already installed.

Use HTTP mode only for advanced deployments where you are hosting the MCP server
yourself.

## Quick Start

You need:

- [uv](https://github.com/astral-sh/uv)
- An AI/LLM app that supports MCP, such as Claude Code, Claude Desktop, Cursor,
  VS Code, Continue, Cline, or Codex CLI

No repository clone is needed for normal use. First check that the package can
start:

```bash
uv tool run --python 3.12 build123d-mcp@latest --version
```

Then add the same command to your AI app's MCP config. The common pieces are:

```text
command: uv
args:    ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
```

> Python 3.11, 3.12, 3.13, and 3.14 are supported. The examples use 3.12 because
> it is a conservative default, and uv can download it if you do not already
> have it installed.

## Connect To Your AI App

The pieces are:

- The LLM app is where you chat with the assistant.
- MCP is the connection that lets the assistant call tools.
- build123d-mcp is the CAD tool server the assistant calls.

The server normally runs over stdio. Your AI app starts it as a local subprocess
when it needs the CAD tools.

### Claude Code

Add this to your project's `.mcp.json`, or to `~/.claude/mcp.json` for global
use:

```json
{
  "mcpServers": {
    "build123d-mcp": {
      "command": "uv",
      "args": ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
    }
  }
}
```

Restart Claude Code after editing.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
or `%APPDATA%\Claude\claude_desktop_config.json` on Windows:

```json
{
  "mcpServers": {
    "build123d-mcp": {
      "command": "uv",
      "args": ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
    }
  }
}
```

Restart Claude Desktop after saving.

### Cursor

Open **Settings -> MCP** and add a new server entry, or edit
`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "build123d-mcp": {
      "command": "uv",
      "args": ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
    }
  }
}
```

### VS Code, Continue, Cline, Codex CLI

Use the same command and arguments in whichever MCP config your AI app or
extension reads. The exact filename varies by app, but the server command is the
same:

```text
command: uv
args:    ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
```

For GitHub Copilot MCP support in VS Code, this repository includes a
`.vscode/mcp.json` for development checkouts and Codespaces. For another
workspace, the config looks like this:

```json
{
  "servers": {
    "build123d-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["tool", "run", "--python", "3.12", "build123d-mcp@latest"]
    }
  }
}
```

## First Test Prompt

Once your AI app is connected to the server, ask your assistant something
concrete:

```text
Use build123d-mcp to make a 60 mm x 40 mm x 6 mm mounting plate with two
5 mm through holes 40 mm apart. Render it, measure it, then export STEP and STL.
```

The useful loop is:

1. Build one feature at a time.
2. Render or measure after important steps.
3. Validate before export.
4. Export the final part.

If something goes wrong, ask the assistant to inspect `last_error`, repair the
script, and try the next smaller step.

Good prompts usually ask the assistant to use the MCP tools explicitly and to
verify the result before exporting. For example:

```text
Use build123d-mcp. Build this incrementally, render after the main features,
measure the final dimensions, run validate(), and export STEP if it passes.
```

## Try It In GitHub Codespaces With Copilot

You can also run the project in a browser with GitHub Codespaces:

[Open in GitHub Codespaces](https://codespaces.new/pzfreo/build123d-mcp)

For a beginner, this is the closest path to "GitHub-hosted LLM + MCP":

- GitHub Codespaces gives you VS Code in the browser.
- GitHub Copilot Chat gives you the LLM assistant.
- build123d-mcp runs inside the codespace as the MCP CAD tool server.

You need GitHub Copilot access for the LLM part. The codespace itself gives you
a throwaway workspace with the project already checked out and the right
Python/CAD dependencies installed. It is useful when you want to:

- Try the project without changing your laptop setup
- Run the tests before making a contribution
- Use Copilot Chat and build123d-mcp together in the same browser workspace

GitHub's docs cover:

- [Using Copilot in Codespaces](https://docs.github.com/en/codespaces/reference/using-github-copilot-in-github-codespaces)
- [Extending Copilot Chat with MCP](https://docs.github.com/copilot/customizing-copilot/using-model-context-protocol/extending-copilot-chat-with-mcp)

This repository includes a dev container that installs Python 3.12, uv, and the
Linux display packages needed for headless rendering. It also installs the
GitHub Copilot VS Code extensions and includes a workspace MCP config at
`.vscode/mcp.json`.

When the codespace opens, it runs:

```bash
uv sync --all-groups
```

To check the development install:

```bash
uv run build123d-mcp --version
uv run pytest
```

To use Copilot with the local CAD server:

1. Open the codespace.
2. Wait for setup to finish.
3. Open Copilot Chat and choose Agent mode.
4. Open `.vscode/mcp.json` and start the `build123d-mcp` server if VS Code has
   not started it already.
5. Ask the first test prompt from the previous section.

The Codespaces MCP config points at the local checkout rather than the PyPI
package:

```json
{
  "servers": {
    "build123d-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "build123d-mcp"]
    }
  }
}
```

Codespaces is a good fit for trying the project, contributing, or using GitHub
Copilot and build123d-mcp in one remote environment. For desktop AI apps on your
own machine, the normal `uv tool run ... build123d-mcp@latest` setup is simpler
because those apps expect to start the MCP server locally.

## What It Can Do

build123d-mcp gives an assistant tools to:

- Execute build123d code in a persistent CAD session
- Render PNG, SVG, and DXF previews
- Measure volume, area, bounding boxes, topology, and centers of mass
- Find holes, bosses, countersinks, and hole patterns
- Check printability, clearances, alignment, and export validity
- Import STEP/STL files for comparison
- Export STEP, STL, DXF, SVG, or multiple formats at once
- Save and restore session snapshots
- Produce 2D engineering drawing previews

For the complete tool and resource reference, see [llms.md](llms.md).

## Guidance For Assistants

The server includes workflow guidance that helps assistants use the CAD loop
properly. This is especially useful in coding agents that read project guidance
files.

After connecting the server, ask your assistant to call `install_skill` for the
workflow you need:

```text
install_skill(target="agents-md", skill="modeling")
install_skill(target="agents-md", skill="drawing")
install_skill(target="agents-md", skill="repair")
```

`install_skill` also supports `target="claude"`, `"cursor"`, and `"windsurf"`.
Use `skill="modeling"` for 3D parts, `skill="drawing"` for engineering drawings,
and `skill="repair"` when a solid fails validation.

You can also paste [default_prompt.md](default_prompt.md) into your AI app as a
system prompt.

## Developer Setup

For local development:

```bash
git clone https://github.com/pzfreo/build123d-mcp.git
cd build123d-mcp
uv sync --all-groups
uv run build123d-mcp --version
uv run pytest
```

To run the server from this checkout in an AI app, use:

```text
command: uv
args:    ["run", "build123d-mcp"]
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.

## Advanced Use

The default stdio transport gives each app process its own isolated session.
HTTP mode is available for web, container, or remote deployments:

```bash
uv tool run --python 3.12 "build123d-mcp[http]@latest" \
  --transport http --host 127.0.0.1 --port 8000
```

HTTP-capable apps then connect to:

```text
http://localhost:8000/mcp
```

HTTP mode has no built-in authentication and uses one shared CAD session unless
the host provides per-request session middleware. Do not expose it to multiple
users directly.

Other advanced topics:

- Security model and sandboxing: [security.md](security.md)
- Complete tool reference: [llms.md](llms.md)
- Live session viewer: [docs/live-viewer.md](docs/live-viewer.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)

## Status

Active development.

<!-- mcp-name: io.github.pzfreo/build123d-mcp -->
