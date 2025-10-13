from typing import Optional
from pydantic import BaseModel


class AllOptional(BaseModel.__class__):
    def __new__(cls, name, bases, namespaces, **kwargs):
        annotations = namespaces.get("__annotations__", {})
        for base in bases:
            annotations.update(getattr(base, "__annotations__", {}))
        for field in annotations:
            if not field.startswith("__"):
                annotations[field] = Optional[annotations[field]]
                # ensure default None so Pydantic v2 treats as optional
                if field not in namespaces:
                    namespaces[field] = None
        namespaces["__annotations__"] = annotations
        return super().__new__(cls, name, bases, namespaces, **kwargs)
