"""Short-lived model-input attachments for ordinary browser downloads."""
import re
import secrets
import time

MAX_EXPORT_BYTES = 2 * 1024 * 1024
MAX_EXPORTS = 4
EXPORT_TTL = 120


class ExportDownloads:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.files = {}

    def purge(self):
        now = self.clock()
        self.files = {token:value for token,value in self.files.items() if value['expires'] > now}

    def create(self, filename, content, mime):
        if (not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}',filename)
                or '..' in filename):
            raise ValueError('Choose a plain text or JSON filename.')
        if not isinstance(mime,str): raise ValueError('Choose a plain text or JSON download.')
        mime = mime.split(';',1)[0].strip().lower()
        extensions = {'text/plain':'.txt','application/json':'.json'}
        if mime not in extensions or not filename.lower().endswith(extensions[mime]):
            raise ValueError('Choose a plain text or JSON download.')
        if not isinstance(content,str) or not content: raise ValueError('There is no model input to download.')
        encoded = content.encode('utf-8')
        if len(encoded) > MAX_EXPORT_BYTES: raise OverflowError('The model-input download is too large.')
        self.purge()
        while len(self.files) >= MAX_EXPORTS: self.files.pop(next(iter(self.files)))
        token = secrets.token_hex(24)
        self.files[token] = {'filename':filename,'content':encoded,'mime':mime,'expires':self.clock()+EXPORT_TTL}
        return token

    def get(self, token):
        self.purge()
        return self.files.get(token)

    def clear(self): self.files.clear()
