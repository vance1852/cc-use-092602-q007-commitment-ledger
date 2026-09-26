"""无第三方依赖的安置承诺核算 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import CommitmentError, ValidationFailed
from .service import CommitmentService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: CommitmentService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/commitments":
                return Response(201, self.service.create_commitment(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "commitments" and parts[2] == "versions":
                return Response(201, self.service.new_commitment_version(actor, parts[1], payload, str(payload.get("correction_reason", ""))))
            if method == "POST" and len(parts) == 3 and parts[0] == "commitments" and parts[2] == "windows":
                return Response(201, self.service.add_window(actor, parts[1], payload))
            if method == "POST" and path == "/exclusion-rules":
                return Response(201, self.service.add_exclusion_rule(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "exclusion-rules" and parts[2] == "deactivate":
                return Response(200, self.service.deactivate_exclusion_rule(actor, parts[1], str(payload.get("reason", ""))))
            if method == "POST" and len(parts) == 3 and parts[0] == "commitments" and parts[2] == "pauses":
                return Response(201, self.service.register_pause(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "pauses" and parts[2] == "close":
                return Response(200, self.service.close_pause(actor, parts[1], payload["ends_on"], int(payload["expected_revision"]), bool(payload.get("confirmed_by_household", False))))
            if method == "POST" and len(parts) == 5 and parts[0] == "projects" and parts[2] == "batches" and parts[4] == "events":
                return Response(201, self.service.record_event(actor, parts[1], parts[3], payload))
            if method == "GET" and len(parts) == 5 and parts[0] == "projects" and parts[2] == "batches" and parts[4] == "events":
                return Response(200, self.service.list_events(actor, parts[1], parts[3]))
            if method == "POST" and len(parts) == 3 and parts[0] == "commitments" and parts[2] == "settlements":
                return Response(201, self.service.prepare_settlement(
                    actor,
                    parts[1],
                    payload["window_id"],
                    payload["as_of_date"],
                    payload.get("include_observed_until"),
                    str(payload.get("correction_reason", "")),
                ))
            if method == "GET" and len(parts) == 2 and parts[0] == "settlements":
                return Response(200, self.service.settlement(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "settlements" and parts[2] == "operations-approval":
                return Response(200, self.service.approve_operations(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "settlements" and parts[2] == "finance-confirmation":
                return Response(200, self.service.confirm_finance(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "settlements" and parts[2] == "replay":
                return Response(200, self.service.replay_settlement(actor, parts[1]))
            if method == "POST" and len(parts) == 5 and parts[0] == "projects" and parts[2] == "batches" and parts[4] == "recompute":
                return Response(200, self.service.recompute_project_batch(
                    actor,
                    parts[1],
                    parts[3],
                    payload["as_of_date"],
                    payload.get("include_observed_until"),
                ))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except CommitmentError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CommitmentAccounting/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动安置承诺履约核算服务")
    parser.add_argument("--database", type=Path, default=Path("resettlement_commitment.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(CommitmentService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
