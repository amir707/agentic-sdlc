# adk web debug entries

Interactive debugging for each reasoning worker: `make adk-web`
(runs `adk web tests/debug/adk_web`), pick an agent, chat with the
exact prompt/model/tools the pipeline uses (same spec, same adapter
— see `_bootstrap.py`). Requires model keys in `.env`; store-backed
agents also need the delivery store running (`make mcp`).

The one-folder-per-agent layout is imposed by `adk web` discovery;
all wiring is shared in `_bootstrap.py`.
