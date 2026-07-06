# radar-plugin-sdk

The plugin SDK for RADAR: base classes, a protocol-conformance registry, and a
config-driven loader that instantiate the backend plugins in `plugins/*`.

## Rules

- **Zero vendor imports.** Depends only on `radar-contracts`, `pydantic`, and
  the standard library. No `anthropic`, `openai`, `slack-sdk`, Elasticsearch,
  or any other SDK — those live inside individual plugins.
- **`typing.Protocol` conformance**, not ABC inheritance. Plugins are validated
  structurally against the `radar-contracts` backend Protocols.
- **mypy strict** must pass.

## Contents

| Module        | Purpose                                                       |
| ------------- | ------------------------------------------------------------ |
| `base.py`     | Shared plugin base/metadata types.                           |
| `registry.py` | Register plugins and check them against a contract Protocol. |
| `loader.py`   | Instantiate a backend from configuration.                    |
| `config.py`   | Pydantic config models for plugin selection.                 |
