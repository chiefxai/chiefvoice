"""Shared normalization for lightweight resource labels."""


def normalize_tags(tags: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Trim, lowercase, deduplicate, and bound user supplied tags."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags or ():
        tag = raw_tag.strip().lower()
        if not tag:
            continue
        if len(tag) > 40:
            raise ValueError("Each tag must be at most 40 characters")
        if tag not in seen:
            seen.add(tag)
            normalized.append(tag)
    if len(normalized) > 20:
        raise ValueError("A resource can have at most 20 tags")
    return tuple(normalized)
