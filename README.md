# mena

`mena` is a privacy-first local meeting notes project.

The repository root is the product suite and knowledge base, not the Python package root.

- project documentation lives under [docs/](docs/README.md)
- runnable software lives under [app/](app/)
- repository workflow is documented in [docs/repository-workflow.md](docs/repository-workflow.md)

Initial local app checks:

```bash
cd app
uv sync --all-groups
make check
```
