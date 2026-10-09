"""Watch GitHub for the things a developer wants to hear about: CI runs that failed on the
repositories pushed most recently, and pull requests waiting for the user's review.

Polls the REST API with a read-only token (fine-grained: Actions + Pull requests read). The first
poll only learns what is already there; later polls report what is new, through ``on_news``.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request
from typing import Callable

log = logging.getLogger(__name__)
API = "https://api.github.com"


class GitHubWatcher:
    def __init__(self, token: str, on_news: Callable[[str], None] | None = None, interval_s: float = 120.0,
                 repos: int = 8, api: str = API, timeout: float = 15.0):
        self.token, self.on_news, self.interval_s = token, on_news, interval_s
        self.repos, self.api, self.timeout = repos, api.rstrip("/"), timeout
        self.failing: dict[str, dict] = {}  # "owner/repo workflow" -> latest failed run
        self.reviews: dict[str, dict] = {}  # PR url -> {repo, number, title, author}
        self._seen_runs: set[int] = set()
        self._primed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _get(self, path: str):
        req = urllib.request.Request(self.api + path, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "flybot"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def poll(self) -> list[str]:
        """One round: refresh the state and return the news (Thai sentences)."""
        news: list[str] = []
        failing: dict[str, dict] = {}
        for repo in self._get(f"/user/repos?sort=pushed&per_page={self.repos}"):
            name = repo["full_name"]
            runs = self._get(f"/repos/{name}/actions/runs?per_page=10").get("workflow_runs", [])
            latest: dict[str, dict] = {}
            for run in runs:  # newest first: the first run of each workflow+branch is its state
                latest.setdefault(f"{run['name']} {run['head_branch']}", run)
            for key, run in latest.items():
                if run.get("conclusion") != "failure":
                    continue
                failing[f"{name} {key}"] = {"repo": repo["name"], "workflow": run["name"],
                                            "branch": run["head_branch"], "url": run["html_url"],
                                            "commit": (run.get("head_commit") or {}).get("message", "").split("\n")[0]}
                if self._primed and run["id"] not in self._seen_runs:
                    news.append(f"CI ของ {repo['name']} ({run['name']}) พังนะ")
            self._seen_runs.update(run["id"] for run in runs)
        q = urllib.parse.quote("is:open is:pr review-requested:@me archived:false")
        reviews = {}
        for item in self._get(f"/search/issues?q={q}&per_page=20").get("items", []):
            repo = item["repository_url"].rsplit("/", 1)[-1]
            reviews[item["html_url"]] = {"repo": repo, "number": item["number"], "title": item["title"],
                                         "author": item["user"]["login"]}
            if self._primed and item["html_url"] not in self.reviews:
                news.append(f"มี PR รอให้รีวิวใน {repo}: {item['title']}")
        with self._lock:
            self.failing, self.reviews = failing, reviews
        self._primed = True
        return news

    def summary(self) -> dict:
        with self._lock:
            return {"failing_ci": list(self.failing.values()), "review_requested": list(self.reviews.values())}

    def start(self) -> None:
        threading.Thread(target=self._run, name="github", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for line in self.poll():
                    log.info("github: %s", line)
                    if self.on_news:
                        self.on_news(line)
            except Exception as e:  # rate limit, network: try again next round
                log.warning("github poll failed: %s", e)
            self._stop.wait(self.interval_s)
