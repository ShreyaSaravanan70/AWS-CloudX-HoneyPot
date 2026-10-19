"""AWS clients using the backend's selected profile, region, and endpoint."""

import boto3

from .config import AWS_ENDPOINT_URL, AWS_PROFILE, AWS_REGION


def session():
    return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)


def client(service, **kwargs):
    if AWS_ENDPOINT_URL:
        kwargs.setdefault("endpoint_url", AWS_ENDPOINT_URL)
    return session().client(service, **kwargs)


def resource(service, **kwargs):
    if AWS_ENDPOINT_URL:
        kwargs.setdefault("endpoint_url", AWS_ENDPOINT_URL)
    return session().resource(service, **kwargs)
