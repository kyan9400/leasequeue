# Security policy

## Supported versions

Security fixes are applied to the latest release.

## Reporting

Use GitHub private vulnerability reporting. Do not include active lease tokens, production payloads, credentials, or database files in a public issue.

## Deployment notes

LeaseQueue does not provide end-user authentication. Place it on a private network or behind an authenticated reverse proxy. Treat job payloads as sensitive application data, protect the data volume, terminate TLS at the edge, and restrict access to the worker mutation endpoints.

The server validates request shapes, caps request and payload sizes, uses opaque lease tokens, compares tokens in constant time, and does not return tokens from read endpoints. These controls do not replace network authentication or encrypted storage.
