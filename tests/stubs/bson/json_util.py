import json, datetime
from . import ObjectId
class JSONMode: RELAXED = 1
class JSONOptions:
    def __init__(self, *a, **k): pass
def _default(o):
    if isinstance(o, datetime.datetime): return {"$date": o.isoformat()}
    if isinstance(o, ObjectId): return {"$oid": str(o)}
    raise TypeError(type(o))
def _hook(d):
    if "$date" in d: return datetime.datetime.fromisoformat(d["$date"]).replace(tzinfo=datetime.UTC)
    if "$oid" in d: return ObjectId(d["$oid"])
    return d
def dumps(o, indent=None, json_options=None): return json.dumps(o, indent=indent, default=_default)
def loads(s, json_options=None): return json.loads(s, object_hook=_hook)
