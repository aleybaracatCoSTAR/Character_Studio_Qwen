"""Read one explicitly selected public Queering the Map entry; never crawl the map."""
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

import httpx

QTM_ORIGIN = 'https://www.queeringthemap.com'
MAX_REMOTE_BYTES = 64 * 1024


class ArchiveSourceUnavailable(RuntimeError):
    pass


def qtm_entry_id(value):
    if not isinstance(value, str): raise ValueError('Enter a story number or an individual entry URL.')
    value = value.strip()
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError('Use a plain story number or individual entry URL.')
    if re.fullmatch(r'[0-9]{1,12}', value):
        if int(value) <= 0: raise ValueError('Use a positive story number.')
        return str(int(value))
    try:
        url = urlsplit(value)
        match = re.fullmatch(r'/moment/([0-9]{1,12})/?', url.path)
        if (url.scheme != 'https' or url.netloc not in ('www.queeringthemap.com', 'queeringthemap.com')
                or url.query or url.fragment or not match): raise ValueError()
        if int(match[1]) <= 0: raise ValueError()
        return str(int(match[1]))
    except ValueError:
        raise ValueError('Use a story number or https://www.queeringthemap.com/moment/123, not the map homepage.') from None


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.skip += 1
        elif not self.skip and tag in ('br', 'p', 'div', 'li'): self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip: self.skip -= 1
        elif not self.skip and tag in ('p', 'div', 'li'): self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


def description_text(value):
    if not isinstance(value, str): raise ValueError('The source did not return story text.')
    parser = _PlainText()
    parser.feed(value)
    parser.close()
    text = '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip()).strip()
    if not text or len(text) > 12000: raise ValueError('This story is empty or too long for the selected-story archive.')
    return text


class QueeringTheMap:
    """The site's own unversioned pin-read route, not a full-text search API."""
    def __init__(self, transport=None):
        self.transport = transport

    async def fetch(self, value):
        entry_id = qtm_entry_id(value)
        source_url = f'{QTM_ORIGIN}/moment/{entry_id}'
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=8, follow_redirects=False) as client:
                async with client.stream('GET', source_url, headers={
                    'Accept': 'application/json', 'User-Agent': 'CharacterStudio-SelectedArchive/1.0'}) as response:
                    response.raise_for_status()
                    if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                        raise ValueError('Unexpected source response.')
                    body = bytearray()
                    async for packet in response.aiter_bytes():
                        body.extend(packet)
                        if len(body) > MAX_REMOTE_BYTES: raise ValueError('Source response is too large.')
            import json
            data = json.loads(body)
            if not isinstance(data, dict) or data.get('short_id') != entry_id:
                raise ValueError('Unexpected source entry.')
            text = description_text(data.get('description'))
        except (httpx.HTTPError, ValueError, UnicodeError) as exc:
            raise ArchiveSourceUnavailable(
                'That entry could not be read from Queering the Map. Check its story number, or paste a selected story instead.') from None
        return {'title': f'Queering the Map · {entry_id}', 'text': text,
                'source_url': source_url, 'source_entry_id': entry_id}
