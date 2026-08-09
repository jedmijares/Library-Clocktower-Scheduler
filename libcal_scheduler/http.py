"""HTTP transport for chipl.libcal.com.

`urllib.request` is serviceable but rough in ways that matter here, so its edges are
confined to this module:

* `HTTPError` swallows the response body unless you read it off the exception, and
  LibCal puts its actual complaint there (`Invalid Referrer.` is a 17-byte body behind
  a bare 403).
* There is no default timeout, so a hung connection hangs forever.
* Form encoding and headers have to be assembled by hand.

Two site requirements are enforced here rather than left to callers:

* **`Referer` is mandatory.** Without it the availability grid returns
  `403 Invalid Referrer.`
* **`Crawl-delay: 10`** from robots.txt is honoured by spacing requests.

The User-Agent identifies this tool honestly; it is not disguised as a browser.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

BASE = "https://chipl.libcal.com"

USER_AGENT = (
    "libcal-scheduler/0.1 (personal meeting-room booking assistant; "
    "+https://github.com/-/libcal-scheduler)"
)

CRAWL_DELAY_SECONDS = 10.0
DEFAULT_TIMEOUT = 30.0


class HttpError(Exception):
    """A non-2xx response, carrying the body LibCal put in it."""

    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body
        detail = body.strip()[:200] or "(empty body)"
        super().__init__(f"{status} from {url}: {detail}")


@dataclass
class Exchange:
    """One request/response pair, for the audit log."""

    method: str
    url: str
    payload: dict
    status: int
    body: str


class Client:
    """Paced HTTP client. One instance per run so spacing is shared."""

    def __init__(
        self,
        *,
        crawl_delay: float = CRAWL_DELAY_SECONDS,
        timeout: float = DEFAULT_TIMEOUT,
        opener=None,
    ) -> None:
        self.crawl_delay = crawl_delay
        self.timeout = timeout
        self.exchanges: list[Exchange] = []
        self._last_request: float | None = None
        self._opener = opener or urllib.request.urlopen

    def _wait_turn(self) -> None:
        if self._last_request is None or self.crawl_delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.crawl_delay:
            time.sleep(self.crawl_delay - elapsed)

    def post(self, path: str, payload: dict, *, referer_lid: int) -> str:
        """POST form-encoded data and return the body text.

        `referer_lid` builds the Referer the site insists on; it is required rather
        than optional so it cannot be forgotten.
        """
        url = f"{BASE}{path}"
        body = urllib.parse.urlencode(payload, doseq=True).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": USER_AGENT,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}/reserve?lid={referer_lid}",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        self._wait_turn()
        try:
            with self._opener(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", "replace")
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", "replace")
            self._last_request = time.monotonic()
            self.exchanges.append(Exchange("POST", url, payload, err.code, text))
            raise HttpError(err.code, url, text) from None
        finally:
            self._last_request = time.monotonic()

        self.exchanges.append(Exchange("POST", url, payload, status, text))
        return text

    def post_json(self, path: str, payload: dict, *, referer_lid: int) -> dict:
        text = self.post(path, payload, referer_lid=referer_lid)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise HttpError(200, f"{BASE}{path}", f"expected JSON, got: {text[:200]}") from None
        if not isinstance(parsed, dict):
            raise HttpError(200, f"{BASE}{path}", f"expected a JSON object, got {type(parsed)}")
        return parsed

    def audit_log(self) -> str:
        """The run's exchanges as JSON lines, for writing under logs/."""
        return "\n".join(
            json.dumps(
                {
                    "method": e.method,
                    "url": e.url,
                    "payload": e.payload,
                    "status": e.status,
                    "body": e.body[:4000],
                },
                ensure_ascii=False,
            )
            for e in self.exchanges
        )
