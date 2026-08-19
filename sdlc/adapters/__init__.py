"""Concrete implementations of the ports — the only code that names a
tool or a framework.

  github.py         RepoHost on the GitHub REST API
  gcloud.py         Deployer + preprod deploys via the gcloud CLI
  store_client.py   Store over the delivery store's MCP surface
  adk/              Google ADK: agent invoker, the per-item and release
                    Workflows, their executors, and the resident apps
"""
