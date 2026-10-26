"""A thin GitHub REST client over urllib (stdlib only), authenticated with
the workflow's GITHUB_TOKEN. Only the calls the review check, the confirm
job and the reviews sync make. Every call has a timeout; a failure is a
`GitHubError` carrying a plain reason, which the check turns into a fail
(fail closed, §18.0 M3)."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
TIMEOUT_S = 30
BOT_LOGIN = "github-actions[bot]"


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, token: str | None = None, api: str = API):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.api = api.rstrip("/")

    # ------------------------------------------------------------ transport

    def request(self, method: str, path: str, *, body=None, params=None,
                accept: str = "application/vnd.github+json", ok=(200, 201, 204)):
        """`(status, data)`; data is parsed JSON, or text for non-JSON media
        types. Statuses outside `ok` raise GitHubError (404 included unless
        the caller lists it)."""
        url = path if path.startswith("http") else self.api + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", accept)
        req.add_header("User-Agent", "fused-apps-index-review")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read() or b""
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubError(f"GitHub could not be reached ({type(e).__name__})") from e
        if status not in ok:
            raise GitHubError(f"GitHub answered {status} for {method} {path}", status)
        if not raw:
            return status, None
        if "json" in accept and "diff" not in accept:
            try:
                return status, json.loads(raw)
            except ValueError:
                return status, raw.decode("utf-8", "replace")
        return status, raw.decode("utf-8", "replace")

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)[1]

    def paged(self, path: str, *, params=None, limit: int = 1000, key: str | None = None):
        """Every item of a paginated list endpoint (100 per page), at most
        `limit`. `key` names the list inside an object answer (search)."""
        out: list = []
        page = 1
        while len(out) < limit:
            p = dict(params or {}, per_page=100, page=page)
            data = self.get(path, params=p)
            items = data.get(key, []) if key and isinstance(data, dict) else data
            if not isinstance(items, list) or not items:
                break
            out.extend(items)
            if len(items) < 100:
                break
            page += 1
        return out[:limit]

    # -------------------------------------------------------------- reading

    def repo(self, repo: str) -> dict | None:
        status, data = self.request("GET", f"/repos/{repo}", ok=(200, 404, 451))
        return data if status == 200 and isinstance(data, dict) else None

    def user_type(self, login: str) -> str | None:
        """"User" | "Organization" | None (no such account)."""
        status, data = self.request("GET", f"/users/{_q(login)}", ok=(200, 404))
        return data.get("type") if status == 200 and isinstance(data, dict) else None

    def is_public_member(self, org: str, login: str) -> bool:
        status, _ = self.request("GET", f"/orgs/{_q(org)}/public_members/{_q(login)}",
                                 ok=(204, 404, 302))
        return status == 204

    def permission(self, repo: str, login: str) -> str:
        """The login's role on `repo` ("admin" | "maintain" | "write" |
        "triage" | "read" | "none"); "none" when GitHub will not say."""
        status, data = self.request(
            "GET", f"/repos/{repo}/collaborators/{_q(login)}/permission",
            ok=(200, 403, 404))
        if status != 200 or not isinstance(data, dict):
            return "none"
        return data.get("role_name") or data.get("permission") or "none"

    def compare(self, repo: str, base: str, head: str) -> dict | None:
        status, data = self.request(
            "GET", f"/repos/{repo}/compare/{_q(base)}...{_q(head)}",
            params={"per_page": 100}, ok=(200, 404))
        return data if status == 200 and isinstance(data, dict) else None

    def file_text(self, repo: str, path: str, ref: str) -> str | None:
        """A file's text at `ref`, or None when it is not there."""
        status, data = self.request(
            "GET", f"/repos/{repo}/contents/{_qp(path)}", params={"ref": ref},
            ok=(200, 404))
        if status != 200 or not isinstance(data, dict) or data.get("type") != "file":
            return None
        if data.get("encoding") == "base64" and isinstance(data.get("content"), str):
            try:
                return base64.b64decode(data["content"]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None
        return None

    def path_is_file(self, repo: str, path: str, ref: str) -> bool:
        status, data = self.request(
            "GET", f"/repos/{repo}/contents/{_qp(path)}", params={"ref": ref},
            ok=(200, 404))
        return status == 200 and isinstance(data, dict) and data.get("type") == "file"

    def pull(self, repo: str, number: int) -> dict:
        return self.get(f"/repos/{repo}/pulls/{int(number)}")

    def pull_files(self, repo: str, number: int) -> list:
        return self.paged(f"/repos/{repo}/pulls/{int(number)}/files", limit=3000)

    def commit_pulls(self, repo: str, sha: str) -> list:
        status, data = self.request("GET", f"/repos/{repo}/commits/{_q(sha)}/pulls",
                                    ok=(200, 404, 422))
        return data if status == 200 and isinstance(data, list) else []

    def search_issues(self, query: str, *, limit: int = 1000) -> list:
        return self.paged("/search/issues", params={"q": query, "sort": "created",
                                                    "order": "asc"},
                          limit=limit, key="items")

    def issue_comments(self, repo: str, number: int) -> list:
        return self.paged(f"/repos/{repo}/issues/{int(number)}/comments", limit=3000)

    # -------------------------------------------------------------- writing

    def create_comment(self, repo: str, number: int, body: str) -> dict:
        return self.request("POST", f"/repos/{repo}/issues/{int(number)}/comments",
                            body={"body": body})[1]

    def edit_comment(self, repo: str, comment_id: int, body: str) -> dict:
        return self.request("PATCH", f"/repos/{repo}/issues/comments/{int(comment_id)}",
                            body={"body": body})[1]

    def add_labels(self, repo: str, number: int, labels: list) -> None:
        self.request("POST", f"/repos/{repo}/issues/{int(number)}/labels",
                     body={"labels": list(labels)})

    def remove_label(self, repo: str, number: int, label: str) -> None:
        self.request("DELETE",
                     f"/repos/{repo}/issues/{int(number)}/labels/{_q(label)}",
                     ok=(200, 204, 404))

    def set_status(self, repo: str, sha: str, state: str, description: str,
                   context: str = "review", target_url: str | None = None) -> None:
        """A commit status on `sha` (state pending | success | failure | error)."""
        body = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        self.request("POST", f"/repos/{repo}/statuses/{_q(sha)}", body=body)

    def react(self, repo: str, comment_id: int, content: str = "+1") -> None:
        self.request("POST", f"/repos/{repo}/issues/comments/{int(comment_id)}/reactions",
                     body={"content": content}, ok=(200, 201))


def _q(part: str) -> str:
    return urllib.parse.quote(str(part), safe="")


def _qp(path: str) -> str:
    return "/".join(_q(p) for p in str(path).split("/"))
