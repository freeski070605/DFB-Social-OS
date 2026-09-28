class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status
        super().__init__(message)


class ProviderError(DomainError):
    def __init__(self, message: str, *, transient=False, uncertain=False):
        super().__init__(message, 502)
        self.transient, self.uncertain = transient, uncertain
