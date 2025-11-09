from dataclasses import asdict, is_dataclass
from enum import Enum

def convert(obj):
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return {k: convert(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [convert(v) for v in obj]
    if isinstance(obj, dict):
        return {k: convert(v) for k, v in obj.items()}
    return obj


def normalize_whitespace(text):
    import re
    # Use regex to replace one or more whitespace characters with a single space
    import re
    # First, normalize all whitespace to single spaces
    text = re.sub(r'\s+', ' ', text).strip()
    # Then, remove spaces after '('
    text = re.sub(r'\(\s+', '(', text)
    # Remove spaces before ')'
    text = re.sub(r'\s+\)', ')', text)
    # Remove spaces after '<'
    text = re.sub(r'<\s+', '<', text)
    # Remove spaces before '>'
    text = re.sub(r'\s+>', '>', text)
    return text