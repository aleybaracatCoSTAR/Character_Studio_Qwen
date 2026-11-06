"""Combine explicit collection selections while preserving the legacy QTM file."""
from .archive import ArchiveEntry, ArchiveSettings, retrieve
from .archive_database import ArchiveDatabase
from .collections import CollectionRouter


def qtm_record(value):
    record = value.model_dump() if hasattr(value, 'model_dump') else dict(value)
    return {**record, 'collection_id':'qtm', 'collection_label':'Queering the Map',
            'source_kind':'qtm', 'metadata':{'rights':'CC BY-NC-SA 4.0'},
            'image_url':None, 'image_alt':''}


class _QTMSnapshot:
    def __init__(self, store):
        self.snapshot = None
        self.entries = ()
        if isinstance(store, ArchiveDatabase):
            self.snapshot = store.open_snapshot()
            self.count = self.snapshot.count
        else:
            self.entries = tuple(ArchiveEntry.model_validate(value) for value in store.list())
            self.count = len(self.entries)
        self.closed = False

    def search(self, query, history=()):
        if self.closed: raise ValueError('The collection snapshot is closed.')
        hits = self.snapshot.search(query, history) if self.snapshot else retrieve(self.entries, query, history)
        return [qtm_record(hit) for hit in hits]

    def close(self):
        if self.snapshot is not None: self.snapshot.close()
        self.entries = ()
        self.closed = True


class CollectionCatalog:
    def __init__(self, qtm_store, collections_store):
        self.qtm = qtm_store
        self.collections = collections_store

    def list_collections(self):
        total = self.qtm.count() if isinstance(self.qtm, ArchiveDatabase) else len(self.qtm.list())
        return [{'id':'qtm', 'label':'Queering the Map', 'source_kind':'qtm',
                 'source_url':'https://www.queeringthemap.com/', 'count':total, 'has_images':False},
                *self.collections.list_collections()]

    def validate(self, collection_ids):
        ids = ArchiveSettings(scope='library', collection_ids=collection_ids).collection_ids
        if not ids: raise ValueError('Choose at least one saved collection.')
        known = {value['id'] for value in self.list_collections()}
        if any(value not in known for value in ids): raise KeyError('Unknown collection.')
        return ids

    def _page(self, collection_id, query, offset, limit, *, literal=False):
        if collection_id != 'qtm':
            page = self.collections.page([collection_id], query=query, offset=offset, limit=limit, literal=literal)
            source = next(value for value in self.collections.list_collections() if value['id']==collection_id)
            return {**page, 'entries':[{**entry, 'collection_label':source['label'],
                                       'source_kind':source['source_kind']} for entry in page['entries']]}
        if isinstance(self.qtm, ArchiveDatabase):
            page = self.qtm.page(query=query, offset=offset, limit=limit)
            return {**page, 'entries':[qtm_record(value) for value in page['entries']]}
        entries = [qtm_record(value) for value in self.qtm.list()]
        wanted = query.casefold().strip()
        matching = [entry for entry in entries if not wanted or wanted in (entry['title']+' '+entry['text']).casefold()]
        return {'entries':matching[offset:offset+limit], 'total':len(entries),
                'matched':len(matching), 'offset':offset, 'limit':limit}

    def page(self, collection_ids, query='', offset=0, limit=25, *, literal=False):
        ids = self.validate(collection_ids)
        if (not isinstance(query, str) or len(query)>200 or type(offset) is not int or offset<0
                or type(limit) is not int or not 1<=limit<=50 or type(literal) is not bool):
            raise ValueError('Choose a short search and valid page.')
        summaries = [(value, self._page(value, query, 0, 1, literal=literal)) for value in ids]
        total = sum(page['total'] for _, page in summaries)
        matched = sum(page['matched'] for _, page in summaries)
        remaining, skip, entries = limit, offset, []
        for value, summary in summaries:
            if skip >= summary['matched']:
                skip -= summary['matched']
                continue
            page = self._page(value, query, skip, remaining, literal=literal)
            entries.extend(page['entries'])
            remaining -= len(page['entries'])
            skip = 0
            if not remaining: break
        return {'entries':entries, 'total':total, 'matched':matched, 'offset':offset,
                'limit':limit, 'scope':'library', 'collections':True}

    def open_snapshot(self, collection_ids):
        ids = self.validate(collection_ids)
        snapshots = []
        try:
            # Separate indexes have independent BM25 scores; the router merges
            # ranked lists rather than comparing those scores as absolute values.
            generic_ids = [value for value in ids if value!='qtm']
            generic_opened = False
            for collection_id in ids:
                if collection_id == 'qtm': snapshots.append(_QTMSnapshot(self.qtm))
                elif not generic_opened:
                    # Generic collections share one FTS index and one frozen
                    # copy; selecting eight sources must not copy it eight times.
                    snapshots.append(self.collections.open_snapshot(generic_ids))
                    generic_opened = True
            return CollectionRouter(snapshots)
        except BaseException:
            for snapshot in snapshots: snapshot.close()
            raise
