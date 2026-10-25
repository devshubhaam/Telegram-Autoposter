class InlineKeyboardButton:
    def __init__(self, text, url=None, callback_data=None, **k):
        self.text, self.url, self.callback_data = text, url, callback_data
class InlineKeyboardMarkup:
    def __init__(self, rows): self.inline_keyboard = rows
class _Media:
    def __init__(self, media, **k): self.media = media; self.kw = k
class InputMediaPhoto(_Media): pass
class InputMediaVideo(_Media): pass
class InputFile:
    def __init__(self, f, filename=None): self.f, self.filename = f, filename
class Update:
    ALL_TYPES = []
class BotCommand:
    def __init__(self, c, d): pass
