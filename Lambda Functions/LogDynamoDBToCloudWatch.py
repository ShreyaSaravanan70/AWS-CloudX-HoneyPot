SAFE_LOG_FIELDS = {"session", "timestamp", "src_ip", "eventid", "source_key"}


def _log_value(field, attribute):
    """Keep identifying event metadata; never emit payload or unknown fields."""
    if field not in SAFE_LOG_FIELDS:
        return "[REDACTED]"
    return next(iter(attribute.values()), "")

def lambda_handler(event, context):
    for record in event['Records']:
        if record['eventName'] == 'INSERT':
            new_image = record['dynamodb']['NewImage']
            print("New insert into CowrieLogs:")
            for key, val in new_image.items():
                print(f"   {key}: {_log_value(key, val)}")
    return {'statusCode': 200}
