from typing_extensions import Literal

ChatTypeName = Literal["PrivateChat", "GroupChat", "SystemChat"]
ReactionMode = Literal["accept", "reject_one", "reject_all"]


def extra(name: str, desc: str):
    def attr_dec(fn):
        setattr(fn, "extra_fn", True)
        setattr(fn, "name", name)
        setattr(fn, "desc", desc)
        return fn

    return attr_dec
