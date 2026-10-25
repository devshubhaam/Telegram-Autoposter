import copy, datetime
from types import SimpleNamespace
from bson import ObjectId

def _get(doc, path):
    cur = doc
    for p in path.split("."):
        if isinstance(cur, dict) and p in cur: cur = cur[p]
        else: return None
    return cur

def _cmp_ok(v, cond):
    if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            if op == "$ne":
                if v == arg: return False
            elif op == "$lt":
                if v is None or not v < arg: return False
            elif op == "$lte":
                if v is None or not v <= arg: return False
            elif op == "$gte":
                if v is None or not v >= arg: return False
            elif op == "$in":
                if v not in arg: return False
            else: raise NotImplementedError(op)
        return True
    return v == cond

def match(doc, q):
    for k, cond in q.items():
        if k == "$or":
            if not any(match(doc, sub) for sub in cond): return False
        elif not _cmp_ok(_get(doc, k), cond): return False
    return True

def _set(doc, path, val):
    parts = path.split("."); cur = doc
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict): cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = val

def _unset(doc, path):
    parts = path.split("."); cur = doc
    for p in parts[:-1]:
        cur = cur.get(p)
        if not isinstance(cur, dict): return
    cur.pop(parts[-1], None)

def apply_update(doc, upd, inserting=False):
    for op, fields in upd.items():
        if op == "$set":
            for k, v in fields.items(): _set(doc, k, copy.deepcopy(v))
        elif op == "$inc":
            for k, v in fields.items(): _set(doc, k, (_get(doc, k) or 0) + v)
        elif op == "$unset":
            for k in fields: _unset(doc, k)
        elif op == "$setOnInsert":
            if inserting:
                for k, v in fields.items(): _set(doc, k, copy.deepcopy(v))
        else: raise NotImplementedError(op)

class Cursor:
    def __init__(self, docs): self.docs = docs; self._skip = 0; self._limit = None
    def sort(self, key, direction=None):
        keys = key if isinstance(key, list) else [(key, direction)]
        for k, d in reversed(keys):
            self.docs.sort(key=lambda x, k=k: (_get(x, k) is None, _get(x, k)) if _get(x, k) is not None else (True, 0), reverse=(d == -1))
        return self
    def skip(self, n): self._skip = n; return self
    def limit(self, n): self._limit = n; return self
    def _final(self):
        d = self.docs[self._skip:]
        return d[: self._limit] if self._limit else d
    async def to_list(self, length=None):
        d = self._final()
        return copy.deepcopy(d[:length] if length else d)
    def __aiter__(self):
        self._it = iter(copy.deepcopy(self._final())); return self
    async def __anext__(self):
        try: return next(self._it)
        except StopIteration: raise StopAsyncIteration

class Collection:
    def __init__(self): self.docs = []
    async def create_index(self, *a, **k): return "idx"
    async def drop(self): self.docs.clear()
    def find(self, q=None, projection=None, sort=None):
        c = Cursor([d for d in self.docs if match(d, q or {})])
        if sort: c.sort(sort)
        return c
    async def find_one(self, q=None, projection=None, sort=None):
        c = self.find(q, sort=sort)
        r = c._final()
        return copy.deepcopy(r[0]) if r else None
    async def count_documents(self, q): return len([d for d in self.docs if match(d, q)])
    async def insert_one(self, doc):
        doc = copy.deepcopy(doc); doc.setdefault("_id", ObjectId()); self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])
    async def insert_many(self, docs):
        ids = []
        for d in docs: ids.append((await self.insert_one(d)).inserted_id)
        return SimpleNamespace(inserted_ids=ids)
    async def update_one(self, q, upd, upsert=False):
        for d in self.docs:
            if match(d, q): apply_update(d, upd); return SimpleNamespace(modified_count=1)
        if upsert:
            doc = {k: v for k, v in q.items() if not k.startswith("$") and not isinstance(v, dict)}
            apply_update(doc, upd, True); doc.setdefault("_id", ObjectId()); self.docs.append(doc)
        return SimpleNamespace(modified_count=0)
    async def update_many(self, q, upd):
        n = 0
        for d in self.docs:
            if match(d, q): apply_update(d, upd); n += 1
        return SimpleNamespace(modified_count=n)
    async def find_one_and_update(self, q, upd, upsert=False, return_document=False, sort=None):
        for d in self.docs:
            if match(d, q):
                before = copy.deepcopy(d); apply_update(d, upd)
                return copy.deepcopy(d) if return_document else before
        if upsert:
            doc = {k: v for k, v in q.items() if not k.startswith("$") and not isinstance(v, dict)}
            apply_update(doc, upd, True); doc.setdefault("_id", ObjectId()); self.docs.append(doc)
            return copy.deepcopy(doc)
        return None
    async def replace_one(self, q, new, upsert=False):
        for i, d in enumerate(self.docs):
            if match(d, q):
                new = copy.deepcopy(new); new["_id"] = d["_id"]; self.docs[i] = new; return
        if upsert: await self.insert_one(new)
    async def delete_one(self, q):
        for i, d in enumerate(self.docs):
            if match(d, q): del self.docs[i]; return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)
    async def delete_many(self, q):
        keep = [d for d in self.docs if not match(d, q)]
        n = len(self.docs) - len(keep); self.docs[:] = keep
        return SimpleNamespace(deleted_count=n)

class DB(dict):
    def __missing__(self, k): self[k] = Collection(); return self[k]

class _Admin:
    async def command(self, *_): return {"ok": 1}

class AsyncIOMotorClient:
    def __init__(self, *a, **k): self.dbs = {}; self.admin = _Admin()
    def __getitem__(self, name): return self.dbs.setdefault(name, DB())
    def close(self): pass
