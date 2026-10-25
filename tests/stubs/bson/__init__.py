import uuid
class ObjectId:
    def __init__(self, v=None):
        if v is None: self._v = uuid.uuid4().hex[:24]
        else:
            v = str(v)
            if len(v) != 24 or any(c not in "0123456789abcdef" for c in v): raise ValueError("invalid ObjectId")
            self._v = v
    def __str__(self): return self._v
    def __repr__(self): return f"ObjectId('{self._v}')"
    def __eq__(self, o): return isinstance(o, ObjectId) and o._v == self._v
    def __hash__(self): return hash(self._v)
    def __lt__(self, o): return self._v < o._v
