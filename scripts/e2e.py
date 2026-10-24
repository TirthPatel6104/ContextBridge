"""
End-to-end smoke test against a running ContextBridge server.

Exercises the whole workflow over HTTP — import → review → sharing boundary →
retrieve → prompt → ledger → health → export/import → merge → delete — and
fails loudly on the first unexpected response.  Used by CI against the Docker
image and by the deploy workflow against the live service.

    python scripts/e2e.py --base-url http://localhost:8000 [--api-key KEY]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import uuid

import httpx

TRANSCRIPT = (
    "User: Hi, I'm Priya. I'm a backend engineer at a fintech startup.\n\n"
    "Assistant: Nice to meet you. What are you working on?\n\n"
    "User: I'm building a payments reconciliation service in Python. "
    "I decided to use PostgreSQL instead of SQLite for production.\n\n"
    "Assistant: Good choice. Anything else?\n\n"
    "User: The requirement is that every ledger entry must be idempotent. "
    "I still need to write the Alembic migration. "
    "I decided to use priya@example.com as the billing alerts contact.\n"
)


def check(cond: bool, message: str) -> None:
    if not cond:
        print(f"FAIL: {message}")
        sys.exit(1)
    print(f"ok   {message}")


def wait_ready(client: httpx.Client, timeout: float) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            res = client.get("/readyz")
            if res.status_code == 200:
                print(f"ready: {res.json()}")
                return
            last = f"{res.status_code} {res.text[:100]}"
        except httpx.HTTPError as exc:
            last = exc.__class__.__name__
        time.sleep(2)
    print(f"FAIL: server not ready after {timeout}s ({last})")
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--timeout", type=float, default=90)
    args = ap.parse_args()

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    name = "e2e_" + uuid.uuid4().hex[:8]
    with httpx.Client(base_url=args.base_url.rstrip("/"), headers=headers, timeout=30) as c:
        wait_ready(c, args.timeout)
        check(c.get("/livez").json()["ok"], "livez")
        health = c.get("/api/v1/health")
        check(health.status_code == 200, f"health ({health.status_code})")
        info = health.json()
        print(
            f"     backend={info['storage_backend']} env={info['environment']} "
            f"tracing={info['tracing']}"
        )
        check(c.get("/openapi.json").json()["info"]["title"] == "ContextBridge API", "openapi")
        check(c.get("/").status_code == 200, "dashboard page")

        res = c.post(
            "/api/v1/extract",
            data={"package_name": name, "model": "local"},
            files=[("files[]", ("chat.txt", io.BytesIO(TRANSCRIPT.encode()), "text/plain"))],
        )
        check(res.status_code == 200, f"extract ({res.status_code}: {res.text[:120]})")
        body = res.json()
        check(
            body["created"] and body["total_items"] >= 3, f"extracted {body['total_items']} items"
        )
        check(body["redaction"]["total"] >= 1, "email redacted on import")
        check("priya@example.com" not in json.dumps(body["memory"]), "no raw email in response")

        detail = c.get(f"/api/v1/packages/{name}").json()
        items = [i for cat in detail["memory"].values() for i in cat]
        decision = next(i for i in items if i["category"] == "decisions")
        res = c.post(
            f"/api/v1/packages/{name}/items/sharing",
            json={"ids": [decision["id"]], "policy": "local_only"},
        )
        check(res.json()["changed"] == 1, "sharing boundary set to local_only")

        res = c.post(
            "/api/v1/retrieve",
            json={"package_name": name, "query": "which database for production", "top_k": 2},
        )
        check(res.status_code == 200 and res.json()["selected"], "retrieve explains selection")

        cloud = c.post(
            "/api/v1/prompt",
            json={"package_name": name, "target_model": "claude", "surface": "e2e"},
        ).json()
        check(cloud["withheld_count"] == 1, "local_only item withheld from Claude")
        check(decision["id"] not in cloud["included_ids"], "withheld id absent from prompt")
        local = c.post(
            "/api/v1/prompt",
            json={"package_name": name, "target_model": "llama3", "surface": "e2e"},
        ).json()
        check(decision["id"] in local["included_ids"], "same item rendered for a local model")

        audit = c.get(f"/api/v1/packages/{name}/audit").json()
        check(audit["summary"]["events"] == 2, "egress ledger recorded both builds")
        check(
            audit["summary"]["items_shared_with_cloud"] >= 1, "ledger knows what reached the cloud"
        )
        health = c.get(f"/api/v1/packages/{name}/health").json()
        check(health["package_name"] == name, "health report")

        exported = c.get(f"/api/v1/packages/{name}/export")
        check(exported.status_code == 200, "export portable package")
        res = c.post("/api/v1/packages/import", json={**exported.json(), "rename": name + "_copy"})
        check(res.status_code == 200, "import portable package")
        res = c.post(
            "/api/v1/packages/merge",
            json={"names": [name, name + "_copy"], "new_name": name + "_merged", "dry_run": True},
        )
        check(res.json()["summary"]["duplicate_groups"] >= 1, "merge dry-run folds duplicates")

        emb = c.get(f"/api/v1/packages/{name}/embeddings").json()
        print(f"     embeddings persistent={emb['persistent']} stored={emb['embeddings']}")

        for n in (name, name + "_copy"):
            check(c.delete(f"/api/v1/packages/{n}").status_code == 200, f"delete {n}")
        check(c.get(f"/api/v1/packages/{name}").status_code == 404, "package gone")
        golden = c.get("/api/v1/eval/golden").json()
        check(golden["pairs"] >= 50, f"golden harness served ({golden['pairs']} pairs)")
    print("E2E PASSED")


if __name__ == "__main__":
    main()
