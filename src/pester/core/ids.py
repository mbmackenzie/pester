from ulid import ULID


def new_id() -> str:
    """A new sortable, globally unique identifier (ULID)."""
    return str(ULID())
