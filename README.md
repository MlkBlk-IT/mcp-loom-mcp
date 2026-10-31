# mcp-loom-mcp

A portable MCP context server that moves context out of the prompt instead of summarising it away.

## Status

Work in progress. The project is being measured and repaired. It is not ready for production.

The repository documents current test results and limitations honestly. Historical demo figures are not production measurements.

## What it provides

- A portable context store backed by SQLite and FTS5
- MCP tools for storing, retrieving, verifying and auditing context
- Local dashboard support for inspecting activity
- stdio and HTTP transport options
- Explicit redaction and sandboxing helpers

## Installation

From the source tree:

```bash
python -m pip install -e .
```

Or install the development extras when they are defined by the project:

```bash
python -m pip install -e ".[dev]"
```

The package and registry status are documented in the repository's installation notes. Check those notes before relying on a published package.

## Dashboard

The dashboard can be started in demo mode:

```bash
python dash/server.py --demo
```

Demo figures are synthetic. They are useful for checking the interface, not for claiming token savings.

## How to evaluate it

Use the verification commands and test suite in the repository. Compare the same task with and without Loom, including retrieval and tool overhead. The project goal is not a pretty percentage; it is a repeatable measurement with the same success criteria.

## Author

Built by [Malik Belkacemi](https://github.com/MlkBlk-IT).
