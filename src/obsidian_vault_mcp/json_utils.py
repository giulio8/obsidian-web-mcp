"""JSON utilities for date and datetime serialization."""

import json
from datetime import date, datetime


class VaultJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder that converts datetime/date objects to ISO format strings."""

    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)


def json_dumps(obj, **kwargs) -> str:
    """Wrapper around json.dumps using VaultJSONEncoder and ensuring utf-8 by default."""
    kwargs.setdefault("cls", VaultJSONEncoder)
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(obj, **kwargs)
