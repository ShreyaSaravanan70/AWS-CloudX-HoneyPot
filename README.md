# AWS-CloudX-HoneyPot ðŸ¯

A Cloud-based Honeypot-as-a-Service (HaaS) implementation that collects, parses, stores, and monitors SSH honeypot activity using AWS services.

The project can be run locally using Docker and LocalStack, allowing the AWS infrastructure and event-driven pipeline to be tested without deploying resources to a real AWS account.

## Architecture

SSH Honeypot / Cowrie
        |
        | Log files
        v
+----------------------+
|        Amazon S3     |
|     Honeypot Logs    |
+----------+-----------+
           |
     ObjectCreated
           |
           v
+----------------------+
|       Lambda         |
|   CowrieLogParser    |
+----------+-----------+
           |
     Parsed Events
           |
           v
+----------------------+
|      DynamoDB        |
|      CowrieLogs      |
+----------+-----------+
           |
    DynamoDB Stream
           |
           v
+-----------------------------+
|           Lambda            |
| LogDynamoDBToCloudWatch     |
+-------------+---------------+
              |
              v
       CloudWatch Logs

## Upload and analyze Cowrie logs

The dashboard accepts UTF-8 `.log` files up to 16 KiB and 25 parsed events. This caps a dashboard upload at one initial DynamoDB `BatchWriteItem` batch (the service limit is 25 items; SDK retries can still occur) and bounds the in-memory upload body. It is a conservative operational limit, not a claim that larger files meet the live Lambda timeout. The backend uploads each file to `uploads/<unique-id>.log` in the configured bucket. The existing S3 ObjectCreated trigger runs the parser; the API reports “processed” only after every unique `(session, timestamp)` parsed from that file is visible in DynamoDB with its `source_key`. Browser code never receives AWS credentials.

The deployed parser must write `source_key` equal to the S3 object key on every row; upload confirmation matches that value plus `(session, timestamp)`. Deploy the parser update to the existing `CowrieLogParser` Lambda manually before expecting upload status to complete. This project does not create or alter AWS resources.

Minimum permission for the upload call is `s3:PutObject` on `arn:aws:s3:::aws-haas-hazel-demo-2026/uploads/*`. The backend also needs its existing DynamoDB read permissions, including `dynamodb:Scan` on `CowrieLogs`, to confirm processing. Pipeline diagnostics independently call `s3:ListBucket` (HeadBucket), `dynamodb:DescribeTable`, `lambda:GetFunction`, `lambda:ListEventSourceMappings`, and `logs:DescribeLogGroups`; grant these only if those diagnostics are needed. Probe errors identify the permission being denied; an S3 HeadBucket 403 can also conceal a missing bucket. No permissions are granted by this project. Status expectations are held in backend memory for up to one hour, and verification scans `CowrieLogs` because no source-key index exists; a backend restart loses in-progress status and large tables may make checks slower.

Accepted input is timestamped Cowrie text: timestamps start `YYYY-MM-DDTHH:MM:SS.<fraction>Z`; a connection line must match `New connection: <source-ip>:<port> (<destination-ip>:<port>) [session: <id>]`. Later timestamped lines need the source IP to associate them with that session. `test_cowrie_upload.txt` is the mocked upload fixture.

LocalStack:

```powershell
docker compose up --build -d
curl.exe -F "file=@.\test_cowrie.log" http://localhost:8000/api/uploads
```

Real AWS (uses the `haas-real` profile and read-only `.aws` mount):

```powershell
docker compose -f docker-compose.yml -f docker-compose.aws.yml up --build -d backend frontend
curl.exe -F "file=@.\test_cowrie.log" http://localhost:8000/api/uploads
```

The upload response includes an `upload_id`. Poll `http://localhost:8000/api/uploads/<upload_id>` until `status` is `processed`; `processing` means the S3/Lambda/DynamoDB pipeline has not yet been confirmed. After confirmation the dashboard refreshes automatically.

If LocalStack was already initialized before this parser change, update only its Lambda code (no resources or triggers are recreated):

```powershell
docker compose exec localstack sh -lc 'cd /tmp && cp /opt/lambda-src/CowrieLogParser.py . && zip -q -FS CowrieLogParser.zip CowrieLogParser.py && awslocal lambda update-function-code --function-name CowrieLogParser --zip-file fileb:///tmp/CowrieLogParser.zip'
```

For real AWS, manually package and update only the existing parser Lambda before uploading (not run automatically by this project):

```powershell
python -c "import zipfile; z=zipfile.ZipFile('CowrieLogParser-handler-compatible.zip','w',zipfile.ZIP_DEFLATED); z.write('Lambda Functions/CowrieLogParser.py','lambda_function.py'); z.close()"
aws lambda update-function-code --function-name CowrieLogParser --zip-file fileb://CowrieLogParser-handler-compatible.zip --profile haas-real --region us-east-1
```
