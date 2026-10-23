from typing import Any

from bottle import Bottle, request, response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from mirenai.api.errors import error, parse_body, read_pagination
from mirenai.api.schemas import PolicyCreate, PolicyUpdate
from mirenai.domain.policy import VALID_ACTIONS
from mirenai.repository import policies as repo


def register_policy_routes(app: Bottle) -> None:
    @app.get("/policies")
    def list_policies() -> dict[str, Any]:
        action = request.query.get("action")
        if action is not None and action not in VALID_ACTIONS:
            return error("validation_error", "Invalid request", 422, "invalid action filter")
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            rows = repo.list_policies(action=action)
            return {"status": "ok", "policies": rows, "total": len(rows)}
        rows = repo.list_policies(limit=limit, offset=offset, action=action)
        return {"status": "ok", "policies": rows, "total": repo.count_policies(action=action)}

    @app.get("/policies/<policy_id:int>")
    def get_policy(policy_id: int) -> dict[str, Any]:
        row = repo.get_policy(policy_id)
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "policy": row}

    @app.post("/policies")
    def create_policy() -> dict[str, Any]:
        model, err = parse_body(PolicyCreate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        try:
            row = repo.create_policy(model.model_dump())
        except IntegrityError:
            return error("conflict", "a policy already exists for this client/domain", 409)
        response.status = 201
        return {"status": "ok", "policy": row}

    @app.put("/policies/<policy_id:int>")
    def update_policy(policy_id: int) -> dict[str, Any]:
        model, err = parse_body(PolicyUpdate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        current = repo.get_policy(policy_id)
        if current is None:
            return error("not_found", "not found", 404)
        changes = model.model_dump(exclude_unset=True)
        merged = {field: current[field] for field in PolicyCreate.model_fields}
        merged.update(changes)
        try:
            PolicyCreate.model_validate(merged)
        except ValidationError as exc:
            return error("validation_error", "Invalid request", 422, str(exc.errors()[0]["msg"]))
        try:
            row = repo.update_policy(policy_id, changes)
        except IntegrityError:
            return error("conflict", "a policy already exists for this client/domain", 409)
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "policy": row}

    @app.delete("/policies/<policy_id:int>")
    def delete_policy(policy_id: int) -> dict[str, Any]:
        if not repo.delete_policy(policy_id):
            return error("not_found", "not found", 404)
        return {"status": "ok", "deleted": policy_id}
