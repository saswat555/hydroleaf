from datetime import datetime
import json

class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

def format_json_response(data):
    return json.dumps(data, cls=CustomJSONEncoder, indent=2)