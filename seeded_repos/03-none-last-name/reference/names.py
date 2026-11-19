def full_name(first, last=None):
    if not last:
        return first
    return first + " " + last
