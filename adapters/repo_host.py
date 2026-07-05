"""RepoHost port: the engine's whole view of a code host.

One adapter (GitHub, REST via a fine-grained PAT scoped to the governed
repo). GitLab support = one more adapter + one config value; core stays
untouched. Agents never hold this token — the orchestrator calls these
methods on their behalf, and the coder pushes branches via a
token-authenticated remote URL that only the engine constructs.
"""

import httpx


class GitHubRepoHost:
    def __init__(self, repo: str, token: str,
                 transport: httpx.BaseTransport | None = None):
        self.repo = repo
        self.client = httpx.Client(
            base_url=f"https://api.github.com/repos/{repo}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            transport=transport,
            timeout=30,
        )
        self._token = token

    def _check(self, resp: httpx.Response) -> httpx.Response:
        if resp.status_code >= 300:
            raise RepoHostError(
                f"{resp.request.method} {resp.request.url.path}: "
                f"{resp.status_code} {resp.text[:200]}")
        return resp

    # --- the port ------------------------------------------------------

    def open_pr(self, head: str, title: str, body: str,
                base: str = "main") -> int:
        resp = self._check(self.client.post("/pulls", json={
            "title": title, "body": body, "head": head, "base": base}))
        return resp.json()["number"]

    def find_open_pr(self, head: str) -> int | None:
        """Existing open PR for a branch, if any — resume support: a
        rerun reuses the PR a crashed run already opened."""
        pr = self.find_pr(head, state="open")
        return pr["number"] if pr else None

    def find_pr(self, head: str, state: str = "open") -> dict | None:
        """Newest PR for a branch in the given state ('open'|'all') —
        resume support: {'number', 'state', 'merged'}. A merged PR means
        the item already shipped and must not be re-implemented."""
        owner = self.repo.split("/")[0]
        resp = self._check(self.client.get(
            "/pulls", params={"head": f"{owner}:{head}", "state": state,
                              "sort": "created", "direction": "desc"}))
        prs = resp.json()
        if not prs:
            return None
        return {"number": prs[0]["number"], "state": prs[0]["state"],
                "merged": prs[0].get("merged_at") is not None}

    def post_comment(self, pr: int, body: str) -> None:
        self._check(self.client.post(f"/issues/{pr}/comments",
                                     json={"body": body}))

    def get_diff(self, pr: int) -> str:
        resp = self._check(self.client.get(
            f"/pulls/{pr}", headers={"Accept": "application/vnd.github.diff"}))
        return resp.text

    def get_review_threads(self, pr: int) -> list[dict]:
        """Conversation on the PR: issue comments, newest last.
        Each entry: {author, body, created_at}."""
        resp = self._check(self.client.get(
            f"/issues/{pr}/comments", params={"per_page": 100}))
        return [{"author": c["user"]["login"], "body": c["body"],
                 "created_at": c["created_at"]} for c in resp.json()]

    def merge_pr(self, pr: int) -> str:
        resp = self._check(self.client.put(
            f"/pulls/{pr}/merge", json={"merge_method": "squash"}))
        return resp.json()["sha"]

    def update_title(self, pr: int, title: str) -> None:
        """Verify+label writes verified labels into the PR title."""
        self._check(self.client.patch(f"/pulls/{pr}", json={"title": title}))

    # --- adapter helpers (not part of the port) --------------------------

    def get_pr(self, pr: int) -> dict:
        resp = self._check(self.client.get(f"/pulls/{pr}"))
        data = resp.json()
        return {"number": data["number"], "title": data["title"],
                "body": data["body"] or "", "state": data["state"],
                "head_ref": data["head"]["ref"], "head_sha": data["head"]["sha"],
                "merged": data.get("merged", False)}

    def close_pr(self, pr: int) -> None:
        self._check(self.client.patch(f"/pulls/{pr}", json={"state": "closed"}))

    def authenticated_remote(self) -> str:
        """Remote URL the engine uses for git pushes of agent branches.
        Never logged; never handed to an agent."""
        return f"https://x-access-token:{self._token}@github.com/{self.repo}.git"


class RepoHostError(Exception):
    pass
