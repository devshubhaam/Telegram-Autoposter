class TelegramError(Exception): pass
class NetworkError(TelegramError): pass
class BadRequest(NetworkError): pass
class TimedOut(NetworkError): pass
class Forbidden(TelegramError): pass
class Conflict(TelegramError): pass
class InvalidToken(TelegramError): pass
class RetryAfter(TelegramError):
    def __init__(self, retry_after): super().__init__(f"retry {retry_after}"); self.retry_after = retry_after
class ChatMigrated(TelegramError):
    def __init__(self, new_chat_id): super().__init__("migrated"); self.new_chat_id = new_chat_id
