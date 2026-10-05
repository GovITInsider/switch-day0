class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


class FormError(Exception):
    def __init__(self, messages):
        super().__init__("; ".join(messages))
        self.messages = list(messages)


class LockError(AppError):
    pass


class AuthRequired(Exception):
    def __init__(self, next_url):
        self.next_url = next_url


class Forbidden(Exception):
    pass
