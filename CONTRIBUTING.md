# Contributing to adk-kanode

Thanks for your interest in improving `adk-kanode`. Bug reports, feature ideas,
documentation fixes and pull requests are all welcome.

## Reporting bugs and requesting features

Open an issue at <https://github.com/benmizrahi/adk-kanode/issues> and pick the
matching template. For bugs, please include:

- the `adk-kanode`, `google-adk` and Python versions,
- the execution mode (`deployment` or `pod`) and the Kubernetes / GKE version,
- a minimal `@kanode` node and workflow that reproduces the problem,
- the full traceback and, where relevant, the worker pod logs.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/) and requires Python 3.12+.

```bash
git clone https://github.com/benmizrahi/adk-kanode.git
cd adk-kanode
uv sync          # install dependencies
uv run pytest    # run the tests
```

The tests do not need a cluster: they run nodes through the local executors
(see [Running without a cluster](README.md#running-without-a-cluster)) and
fake Kubernetes API clients.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make your change, and add or update tests in [`tests/`](tests/) for it.
3. Run `uv run pytest` and make sure it passes.
4. Update [`README.md`](README.md) or
   [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) if behaviour or configuration
   changes.
5. Open a pull request that explains what changed and why.

Keep pull requests focused on one change; small PRs are reviewed faster.

## Project layout

See the [Development](README.md#development) section of the README for a map of
the source files, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the
pieces fit together.
