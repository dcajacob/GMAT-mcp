class GmatError(Exception):
    def __init__(self, code: str, message: str, **details: object):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}
