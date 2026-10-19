
import re
import os
import boto3
from datetime import datetime
from urllib.parse import unquote_plus

# Table name and region are read from the environment so the same code works
# in LocalStack (AWS_ENDPOINT_URL set by docker-compose) and real AWS
# (standard credential chain). No credentials or endpoints are hardcoded.
DYNAMODB_TABLE = os.environ.get("HAA_DYNAMODB_TABLE", "CowrieLogs")
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")

# Regex patterns
TIMESTAMP_PATTERN = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)')
NEW_CONNECTION_PATTERN = re.compile(
    r'New connection: ([\d\.]+):\d+ \(\d+\.\d+\.\d+\.\d+:\d+\) \[session: (\w+)\]'
)
IP_PATTERN = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
AUTH_PATTERN = re.compile(r"login attempt \[b'([^']+)'/b'([^']+)'\]")
COMMAND_PATTERN = re.compile(r'(CMD|Command found):\s+(.*)', re.DOTALL)

# Byte-string repr wrappers (require the leading ``b`` so ordinary quoted
# strings such as ``'hello'`` or ``"hello"`` are NOT unwrapped).
_BYTES_REPR_RE = re.compile(r"^b'(.*?)'$|^b\"(.*?)\"$", re.DOTALL)


def clean_value(value):
    """Normalize a stored field value.

    Cowrie sometimes leaves Python byte-string repr artifacts such as
    ``b'root'`` or ``b"admin123"`` in the ``input``/``command`` fields.
    This strips only the repr wrapper when the value *is* a bytes repr,
    leaving real command text (e.g. a command that legitimately contains
    the characters ``b'``) untouched.
    """
    if not value or value == 'none':
        return value
    if not isinstance(value, str):
        return value
    m = _BYTES_REPR_RE.match(value)
    if m:
        inner = m.group(1)
        if inner is None:
            inner = m.group(2)
        return inner if inner is not None else value
    return value

def parse_log_line(line, current_sessions):
    entry = {
        'timestamp': None,
        'eventid': 'unknown',
        'src_ip': 'unknown',
        'session': 'unknown',
        'input': 'none',
        'command': 'none'
    }

    # Timestamp
    timestamp_match = TIMESTAMP_PATTERN.match(line)
    if timestamp_match:
        entry['timestamp'] = datetime.strptime(
            timestamp_match.group(1).rstrip('Z'),
            "%Y-%m-%dT%H:%M:%S.%f"
        ).isoformat()

    # New connection line
    new_conn_match = NEW_CONNECTION_PATTERN.search(line)
    if new_conn_match:
        src_ip = new_conn_match.group(1)
        session_id = new_conn_match.group(2)
        current_sessions[src_ip] = session_id
        entry.update({
            'eventid': 'CowrieSSHFactory',
            'src_ip': src_ip,
            'session': session_id
        })
        return entry

    # General IP
    ip_match = IP_PATTERN.search(line)
    if ip_match:
        src_ip = ip_match.group(0)
        entry['src_ip'] = src_ip
        entry['session'] = current_sessions.get(src_ip, 'unknown')

# EventID extraction
    components = re.findall(r'\[([^\]]+)\]', line)
    # Skip credential-payload brackets (e.g. [b'root'/b'password']) so
    # byte-string reprs don't leak into the stored eventid field. Only
    # applied when at least one non-payload bracket remains, so real
    # event/session components are never lost.
    filtered = [c for c in components if not re.search(r"\bb['\"]", c)]
    if filtered:
        components = filtered
    if components:
        entry['eventid'] = components[-1].split('.')[-1].split('#')[0]

    # Login attempt
    if 'login attempt' in line:
        auth_match = AUTH_PATTERN.search(line)
        if auth_match:
            entry['input'] = f"{auth_match.group(1)}:{auth_match.group(2)}"

    # Command execution
    cmd_match = COMMAND_PATTERN.search(line)
    if cmd_match:
        command = cmd_match.group(2).strip()[:500]
        entry['command'] = command.replace('\n', ' ').replace('\t', ' ')

    # Normalize byte-string repr artifacts (b'root', b"admin123", ...) so
    # stored/displayed values are clean strings. This only affects values
    # that *are* bytes reprs; real command text is left untouched.
    entry['input'] = clean_value(entry['input'])
    entry['command'] = clean_value(entry['command'])

    # Skip system logs
    if any(msg in line for msg in ['Failed to import', 'Twisted Version']):
        return None

    return entry

def lambda_handler(event, context):
    # boto3 picks up credentials from the standard chain (env vars,
    # ~/.aws/credentials, or IAM role). In LocalStack the endpoint is
    # supplied via AWS_ENDPOINT_URL, which boto3 respects automatically.
    s3 = boto3.client("s3", region_name=AWS_REGION)
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = dynamodb.Table(DYNAMODB_TABLE)
    try:
        current_sessions = {}  # Map IPs to session IDs
        processed = 0

        with table.batch_writer() as batch:
            for record in event["Records"]:
                bucket = record["s3"]["bucket"]["name"]
                key = unquote_plus(record["s3"]["object"]["key"])
                obj = s3.get_object(Bucket=bucket, Key=key)

                for raw_line in obj['Body'].iter_lines():
                    if not raw_line.strip():
                        continue
                    line = raw_line.decode('utf-8')
                    try:
                        parsed = parse_log_line(line, current_sessions)
                        if parsed and parsed['timestamp'] and parsed['session'] != 'unknown':
                            parsed['source_key'] = key
                            batch.put_item(Item={
                                'session': parsed['session'],
                                'timestamp': parsed['timestamp'],
                                'source_key': parsed['source_key'],
                                'src_ip': parsed['src_ip'],
                                'eventid': parsed['eventid'],
                                'input': parsed['input'],
                                'command': parsed['command']
                            })
                            processed += 1
                    except Exception as e:
                        # Log only safe metadata; Cowrie lines can contain
                        # attempted passwords or commands with credentials.
                        print(f"Error processing record in {key}: {type(e).__name__}")

        print(f"Processed {processed} entries from {len(event['Records'])} S3 objects")
        return {'statusCode': 200}

    except Exception as e:
        print(f"Fatal error: {str(e)}")
        raise e
