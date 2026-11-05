from .base import Adapter, AudioNotReady, Call  # noqa: F401


def make_adapter(kind: str, cfg: dict) -> Adapter:
    """Фабрика адаптеров. Новая АТС = новый модуль здесь + раздел [source.<тип>] в конфиге."""
    if kind == "freepbx":
        from .freepbx import FreePBX
        return FreePBX(cfg)
    raise ValueError(f"неизвестный тип АТС: {kind}")
