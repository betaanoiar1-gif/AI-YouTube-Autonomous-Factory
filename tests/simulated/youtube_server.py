"""A realistic local simulation of the YouTube Data API v3.

SIMULATED / OFFLINE — no real credentials, no real network egress.

The server implements the *documented* YouTube Data API v3 list endpoints over
real HTTP on ``127.0.0.1``:

* ``GET /youtube/v3/search``  (part=snippet, video search, pagination);
* ``GET /youtube/v3/videos``  (part=snippet,statistics,contentDetails);
* ``GET /youtube/v3/channels`` (part=snippet,statistics);
* API-key authentication via the documented ``key`` query parameter, with the
  documented error envelope ``{"error": {"code", "message", "errors": [...]}}``
  (400 keyRequired/keyInvalid, 403 quotaExceeded, 429 rate limit);
* scriptable behaviors: malformed JSON, missing items, arbitrary HTTP status,
  quota exceeded, rate limit with Retry-After, delayed responses (timeouts).

The dataset is deterministic (seeded), so tests are reproducible. The
production ``YouTubeDiscoveryProvider``/``YouTubeClient`` talk to this server
exactly as they would talk to the real API.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

# A clearly-fake Google-style API key. NOT a real credential.
SIMULATED_API_KEY = "AIzaSySIMULATED_OFFLINE_KEY_0000000001"  # fake value, offline only

_TOPICS = [
    "forgotten tunnels",
    "lost expeditions",
    "abandoned railways",
    "cold war bunkers",
    "ancient aqueducts",
    "shipwreck mysteries",
    "underground cities",
    "vanished inventions",
]
_PLACES = ["Roman Empire", "Soviet Union", "Wild West", "Imperial China", "Wild North"]
_TITLE_TEMPLATES = [
    "The {topic} of the {place} Nobody Talks About",
    "How the {place} Lost Its {topic}",
    "{topic} of the {place}: What Really Happened",
    "Why the {place} Hid Its {topic}",
    "Exploring the {topic} of the {place}",
    "The {place} {topic} Mystery",
    "What Is the {topic} of the {place}?",
    "The Truth About the {place} {topic}",
]
_CHANNEL_NAMES = [
    "History Underground",
    "Forgotten Places",
    "Deep Time Docs",
    "The Lost Archives",
    "Cold Ground Stories",
]


@dataclass
class SimulatedYouTubeVideo:
    video_id: str
    channel_id: str
    title: str
    description: str
    published_at: str
    duration_seconds: int
    views: int
    likes: int
    comments: int
    tags: list[str]
    category_id: str
    channel_title: str


@dataclass
class SimulatedYouTubeChannel:
    channel_id: str
    title: str
    description: str
    published_at: str
    subscriber_count: int
    view_count: int
    video_count: int


def make_dataset(
    *, video_count: int = 40, channel_count: int = 5, seed: int = 20261008
) -> tuple[list[SimulatedYouTubeVideo], list[SimulatedYouTubeChannel]]:
    """Deterministic dataset with recurring topics, varied saturation, and an
    underserved theme (low views despite recurring demand)."""
    import random

    rng = random.Random(seed)  # noqa: S311 - deterministic test data, not security
    channels = []
    for index in range(channel_count):
        channel_id = f"UCSIM{index:012d}"
        video_count_channel = video_count // channel_count + (
            1 if index < video_count % channel_count else 0
        )
        channels.append(
            SimulatedYouTubeChannel(
                channel_id=channel_id,
                title=_CHANNEL_NAMES[index % len(_CHANNEL_NAMES)],
                description=f"Documentaries about {_PLACES[index % len(_PLACES)]}.",
                published_at="2019-03-01T00:00:00Z",
                subscriber_count=rng.randint(50_000, 900_000),
                view_count=0,  # filled below
                video_count=video_count_channel,
            )
        )
    videos: list[SimulatedYouTubeVideo] = []
    for index in range(video_count):
        channel = channels[index % channel_count]
        topic = _TOPICS[index % len(_TOPICS)]
        place = _PLACES[index % len(_PLACES)]
        template = _TITLE_TEMPLATES[index % len(_TITLE_TEMPLATES)]
        # "ancient aqueducts" is the UNDERSERVED theme: recurring in titles but
        # with consistently low views, while every other topic is established
        # (high views) — so saturation classification is deterministic.
        underserved = topic == "ancient aqueducts"
        views = rng.randint(800, 4_000) if underserved else rng.randint(600_000, 900_000)
        video = SimulatedYouTubeVideo(
            video_id=f"VIDSIM{index:012d}",
            channel_id=channel.channel_id,
            title=template.format(topic=topic, place=place),
            # A shared niche phrase so broad queries (e.g. "history mystery")
            # match many videos and paginate realistically.
            description=f"A documentary about {topic}. Part of our history mystery series. " * 3,
            published_at=f"2024-{1 + index % 12:02d}-{1 + index % 28:02d}T12:00:00Z",
            duration_seconds=rng.choice([240, 600, 900, 1500, 2400]),
            views=views,
            likes=int(views * rng.uniform(0.01, 0.05)),
            comments=int(views * rng.uniform(0.0005, 0.004)),
            tags=[topic.split()[0], place.lower().replace(" ", "-")],
            category_id="22",
            channel_title=channel.title,
        )
        videos.append(video)
        channel.view_count += views
    return videos, channels


@dataclass
class SimulatedYouTubeBehavior:
    """How the simulated API should respond to the next request(s)."""

    status: int = 200
    # Response body modes: normal | invalid_json | missing_items
    mode: str = "normal"
    delay_seconds: float = 0.0


@dataclass
class SimulatedYouTubeRequest:
    """One request observed by the simulated API."""

    method: str
    path: str
    endpoint: str
    authorized: bool
    params: dict[str, str]


@dataclass
class SimulatedYouTubeState:
    valid_key: str = SIMULATED_API_KEY
    videos: list[SimulatedYouTubeVideo] = field(default_factory=list)
    channels: list[SimulatedYouTubeChannel] = field(default_factory=list)
    behaviors: list[SimulatedYouTubeBehavior] = field(default_factory=list)
    requests: list[SimulatedYouTubeRequest] = field(default_factory=list)

    def queue(self, *behaviors: SimulatedYouTubeBehavior) -> None:
        self.behaviors.extend(behaviors)

    def next_behavior(self) -> SimulatedYouTubeBehavior:
        if self.behaviors:
            return self.behaviors.pop(0)
        return SimulatedYouTubeBehavior()

    def count(self, *, endpoint: str | None = None, **param_filters: str) -> int:
        """Count observed requests, optionally filtered by endpoint/params."""
        count = 0
        for request in self.requests:
            if endpoint is not None and request.endpoint != endpoint:
                continue
            if any(request.params.get(key) != value for key, value in param_filters.items()):
                continue
            count += 1
        return count


class _SimulatedYouTubeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state: SimulatedYouTubeState) -> None:
        super().__init__(("127.0.0.1", 0), _SimulatedYouTubeHandler)
        self.sim_state = state


class _SimulatedYouTubeHandler(BaseHTTPRequestHandler):
    """Implements the documented YouTube Data API v3 list endpoints."""

    server: _SimulatedYouTubeHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        """Silence default stderr logging."""

    @property
    def state(self) -> SimulatedYouTubeState:
        return self.server.sim_state

    # -- plumbing ---------------------------------------------------------

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> None:
        body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionError):
            # The client may already have timed out and closed the socket.
            self.wfile.write(body)

    def _send_error(
        self,
        code: int,
        message: str,
        reason: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        headers = {"Retry-After": str(int(retry_after))} if retry_after is not None else {}
        self._send_json(
            code,
            {
                "error": {
                    "code": code,
                    "message": message,
                    "errors": [
                        {
                            "domain": "youtube.api",
                            "reason": reason,
                            "message": message,
                        }
                    ],
                }
            },
            extra_headers=headers,
        )

    def _authorized(self, params: dict[str, str]) -> bool:
        return params.get("key") == self.state.valid_key

    # -- endpoints --------------------------------------------------------

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        params = {k: v[0] for k, v in parse_qs(split.query).items()}
        endpoint = split.path.rsplit("/", 1)[-1]

        if not self._authorized(params):
            self.state.requests.append(
                SimulatedYouTubeRequest("GET", split.path, endpoint, False, params)
            )
            if "key" not in params:
                self._send_error(400, "API key required.", "keyRequired")
            else:
                self._send_error(
                    400, "API key not valid. Please pass a valid API key.", "keyInvalid"
                )
            return

        self.state.requests.append(
            SimulatedYouTubeRequest("GET", split.path, endpoint, True, params)
        )
        behavior = self.state.next_behavior()
        if behavior.delay_seconds > 0:
            time.sleep(behavior.delay_seconds)
        if behavior.status != 200:
            reason = {
                403: "quotaExceeded",
                429: "rateLimitExceeded",
                500: "backendError",
                503: "backendError",
            }.get(behavior.status, "internalError")
            self._send_error(
                behavior.status,
                f"Simulated YouTube API error (HTTP {behavior.status}).",
                reason,
                retry_after=1.0 if behavior.status == 429 else None,
            )
            return
        if behavior.mode == "invalid_json":
            self._send_json(200, {}, raw_body=b"not json {{{")
            return
        if endpoint == "search":
            self._handle_search(params, behavior)
        elif endpoint == "videos":
            self._handle_videos(params, behavior)
        elif endpoint == "channels":
            self._handle_channels(params, behavior)
        else:
            self._send_error(404, f"Unknown endpoint: {split.path}", "notFound")

    # -- endpoint implementations -----------------------------------------

    def _handle_search(self, params: dict[str, str], behavior: SimulatedYouTubeBehavior) -> None:
        query = (params.get("q") or "").lower()
        max_results = max(1, min(int(params.get("maxResults", "5")), 50))
        page_token = params.get("pageToken")
        page_index = (
            int(page_token.split("-")[1]) if page_token and page_token.startswith("page-") else 0
        )

        # Deterministic result set for the query: videos whose title or
        # description matches any query term.
        terms = [term for term in query.split() if term]
        matched = [
            video
            for video in self.state.videos
            if not terms
            or any(
                term in video.title.lower() or term in video.description.lower() for term in terms
            )
        ]
        start = page_index * max_results
        page_items = matched[start : start + max_results]
        next_index = page_index + 1
        has_more = start + max_results < len(matched)

        items = []
        for video in page_items:
            items.append(
                {
                    "kind": "youtube#searchResult",
                    "etag": "simulated",
                    "id": {"kind": "youtube#video", "videoId": video.video_id},
                    "snippet": {
                        "publishedAt": video.published_at,
                        "channelId": video.channel_id,
                        "title": video.title,
                        "description": video.description,
                        "channelTitle": video.channel_title,
                    },
                }
            )
        payload: dict[str, Any] = {
            "kind": "youtube#searchListResponse",
            "etag": "simulated",
            "pageInfo": {"totalResults": len(matched), "resultsPerPage": len(page_items)},
            "items": items,
        }
        if behavior.mode == "missing_items":
            payload.pop("items")
        if has_more:
            payload["nextPageToken"] = f"page-{next_index}"
        self._send_json(200, payload)

    def _handle_videos(self, params: dict[str, str], behavior: SimulatedYouTubeBehavior) -> None:
        ids = [vid.strip() for vid in (params.get("id") or "").split(",") if vid.strip()]
        if not ids:
            self._send_error(400, "Required parameter: id", "invalidParameter")
            return
        by_id = {video.video_id: video for video in self.state.videos}
        items = []
        for video_id in ids:
            video = by_id.get(video_id)
            if video is None:
                continue  # unknown ids are simply absent (documented behavior)
            items.append(
                {
                    "kind": "youtube#video",
                    "etag": "simulated",
                    "id": video.video_id,
                    "snippet": {
                        "publishedAt": video.published_at,
                        "channelId": video.channel_id,
                        "title": video.title,
                        "description": video.description,
                        "channelTitle": video.channel_title,
                        "tags": video.tags,
                        "categoryId": video.category_id,
                    },
                    "statistics": {
                        "viewCount": str(video.views),
                        "likeCount": str(video.likes),
                        "commentCount": str(video.comments),
                    },
                    "contentDetails": {
                        "duration": _iso8601_duration(video.duration_seconds),
                        "dimension": "2d",
                        "definition": "hd",
                    },
                }
            )
        payload = {"kind": "youtube#videoListResponse", "etag": "simulated", "items": items}
        if behavior.mode == "missing_items":
            payload.pop("items")
        self._send_json(200, payload)

    def _handle_channels(self, params: dict[str, str], behavior: SimulatedYouTubeBehavior) -> None:
        ids = [cid.strip() for cid in (params.get("id") or "").split(",") if cid.strip()]
        if not ids:
            self._send_error(400, "Required parameter: id", "invalidParameter")
            return
        by_id = {channel.channel_id: channel for channel in self.state.channels}
        items = []
        for channel_id in ids:
            channel = by_id.get(channel_id)
            if channel is None:
                continue
            items.append(
                {
                    "kind": "youtube#channel",
                    "etag": "simulated",
                    "id": channel.channel_id,
                    "snippet": {
                        "title": channel.title,
                        "description": channel.description,
                        "publishedAt": channel.published_at,
                    },
                    "statistics": {
                        "viewCount": str(channel.view_count),
                        "subscriberCount": str(channel.subscriber_count),
                        "videoCount": str(channel.video_count),
                    },
                }
            )
        payload = {"kind": "youtube#channelListResponse", "etag": "simulated", "items": items}
        if behavior.mode == "missing_items":
            payload.pop("items")
        self._send_json(200, payload)


def _iso8601_duration(seconds: int) -> str:
    """Format seconds as an ISO 8601 duration (``PT1H2M3S``)."""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = ["PT"]
    if hours:
        parts.append(f"{hours}H")
    if minutes:
        parts.append(f"{minutes}M")
    if secs or (not hours and not minutes):
        parts.append(f"{secs}S")
    return "".join(parts)


class SimulatedYouTubeServer:
    """Context-managed local simulation of the YouTube Data API v3.

    Usage::

        with SimulatedYouTubeServer() as server:
            settings = YouTubeSettings(
                youtube_API_KEY=SIMULATED_API_KEY,
                base_url=server.base_url,
            )
            provider = YouTubeDiscoveryProvider(settings)  # production code path
            ...
    """

    def __init__(
        self,
        *,
        valid_key: str = SIMULATED_API_KEY,
        video_count: int = 40,
        channel_count: int = 5,
        seed: int = 20261008,
    ) -> None:
        videos, channels = make_dataset(
            video_count=video_count, channel_count=channel_count, seed=seed
        )
        self.state = SimulatedYouTubeState(valid_key=valid_key, videos=videos, channels=channels)
        self._httpd = _SimulatedYouTubeHTTPServer(self.state)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """Base URL of the simulated API (``/youtube/v3`` path prefix included)."""
        address = self._httpd.server_address
        return f"http://{address[0]!s}:{int(address[1])}/youtube/v3"

    def start(self) -> SimulatedYouTubeServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> SimulatedYouTubeServer:
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()
