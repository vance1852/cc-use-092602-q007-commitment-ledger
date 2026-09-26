"""安置承诺核算服务向 API 和 CLI 暴露的稳定错误。"""


class CommitmentError(RuntimeError):
    code = "commitment_error"
    status = 400


class NotFound(CommitmentError):
    code = "not_found"
    status = 404


class Conflict(CommitmentError):
    code = "conflict"
    status = 409


class Forbidden(CommitmentError):
    code = "forbidden"
    status = 403


class InvalidState(CommitmentError):
    code = "invalid_state"
    status = 409


class ValidationFailed(CommitmentError):
    code = "validation_failed"
    status = 422
